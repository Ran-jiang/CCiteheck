# CCiteheck

面向 Microsoft Word 的可追溯法律引用核查工具。

[在线安装与体验](https://cciteheck-api.onrender.com/install.html) · [功能介绍](https://cciteheck-api.onrender.com/help.html)

核心数据生命周期：

```text
RawClaim
→ SearchHypothesis
→ RetrievalEvidence
→ VerificationResult
→ Word Output
```

`Recognition` 忠实记录原文；`Query Construction` 构造检索假设；
`Retrieval` 执行指定权威来源；`Verification` 只比较原文与既有证据；
`Output` 映射为现有 Word 卡片；`Scheduler` 只控制下一步动作。

## 项目结构

```text
src/ccitecheck/
├── parsing/             # DOCX 与 Word 文档结构
├── recognition/         # 原文事实识别
├── query_construction/  # 法名、法域、版本、定位符和来源计划
├── retrieval/           # 单一权威来源执行
├── verification/        # 确定性与语义证据比较
├── orchestration/       # Scheduler / Control Plane
├── output/              # Word 兼容输出
├── domain/              # 生命周期领域模型
└── infrastructure/      # 配置、数据库与 HTTP

apps/
├── api/                 # Word 后端 transport
└── word_addin/          # Microsoft Word 加载项
```

## 运行条件

- Python 3.12+
- Node.js / npm
- 本地法规库：`data/laws.sqlite`

语义核查默认使用服务端环境变量：

```env
DASHSCOPE_API_KEY=你的百炼APIKey
QWEN_MODEL=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

北大法宝为可选回退来源，使用 `PKULAW_ACCESS_TOKEN` 与
`PKULAW_MCP_GATEWAY`。Word 定位信息可能包含敏感原文；调试现场默认写入
`debug_runs/`，可用 `CCITECHECK_DEBUG_CAPTURE=0` 关闭。

## 本地运行

```bash
python3 -m pip install -r requirements.txt
npm install
npm run certs
npm start
```

将 [apps/word_addin/manifest.xml](apps/word_addin/manifest.xml) 复制到 Word
加载项目录后重启 Word。Word for Mac 的目录为：

```text
~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```

校验 manifest：

```bash
npm run validate:manifest
```

## 测试

```bash
PYTHONPATH=src pytest -q
npm run test:js
```

FastAPI 仅保留 Word 所需的文件核查、选区核查、health、模型配置和调试事件接口；
CLI、飞书和独立 Web 产品入口已退出产品范围。
