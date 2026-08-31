# literature-evidence-mcp

一个面向普通用户的本地文献证据工具：把用户明确选择的 Markdown 或带文本层 PDF 复制进新的冻结快照，记录来源哈希、SQLite schema 与数量，再用本地 SQLite FTS5/BM25 提供可追溯的搜索结果。

项目目标是“可审计的本地文献证据工具 + 八个只读 MCP 工具”，不是通用聊天软件。当前已完成历史阶段一至阶段四；下一轮产品化阶段 0 已冻结合同，阶段 1–9 尚未实现。历史阶段四提供的是源码项目里的 Apple Silicon macOS 开发版 `.command` 入口，不是自包含、已签名或已公证的 App/DMG/pkg。

## 当前已实现：阶段一至阶段四

- 显式导入 UTF-8 Markdown 和带文本层 PDF。
- 每次 `build` 都新建快照；不覆盖或修改旧快照。
- 快照自带冻结源副本、`evidence.sqlite`、`schema.sql` 和 `manifest.json`。
- `manifest.json` 记录源文件 SHA-256、数据库 SHA-256、schema 哈希、软件版本和 document/asset/chunk/FTS 数量。
- PDF 结果保留页码 anchor；Markdown 结果保留标题路径和行号 anchor。
- `fulltext_verified` 与 `formula_verified` 默认均为 `false`。成功提取文本不会自动提升核验状态。
- 本地 BM25 预览严格返回真实空结果：`found=false, results=[]`。
- 搜索前重新核验快照，并用 SQLite `mode=ro&immutable=1`、`query_only` 和 authorizer 保持只读。
- 演示资料完全合成，来源记录在 `demo/provenance.json`。
- 固定资料库的 `127.0.0.1` Starlette 管理页：状态、显式选择文件并构建、快照列表/核验和本地搜索。
- HTTP 只接收受控 `snapshot_id`，不接收 library、快照、源文件或其他任意本机路径。
- 严格 Host/Origin、随机签名会话和 CSRF 防护；没有 CORS，静态 HTML/CSS/JS 全部随包安装。
- 上传文件先进入一次性受控临时目录，无论构建成功或失败都清理；每次构建仍只新增快照。
- 一个独立的 `literature-evidence-mcp` 命令通过本地标准输入/输出（stdio）暴露恰好八个 closed-world 只读工具；closed-world 表示只读取启动时固定的 library 中、调用明确指定的快照。
- MCP 工具只接受受控 `snapshot_id`、`document_id`、`chunk_id`、`section_id`、查询和数量上限，不接受 library、快照、SQLite 或源文件路径，也不接受任意 URI。
- 每次 MCP 内容调用都重新执行现有快照哈希、schema、数量和语义核验，再在 `query_only` 内存 SQLite 镜像上安装只读 authorizer；不会写缓存、日志或 SQLite sidecar。
- `search_documents` 与 `find_in_document` 只使用现有 SQLite FTS5/BM25，真实无命中返回 `found=false, results=[]`，不会强行补 top-k。
- Finder 可双击的 [`启动文献证据管理页.command`](./启动文献证据管理页.command)：从自身位置确定完整项目，路径含空格也可使用。
- 首次运行先核对 macOS、arm64、Python 3.11+、SQLite `serialize/deserialize` 和 FTS5，再在项目 `.venv/` 中做非 editable 安装。
- 双击入口使用稳定的用户资料库目录，端口绑定成功后才打开默认浏览器；终端前台运行，`Ctrl+C` 即停止。

## 读写边界

`build` 是用户主动执行的本地写操作：CLI 读取用户逐个列出的源文件；管理页只接收浏览器明确选择的文件字节。二者都只在固定 library 中新增快照，不修改源文件或旧快照。

`status`、`list`、`verify`、`search` 与八个 MCP 工具是只读操作：它们不会创建 library、重建数据库、写回快照或创建 SQLite sidecar。MCP 不暴露导入、重建、激活、删除、监控、迁移或任意文件读取能力。完整边界见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

用户导入的 PDF/Markdown、快照、数据库和运行态不属于项目代码，也不应提交到公开仓库。`.gitignore` 默认排除示例使用的 `local-library/`、常见 `library/runtime/snapshots` 目录、SQLite、虚拟环境、密钥和缓存；如果把 `--library` 指向仓库内的其他自定义目录，需要自行加入 ignore，或者把资料库放到仓库外。

## Apple Silicon macOS 最短流程

1. 保留完整项目文件夹，不要只单独移动 `.command` 文件。
2. 在 Finder 双击 [`启动文献证据管理页.command`](./启动文献证据管理页.command)。如果 macOS 首次拦截未签名开发脚本，可在 Finder 中右键它并选“打开”；不需要关闭系统安全功能。
3. 首次等待环境准备完成。端口 `8765` 成功绑定后，默认浏览器会打开 `http://127.0.0.1:8765/`。
4. 保持 Terminal（终端）窗口打开。用完后在该窗口按 `Ctrl+C`，服务停止且端口随即释放。

入口只支持 Apple Silicon macOS，需要 Python 3.11 或更新版本，且该 Python 的 `sqlite3` 必须能真实使用 `serialize/deserialize` 和 FTS5（Full-Text Search 5，SQLite 全文检索模块）。不满足时入口会给出中文提示；可从 [Python.org 的 macOS 下载页](https://www.python.org/downloads/macos/) 获取合适安装包，然后重新双击。入口不使用 `sudo`，不自动安装 Homebrew，不执行 `curl` 管道脚本，也不修改 `PATH` 或 shell 配置文件。

长期运行与资料数据主要使用两个明确目录：

- `<完整项目>/.venv/`：安装当前本地项目和免费开源依赖的受控虚拟环境；已被 `.gitignore` 排除。
- `~/Library/Application Support/literature-evidence-mcp/library/`：默认用户资料库；位于项目外，不会被提交 Git。第一次成功准备时建立目录，只有用户在管理页明确选文件并点击构建后，才会在其中新增快照。

预检和创建 `.venv` 本身只是本机操作。首次安装可能访问默认的 [PyPI](https://pypi.org/) 软件包索引，下载 PEP 517 构建所需的 `setuptools` 以及项目声明的免费开源直接/传递依赖。为防止安装位置被外部配置改到 `.venv` 之外，入口会忽略用户的 `PIP_*`、`pip.conf`、`PYTHONPATH`、`PYTHONHOME` 和当前激活的虚拟环境，要求 `.venv` 明确禁用系统 site-packages，并把安装前缀固定为项目 `.venv`；因此不会使用用户配置的私有索引、其中的凭据或项目外 Python 包。这些对外请求只用于依赖解析和软件包下载；不发送文献或资料库内容，不主动请求 Keychain（钥匙串）凭据，不下载模型，不调用云模型或付费 API。pip 缓存已禁用；pip 可能对暂时网络失败做有限自动重试，不会由启动器另加循环或无限重试。本地构建可能在项目中生成已被 `.gitignore` 排除的 `build/` 和 `src/literature_evidence_mcp.egg-info/`；它们是包构建产物，不含文献或资料库。当本地项目内容没变且环境完整时，后续双击不再运行 pip、不检查更新；管理页只监听 `127.0.0.1`。

## 管理页与 MCP 的区别

双击入口只启动本机管理页。管理页用于用户明确选择 PDF/Markdown、新增冻结快照、核验和本地搜索；“构建全新快照”是明确写操作。MCP 则是给支持本地 stdio（标准输入/输出）的 MCP host 调用的八个只读工具。双击入口不会自动启动 MCP，也不会修改任何 Codex、ChatGPT 或 Claude 配置。

需要 MCP 时，由用户在支持 stdio command/args 的 host 中手工设置下列等价命令；它只固定 library，没有 host、port、URL 或其他 transport 参数：

```bash
"<完整项目>/.venv/bin/python" -m literature_evidence_mcp.mcp_server \
  --library "$HOME/Library/Application Support/literature-evidence-mcp/library"
```

`literature-evidence-mcp --library <library>` 这个原有 console command 仍保留。上面用 `python -m` 写法，是为了让虚拟环境的项目路径含空格时也能稳定启动。MCP 会在前台等待 host 通过 stdin/stdout 发送协议消息，所以直接运行时没有普通交互提示。stdout 只用于 MCP 协议；可操作启动错误写入 stderr，且不回显内部路径或 traceback。host 每次调用都必须提供目标 `snapshot_id`，服务不会暗中选择“最新”或“当前”快照。

只安装 wheel 不会得到 Finder `.command`；这个双击入口属于完整源码项目的开发版外层。wheel 仍包含两个正式 Python CLI 入口和管理页静态资源。

### 八个 MCP 工具

所有 ID 都必须属于同一个调用指定的快照；错误类型、多余参数、超限、重复批量 ID 或跨文档/跨快照错配会返回 `isError=true`。成功结果是一个 JSON 对象，编码在单个 MCP `TextContent` 中。

| 工具 | 参数 | 成功返回语义 |
|---|---|---|
| `search_documents` | `snapshot_id`；`query` 1–400 字符；`top_k` 1–10（默认 5）；`excerpt_chars` 1–1200（默认 1000） | `found`、`results`、`snapshot_id`、`retrieval_mode="bm25"`；每项含文档/资产/块 ID、标题、来源名、anchor、核验状态、score 和有界摘录 |
| `get_excerpt` | `snapshot_id`、`document_id`、`chunk_id`；`max_chars` 1–1200（默认 600） | 一个 `result` 及 `truncated`；先核验块确属该文档和快照 |
| `get_multiple_excerpts` | `snapshot_id`、`document_id`；1–5 个唯一 `chunk_ids`；`per_item_chars` 1–1200（默认 600） | 按请求 ID 顺序返回全部 `results`，每项含 `truncated`；任一 ID 缺失或错配时整次拒绝，不返回部分结果 |
| `get_document_metadata` | `snapshot_id`、`document_id` | 白名单化的文档元数据和资产 ID/提取状态/页数/chunk 数；不返回路径或内部 SHA |
| `get_document_toc` | `snapshot_id`、`document_id`；`max_items` 1–100（默认 100） | `items`、`truncated`；Markdown 按已索引标题路径、PDF 按页码派生导航，并明确不宣称是作者目录 |
| `read_document_section` | `snapshot_id`、`document_id`、由 TOC 返回的 `section_id`；`max_chars` 1–1200（默认 1200） | 章节描述、证据 `results`、`returned_chars` 和 `truncated`；所有摘录合计不超过上限 |
| `find_in_document` | `snapshot_id`、`document_id`、`query` 1–400；`top_k` 1–10（默认 5）；`excerpt_chars` 1–1200（默认 600） | 只在该文档内执行同一 FTS5/BM25 排序；返回真实命中或 `found=false, results=[]` |
| `retrieval_status` | `snapshot_id` | 重新核验后的 path-free 快照哈希、schema 版本、数量、SDK/服务版本、只读/closed-world 状态、未逐个核验的快照候选数和已知限制 |

工具 annotations 均为 `readOnlyHint=true`、`destructiveHint=false`、`openWorldHint=false`。这些 annotations 是给客户端的提示；真正的只读边界来自固定 library、严格逻辑 ID、完整快照核验、只读内存 SQLite、authorizer 和输出白名单。

## 命令行与贡献者用法

需要 Python 3.11 或更新版本，并要求该 Python 的 `sqlite3` 提供可用的 `serialize/deserialize` 和 FTS5。贡献者可手工建立 editable 环境：

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -e .
```

启动管理页（这条命令固定 library 和端口；没有 `--host` 参数）：

```bash
literature-evidence serve --library ./local-library --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。页面里的“上传”只把文件发送到这个本机回环地址，不是云上传。按 Ctrl+C 停止，端口随后释放。

贡献者安装测试 extra 后，可在仓库根目录运行全部测试：

```bash
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
```

自动验收可显式指定测试用 Python，但该变量不会写入 shell 配置，也不会修改 `PATH`：

```bash
LITERATURE_EVIDENCE_PYTHON=/absolute/path/to/python3.12 \
  ./启动文献证据管理页.command --preflight-only
```

`--preflight-only` 只读，不创建文件。自动验收还可用 `--prepare-only` 只准备环境和默认资料库，或用 `--no-browser` 启动服务但不打开浏览器；普通 Finder 双击不需要这些参数。

也可以继续使用阶段一 CLI。用合成 Markdown 建立新快照（这一步会在 `local-library/` 新增文件）：

```bash
literature-evidence build \
  --library ./local-library \
  ./demo/synthetic-evidence.md
```

命令会输出一段 JSON；其中 `snapshot_path` 字段是新快照的绝对路径。下面两步只读，把 `<snapshot-path>` 换成该字段的值：

```bash
literature-evidence verify <snapshot-path>
literature-evidence search <snapshot-path> "Shannon entropy"
```

## 管理页资源边界

- 每次最多 20 个文件；单文件最多 10 MiB；文件内容合计最多 20 MiB。
- multipart 请求体最多 21 MiB，其中 1 MiB 留给文件名、边界和请求头编码。
- 文件名最多 240 个 UTF-8 字节，且不得含路径分隔符、控制字符或同批冲突名。
- 搜索 JSON 最多 8 KiB；核心查询仍限制为 1–400 字符、`top_k` 1–10、摘录 1–1200 字符。

选择依据是 2026-08-31 在 Apple M5 / 16 GiB、macOS arm64、Python 3.12.14、SQLite 3.53.1 上对完全合成 Markdown 的最小测量：

| 合成输入 | 构建耗时 | 峰值 RSS | 冻结快照大小 |
|---|---:|---:|---:|
| 1 × 10 MiB | 0.80 s | 157 MiB | 30.0 MiB |
| 20 × 1 MiB | 1.57 s | 205 MiB | 60.3 MiB |
| 10 × 4 MiB | 3.07 s | 337 MiB | 97.9 MiB |

最终把总量限制在 20 MiB，而不是测过的 40 MiB。原因是实际 PDF 解压和文本层提取可能比合成 Markdown 使用更多内存；这些字节上限不是操作系统级内存沙箱。

## 运行依赖与来源

- [MCP Python SDK](https://pypi.org/project/mcp/) `>=2.1.1,<3`：官方 SDK 的低层 `Server` 与 stdio client/server 协议实现；MIT。阶段三已用稳定版 `2.1.1` 和 Python 3.12 验证；项目不安装 SDK 的额外 CLI。
- [pypdf](https://pypi.org/project/pypdf/) `>=6.0,<7`：本机读取 PDF 文本层；BSD-3-Clause。
- [Starlette](https://pypi.org/project/starlette/) `>=1.6,<2`：轻量 ASGI 应用层；BSD-3-Clause。
- [Uvicorn](https://pypi.org/project/uvicorn/) `>=0.52,<1`：只绑定本机回环地址的 ASGI 服务；BSD-3-Clause。
- [python-multipart](https://pypi.org/project/python-multipart/) `>=0.0.32,<1`：流式解析浏览器文件选择；Apache-2.0。
- [HTTPX2](https://pypi.org/project/httpx2/) `>=2.12,<3`：项目代码只在管理页测试中直接使用；MCP SDK 的传递依赖树也会安装它；BSD-3-Clause。

## 快照结构

```text
<library>/snapshots/<snapshot-id>/
├── manifest.json
├── schema.sql
├── evidence.sqlite
└── sources/
    └── <content-derived-document-id>.md-or-pdf
```

快照不会保存原始绝对路径，只保留源文件名和冻结副本。在相同项目、Python、SQLite 与 pypdf 版本下，相同输入再次构建会得到新的 snapshot ID 和目录，但 corpus hash、数据库内容、排序与搜索结果应保持一致；manifest 会记录这些版本，避免跨版本作不恰当比较。

## 历史实现基线

1. **阶段一（已完成）**：导入、冻结快照、哈希/schema/数量核验、SQLite BM25 与 CLI 预览。
2. **阶段二（已完成）**：本机 `127.0.0.1` Starlette 静态管理页；写操作只能由页面中的明确按钮触发。
3. **阶段三（已完成）**：八类 closed-world stdio 只读 MCP 能力；`search_documents` 与 `find_in_document` 只走本地 BM25，不含 combo、API Key 或 Tunnel。
4. **阶段四（已完成）**：macOS Apple Silicon 开发版 `.command` 入口、首次预检/受控安装、默认用户资料库和前台管理页启动。自包含、签名/公证 App、DMG 或 pkg 仍属范围外。

下一轮阶段 0–9 与上面的历史编号分开：阶段 0 只冻结多资料库、稳定 ID、完整冻结快照、库内增量复用、BM25/增强分离和八工具演进合同；阶段 1–9 必须依次验收，当前均未实现。完整顺序、每阶段最小验收和真实模型/钥匙串/Tunnel/ChatGPT 停止闸门见 [ARCHITECTURE.md](./ARCHITECTURE.md#下一轮产品化阶段-0-合同)。后续路线做到阶段 9 的本地实现与离线模拟即停止，不把模拟通过表述为真实链路通过。

OCR、Zotero、Windows/Linux、Intel Mac、自动更新、文件夹监控、完整聊天界面、自动删除/垃圾回收、App/DMG 打包、签名、公证和发布不在阶段 0–9 范围内。

## 已知限制

- SQLite `unicode61` 没有专门中文分词；连续无空格中文的词法召回可能较低。
- 管理页在连续中文空结果时只提示尝试加入空格，不会静默改写查询或改变 tokenizer。
- MCP 每次工具调用都重新核验快照并建立只读内存 SQLite 镜像；这是明确的完整性边界，也意味着大快照上的单次调用会有额外本地读取开销。当前不添加缓存。
- `get_document_toc` 的 Markdown 标题与 PDF 页码导航来自现有 chunk 索引，不等同于作者提供的正式目录；`section_id` 只对生成它的快照与文档有效。
- TOC 当前一次最多返回前 100 节且没有翻页参数；章节读取一次最多返回该节开头 1200 字符且没有续读游标。`truncated=true` 只诚实标记截断，不代表可由本工具继续翻页。
- MCP 成功结果当前用单个 JSON `TextContent` 返回，没有另行声明 output schema。
- pypdf 能提取文本不等于排版、全文或公式已经人工核验。
- 无文本层 PDF 会明确拒绝，当前不会静默 OCR。
- 一个导入文件暂视为一个文档；多资产合并和人工元数据编辑留待后续。
- 上传资源边界限制输入字节数，不隔离 pypdf 的进程内解析内存；特别复杂的合法 PDF 仍可能比合成测量更耗资源。

## 许可证与发布状态

旧项目源码没有可确认的公开许可证，本项目因此按行为规格 clean-room 重写，没有复制旧数据库、文献、向量、报告、运行态、凭据、虚拟环境、Tunnel 或 App 二进制。

除另有标注外，本项目原创代码、文档与合成演示资料采用 [Apache License 2.0](./LICENSE)。该许可证允许使用、修改和再分发（包括商业用途），但再分发时必须遵守许可证中的保留许可证、变更说明和相关归属信息等条件。

该许可证不替用户导入的 PDF/Markdown、由其生成的快照或其他第三方文献授予版权或再分发权，也不改变第三方依赖各自的许可证。项目当前为早期开发版，尚未发布稳定版本。
