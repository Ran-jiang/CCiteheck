#!/usr/bin/env python3
"""CCiteheck 自包含离线安装包组装器。

用法：
    python3 tools/package/build.py --platform mac-arm64|mac-x64|win-x64 [--version 20260719] [--cross]

产出 dist/CCiteheck-<version>-<platform>.zip（无密钥；密钥由 tools/release/inject_env.sh 注入）。
组装内容：PBS 便携 Python + pip 依赖 + Node 二进制 + 预装 EUR-Lex/证书工具
+ 项目运行文件（白名单）+ 平台安装脚本。下载物缓存于 tools/package/cache/ 并校验 sha256。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform as host_platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PKG_DIR = Path(__file__).resolve().parent
REPO = PKG_DIR.parents[1]
CACHE = PKG_DIR / "cache"
DIST = REPO / "dist"
LOCK = json.loads((PKG_DIR / "runtime-versions.lock").read_text(encoding="utf-8"))

# 项目运行文件白名单（相对仓库根）
PROJECT_INCLUDE = [
    "src",
    "apps",
    "laws/common_laws.json",
    "data/laws.sqlite",
]
# 白名单内的排除（目录名或相对路径片段）
PROJECT_EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache"}

PIP_CROSS_PLATFORMS = {
    "win-x64": ["win_amd64"],
    "mac-x64": ["macosx_10_13_x86_64", "macosx_11_0_x86_64", "macosx_10_9_x86_64"],
    "mac-arm64": ["macosx_11_0_arm64"],
}


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, filename: str, expected_sha256: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / filename
    if target.exists() and sha256_of(target) == expected_sha256:
        log(f"缓存命中 {filename}")
        return target
    log(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=600) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    actual = sha256_of(target)
    if actual != expected_sha256:
        target.unlink(missing_ok=True)
        raise SystemExit(f"sha256 校验失败：{filename}\n  期望 {expected_sha256}\n  实际 {actual}")
    return target


def host_key() -> str:
    system, machine = host_platform.system(), host_platform.machine()
    if system == "Darwin":
        return "mac-arm64" if machine == "arm64" else "mac-x64"
    if system == "Windows":
        return "win-x64"
    return f"{system.lower()}-{machine}"


def extract_python(archive: Path, staging: Path) -> Path:
    """PBS install_only 包根目录为 python/，原样落到 runtime/python。"""
    runtime = staging / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(runtime, filter="tar")
    result = runtime / "python"
    if not result.is_dir():
        raise SystemExit(f"PBS 解包后未找到 python/ 目录：{archive.name}")
    return result


def extract_node(archive: Path, staging: Path, target_platform: str) -> None:
    """只保留 node 可执行文件本体。"""
    node_dir = staging / "runtime" / "node"
    if target_platform.startswith("mac"):
        (node_dir / "bin").mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/bin/node"))
            with tar.extractfile(member) as src, (node_dir / "bin" / "node").open("wb") as dst:
                shutil.copyfileobj(src, dst)
        (node_dir / "bin" / "node").chmod(0o755)
    else:
        node_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            member = next(n for n in bundle.namelist() if n.endswith("/node.exe"))
            with bundle.open(member) as src, (node_dir / "node.exe").open("wb") as dst:
                shutil.copyfileobj(src, dst)


def pip_install(python_bin: Path, staging: Path, target_platform: str, cross: bool) -> None:
    runtime_reqs = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    target = staging / "runtime" / "site-packages"
    interpreter = str(python_bin) if python_bin.exists() else sys.executable
    cmd = [interpreter, "-m", "pip", "install", "--quiet", "--no-compile",
           "--target", str(target)]
    if cross:
        cmd += ["--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12"]
        for pip_platform in PIP_CROSS_PLATFORMS[target_platform]:
            cmd += ["--platform", pip_platform]
    cmd += runtime_reqs
    log(f"pip 安装依赖（cross={cross}）")
    subprocess.run(cmd, check=True)


def npm_vendor(staging: Path) -> None:
    for key in ("eurlex", "certs"):
        spec = LOCK["npm_packages"][key]
        prefix = staging / "vendor" / key
        prefix.mkdir(parents=True, exist_ok=True)
        log(f"npm 预装 {spec['name']}@{spec['version']}")
        subprocess.run(
            ["npm", "install", "--prefix", str(prefix),
             f"{spec['name']}@{spec['version']}",
             "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"],
            check=True, shell=(host_platform.system() == "Windows"),
        )
        entry = prefix / "node_modules" / spec["name"] / spec["entry"]
        if not entry.exists():
            raise SystemExit(f"vendor 入口缺失：{entry}")
        native = [p for p in prefix.rglob("*") if p.suffix == ".node" or p.name == "binding.gyp"]
        if native:
            raise SystemExit(f"vendor/{key} 含原生扩展，跨平台复用假设失效：{native[:3]}")


def copy_project(staging: Path) -> None:
    log("拷贝项目运行文件")

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in PROJECT_EXCLUDE_DIRS or n.endswith(".pyc")}

    for rel in PROJECT_INCLUDE:
        src = REPO / rel
        dst = staging / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def copy_payload(staging: Path, bundle_dir: Path, target_platform: str) -> None:
    common = PKG_DIR / "payload" / "common"
    shutil.copy2(common / "env.template", staging / ".env.template")
    # 输出用 ASCII 文件名，避免跨平台 zip 中文文件名乱码
    shutil.copy2(common / "README-安装说明.md", bundle_dir / "README.md")
    (staging / "logs").mkdir(exist_ok=True)
    (staging / "logs" / ".keep").write_text("")

    if target_platform.startswith("mac"):
        plat = PKG_DIR / "payload" / "mac"
        shutil.copytree(plat / "bin", staging / "bin", dirs_exist_ok=True)
        for tmpl in plat.glob("*.plist.tmpl"):
            shutil.copy2(tmpl, staging / tmpl.name)
        for script in ("install.command", "uninstall.command"):
            shutil.copy2(plat / script, bundle_dir / script)
            (bundle_dir / script).chmod(0o755)
        for script in (staging / "bin").glob("*.sh"):
            script.chmod(0o755)
    else:
        plat = PKG_DIR / "payload" / "windows"
        shutil.copytree(plat / "bin", staging / "bin", dirs_exist_ok=True)
        for tmpl in plat.glob("*.xml.tmpl"):
            shutil.copy2(tmpl, staging / tmpl.name)
        for script in ("install.bat", "install.ps1", "uninstall.ps1"):
            shutil.copy2(plat / script, bundle_dir / script)
        # Windows PowerShell 5.1 对无 BOM 文件按 ANSI 代码页解析，
        # 含中文的 .ps1 必须带 UTF-8 BOM 才能正确解析；
        # bat/cmd/vbs/ps1 统一 CRLF，避免 LF-only 批处理的解析边角问题
        win_scripts = list(bundle_dir.glob("*.ps1")) + list(bundle_dir.glob("*.bat")) \
            + list((staging / "bin").glob("*.cmd")) + list((staging / "bin").glob("*.vbs"))
        for script in win_scripts:
            data = script.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if script.suffix == ".ps1" and not data.startswith(b"\xef\xbb\xbf"):
                data = b"\xef\xbb\xbf" + data
            script.write_bytes(data)


def assert_no_secrets(bundle_dir: Path) -> None:
    log("防呆检查：包内不得含密钥")
    offenders = [p for p in bundle_dir.rglob(".env") if p.name == ".env"]
    if offenders:
        raise SystemExit(f"包内不得含 .env：{offenders}")
    import re
    key_pattern = re.compile(r"(sk-[A-Za-z0-9]{24,}|gho_[A-Za-z0-9]{20,}|DASHSCOPE_API_KEY=\S{8,})")
    for path in bundle_dir.rglob("*"):
        if path.is_file() and path.suffix in {".template", ".md", ".sh", ".cmd", ".ps1", ".bat", ".py", ".tmpl"}:
            if key_pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                raise SystemExit(f"疑似密钥泄漏：{path}")


def make_zip(bundle_dir: Path, zip_path: Path) -> None:
    log(f"打包 {zip_path.name}")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.unlink(missing_ok=True)
    if host_platform.system() == "Windows":
        shutil.make_archive(str(zip_path.with_suffix("")), "zip",
                            bundle_dir.parent, bundle_dir.name)
    else:
        subprocess.run(["zip", "-ryq", str(zip_path), bundle_dir.name],
                       cwd=bundle_dir.parent, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["mac-arm64", "mac-x64", "win-x64"])
    parser.add_argument("--version", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--cross", action="store_true",
                        help="允许在非目标平台组装（依赖 wheel 交叉获取；产物须在目标平台真机验证）")
    args = parser.parse_args()

    cross = host_key() != args.platform
    if cross and not args.cross:
        raise SystemExit(f"宿主 {host_key()} ≠ 目标 {args.platform}；确需交叉组装请加 --cross")

    bundle_name = f"CCiteheck-{args.version}-{args.platform}"
    bundle_dir = DIST / "staging" / bundle_name
    staging = bundle_dir / "payload"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    staging.mkdir(parents=True)

    pbs = LOCK["python_build_standalone"]
    asset = pbs["assets"][args.platform]
    python_archive = download(
        f"https://github.com/astral-sh/python-build-standalone/releases/download/{pbs['release']}/{asset['file']}",
        asset["file"], asset["sha256"])
    python_dir = extract_python(python_archive, staging)

    node = LOCK["node"]
    node_asset = node["assets"][args.platform]
    node_archive = download(
        f"https://nodejs.org/dist/v{node['version']}/{node_asset['file']}",
        node_asset["file"], node_asset["sha256"])
    extract_node(node_archive, staging, args.platform)

    python_bin = python_dir / ("python.exe" if args.platform == "win-x64" else "bin/python3")
    pip_install(python_bin if not cross else Path(sys.executable), staging, args.platform, cross)

    npm_vendor(staging)
    copy_project(staging)
    copy_payload(staging, bundle_dir, args.platform)

    (staging / "VERSION").write_text(
        f"version={args.version}\nplatform={args.platform}\n"
        f"python={pbs['python_version']}+{pbs['release']}\nnode={node['version']}\n",
        encoding="utf-8")

    assert_no_secrets(bundle_dir)
    zip_path = DIST / f"{bundle_name}.zip"
    make_zip(bundle_dir, zip_path)
    size_mb = zip_path.stat().st_size / 1e6
    log(f"完成：{zip_path}（{size_mb:.0f} MB）")


if __name__ == "__main__":
    main()
