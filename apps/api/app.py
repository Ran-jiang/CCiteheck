"""Microsoft Word 加载项的 FastAPI 后端。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ccitecheck.application import (
    DocumentPipelineError,
    extract_document_claims,
    parse_and_validate_document,
    verify_document_claims,
)
from ccitecheck.infrastructure.paths import PROJECT_ROOT
from ccitecheck.infrastructure.config import load_project_env
from ccitecheck.infrastructure.debug_timing import timing_session
from ccitecheck.output import summarize_verification

from .docx_text import sanitize_for_docx
from .schema import (
    DebugEventRequest,
    DocumentCheckRequest,
    DocumentCheckResponse,
    SelectionCheckRequest,
)
from .debug_capture import append_event, create_run, write_json

ADDIN_ROOT = PROJECT_ROOT / "apps" / "word_addin"
INSTALL_ROOT = ADDIN_ROOT / "install"
LAW_DB = PROJECT_ROOT / "data" / "laws.sqlite"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
# 保活间隔：核查超过该秒数仍未完成时，向连接发送保活空白防止 WKWebView 超时
KEEPALIVE_SECONDS = 15

load_project_env()
app = FastAPI(title="CCiteheck API", version="1.0.0")
allowed_origins = [
    origin.strip()
    for origin in os.getenv("CCITECHECK_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
@app.middleware("http")
async def revalidate_static_assets(request, call_next):
    """Office WebView 磁盘缓存极顽固；静态资源强制每次向服务端校验新鲜度。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(("/assets", "/shared-assets")) or path.endswith((".html", ".css", ".js")):
        response.headers["Cache-Control"] = "no-cache"
    if path in {"/", "/taskpane.html", "/help.html", "/install.html"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://appsforoffice.microsoft.com; "
            "style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


app.mount("/assets", StaticFiles(directory=ADDIN_ROOT / "assets"), name="assets")
app.mount(
    "/shared-assets",
    StaticFiles(directory=PROJECT_ROOT / "apps" / "shared" / "assets"),
    name="shared-assets",
)
@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/taskpane.html")


@app.get("/taskpane.html", include_in_schema=False)
def taskpane() -> FileResponse:
    return FileResponse(ADDIN_ROOT / "taskpane.html")


@app.get("/help.html", include_in_schema=False)
def help_page() -> FileResponse:
    return FileResponse(ADDIN_ROOT / "help.html")


@app.get("/manifest.xml", include_in_schema=False)
def addin_manifest() -> FileResponse:
    """公网 manifest:Windows"上传我的加载项"选文件上传,或由管理员集中部署。"""
    return FileResponse(ADDIN_ROOT / "manifest.render.xml", media_type="application/xml")


@app.get("/install.html", include_in_schema=False)
def install_page() -> FileResponse:
    return FileResponse(INSTALL_ROOT / "index.html")


@app.get("/install/install-ccitecheck.command", include_in_schema=False)
def mac_installer() -> FileResponse:
    """macOS 安装器以仓库 tools/ 下的脚本为唯一来源,避免副本漂移。"""
    return FileResponse(
        PROJECT_ROOT / "tools" / "word-installers" / "mac" / "install-ccitecheck.command",
        media_type="text/x-shellscript",
    )


@app.get("/install/install-ccitecheck.bat", include_in_schema=False)
def windows_installer_bat() -> FileResponse:
    """Windows 安装器(.bat 与同目录 .ps1 配对使用)以仓库 tools/ 下的脚本为唯一来源。"""
    return FileResponse(
        PROJECT_ROOT / "tools" / "word-installers" / "windows" / "install-ccitecheck.bat",
        media_type="application/x-bat",
    )


@app.get("/install/install-ccitecheck.ps1", include_in_schema=False)
def windows_installer_ps1() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "tools" / "word-installers" / "windows" / "install-ccitecheck.ps1",
        media_type="text/x-powershellscript",
    )


app.mount("/install", StaticFiles(directory=INSTALL_ROOT), name="install")


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    from ccitecheck.domain.citation import CLAIM_SCHEMA_VERSION
    from ccitecheck.domain.result import VERIFICATION_SCHEMA_VERSION
    from ccitecheck.verification.semantic import SUPPORTED_MODELS, model_api_key

    return {
        "status": "ok",
        "claim_schema_version": CLAIM_SCHEMA_VERSION,
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
        "pkulaw_configured": bool(os.getenv("PKULAW_ACCESS_TOKEN", "").strip()),
        "ansvar_configured": bool(os.getenv("ANSVAR_MCP_GATEWAY", "").strip()),
        "llm_configured": any(model_api_key(model) for model in SUPPORTED_MODELS),
    }


@app.get("/api/models")
def list_models() -> dict:
    """可选语义核查模型；configured 表示该模型所需密钥是否已配置。"""
    from ccitecheck.verification.semantic import (
        SUPPORTED_MODELS, model_api_key, resolve_model_option,
    )

    return {
        "default": resolve_model_option(None).key,
        "models": [
            {"key": m.key, "label": m.label, "configured": bool(model_api_key(m))}
            for m in SUPPORTED_MODELS
        ],
    }


def _validate_scope(request) -> None:
    if not (request.include_statutes or request.include_cases):
        raise HTTPException(status_code=400, detail="请至少选择一种核查范围（法规引用或司法案例）")


@app.post("/api/checks")
async def check_document(request: DocumentCheckRequest):
    # 快速校验（大小、格式、范围）同步返回正确状态码；随后进入保活流式响应，
    # 核查在线程池执行，期间每隔 15 秒发送保活空白，避免 Word 插件所在的
    # WKWebView 因大文书核查超过 ~60 秒网络超时而报 "Load failed"。
    _validate_scope(request)
    document_bytes = _decode_and_validate(request.docx_base64)

    async def stream():
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            lambda: _run_document_check(request, document_bytes),
        )
        while True:
            try:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=KEEPALIVE_SECONDS)
                break
            except asyncio.TimeoutError:
                yield b" "  # 保活：JSON 允许前导空白，客户端 JSON.parse 忽略
            except HTTPException as exc:
                yield json.dumps({"__stream_error__": True, "detail": exc.detail}).encode("utf-8")
                return
            except Exception as exc:  # noqa: BLE001 - 兜底避免连接悬挂
                yield json.dumps({"__stream_error__": True, "detail": f"核查失败：{exc}"}).encode("utf-8")
                return
        yield response.model_dump_json().encode("utf-8")

    return StreamingResponse(stream(), media_type="application/json")


def _run_document_check(
    request: DocumentCheckRequest, document_bytes: bytes
) -> DocumentCheckResponse:
    debug_run_id = create_run("document", document_bytes)
    write_json(debug_run_id, "request.json", {
        **request.model_dump(exclude={"docx_base64"}),
        "docx_base64_length": len(request.docx_base64),
    })

    with timing_session() as timer:
        try:
            with tempfile.TemporaryDirectory(prefix="ccitecheck-document-") as temporary_dir:
                document_path = Path(temporary_dir) / "document.docx"
                document_path.write_bytes(document_bytes)
                with timer.measure("recognition.total"):
                    parsed_document = parse_and_validate_document(document_path)
                    write_json(debug_run_id, "parsed-document.json", parsed_document)
                    claim_document = extract_document_claims(
                        parsed_document,
                        include_statutes=request.include_statutes,
                        include_cases=request.include_cases,
                    )
                    write_json(debug_run_id, "claim-document.json", claim_document)
                verification = verify_document_claims(
                    claim_document,
                    LAW_DB,
                    semantic_check=request.semantic_check,
                    qwen_model=getattr(request, "model", None),
                    include_statutes=request.include_statutes,
                    include_cases=request.include_cases,
                )
        except DocumentPipelineError as exc:
            write_json(debug_run_id, "error.json", {"type": type(exc).__name__, "message": str(exc)})
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            write_json(debug_run_id, "error.json", {"type": type(exc).__name__, "message": str(exc)})
            raise

        with timer.measure("output"):
            response = DocumentCheckResponse(
                file_name=Path(request.file_name).name,
                document_key="sha256:" + hashlib.sha256(document_bytes).hexdigest(),
                semantic_check=request.semantic_check,
                summary=summarize_verification(verification),
                verification=verification,
                debug_run_id=debug_run_id,
            )
            write_json(debug_run_id, "response.json", response)
        return response


@app.post("/api/checks/selection", response_model=DocumentCheckResponse)
def check_selection(request: SelectionCheckRequest) -> DocumentCheckResponse:
    """核查用户在 Word 中选中的文本片段：构造临时 DOCX 复用完整核查管线。"""
    _validate_scope(request)
    lines = [line.strip() for line in request.text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="选中内容为空，无法核查")

    from docx import Document as DocxDocument

    debug_document = (
        _decode_document(request.debug_docx_base64)
        if request.debug_docx_base64
        else None
    )
    debug_run_id = create_run("selection", debug_document)
    write_json(debug_run_id, "request.json", {
        **request.model_dump(exclude={"debug_docx_base64"}),
        "debug_docx_base64_length": len(request.debug_docx_base64 or ""),
    })

    with timing_session() as timer:
        try:
            with tempfile.TemporaryDirectory(prefix="ccitecheck-selection-") as temporary_dir:
                selection_path = Path(temporary_dir) / "selection.docx"
                with timer.measure("recognition.total"):
                    selection_doc = DocxDocument()
                    for line in lines:
                        selection_doc.add_paragraph(sanitize_for_docx(line))
                    selection_doc.save(selection_path)
                    parsed_document = parse_and_validate_document(selection_path)
                    write_json(debug_run_id, "parsed-selection.json", parsed_document)
                    claim_document = extract_document_claims(
                        parsed_document,
                        include_statutes=request.include_statutes,
                        include_cases=request.include_cases,
                    )
                    write_json(debug_run_id, "claim-document.json", claim_document)
                verification = verify_document_claims(
                    claim_document,
                    LAW_DB,
                    semantic_check=request.semantic_check,
                    qwen_model=getattr(request, "model", None),
                    include_statutes=request.include_statutes,
                    include_cases=request.include_cases,
                )
                _rebase_selection_locations(verification, request.source_blocks)
        except DocumentPipelineError as exc:
            write_json(debug_run_id, "error.json", {"type": type(exc).__name__, "message": str(exc)})
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            write_json(debug_run_id, "error.json", {"type": type(exc).__name__, "message": str(exc)})
            raise

        with timer.measure("output"):
            response = DocumentCheckResponse(
                file_name=f"{Path(request.file_name).name}（选中片段）",
                document_key="sha256:" + hashlib.sha256(
                    (Path(request.file_name).name + "\0" + request.text).encode("utf-8")
                ).hexdigest(),
                semantic_check=request.semantic_check,
                summary=summarize_verification(verification),
                verification=verification,
                debug_run_id=debug_run_id,
            )
            write_json(debug_run_id, "response.json", response)
        return response


def _rebase_selection_locations(verification, source_blocks) -> None:
    """把临时选区 DOCX 的段落坐标映射回当前 Word 文档。"""
    located_items = [*verification.statute_results, *verification.case_results]
    for check in located_items:
        rebased = []
        for location in check.source_locations:
            parts = location.block_id.split(":")
            if len(parts) != 3 or parts[:2] != ["word", "p"]:
                continue
            index = int(parts[2])
            if index >= len(source_blocks):
                continue
            source = source_blocks[index]
            location.block_id = source.block_id
            location.char_start += source.char_start
            location.char_end += source.char_start
            # 选区临时 DOCX 中的出现序号不等于原文所属段落中的出现序号。
            location.occurrence = None
            rebased.append(location)
        check.source_locations = rebased


@app.post("/api/debug-events", status_code=204)
def capture_debug_event(request: DebugEventRequest) -> None:
    try:
        append_event(request.run_id, {
            "event": request.event,
            "payload": request.payload,
        })
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _decode_document(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="DOCX Base64 数据无效") from exc


def _decode_and_validate(encoded: str) -> bytes:
    document_bytes = _decode_document(encoded)
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="文档超过 25 MB 限制")
    if not document_bytes.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="文件不是有效的 DOCX 文档")
    return document_bytes
