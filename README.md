# literature-evidence-mcp

一个面向普通用户的本地文献证据工具：把用户明确选择的 Markdown 或带文本层 PDF 复制进新的冻结快照，记录来源哈希、SQLite schema 与数量，再用本地 SQLite FTS5/BM25 提供可追溯的搜索结果。

项目目标是“可审计的本地文献证据桌面应用 + 八个只读 MCP 工具”，不是通用聊天软件。当前已完成阶段一核心、阶段二本机管理页和阶段三本地 stdio MCP；还没有 App/DMG。

## 当前已实现：阶段一至阶段三

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

## 读写边界

`build` 是用户主动执行的本地写操作：CLI 读取用户逐个列出的源文件；管理页只接收浏览器明确选择的文件字节。二者都只在固定 library 中新增快照，不修改源文件或旧快照。

`status`、`list`、`verify`、`search` 与八个 MCP 工具是只读操作：它们不会创建 library、重建数据库、写回快照或创建 SQLite sidecar。MCP 不暴露导入、重建、激活、删除、监控、迁移或任意文件读取能力。完整边界见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

用户导入的 PDF/Markdown、快照、数据库和运行态不属于项目代码，也不应提交到公开仓库。`.gitignore` 默认排除示例使用的 `local-library/`、常见 `library/runtime/snapshots` 目录、SQLite、虚拟环境、密钥和缓存；如果把 `--library` 指向仓库内的其他自定义目录，需要自行加入 ignore，或者把资料库放到仓库外。

## 本地试用

需要 Python 3.11 或更新版本，并要求该 Python 的 `sqlite3` 提供 `serialize/deserialize`（标准 CPython 的常见构建通常具备；缺失时命令会明确报错，不会发布半成品快照）。依赖用于本地 PDF 文本层、管理页 HTTP/ASGI、multipart 文件解析和 MCP stdio 协议；运行时不调用网络模型、付费 API 或外部数据服务。MCP SDK 的依赖树包含其他标准 transport 组件，但本项目入口只启动 stdio。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

启动阶段三 MCP（这条命令只固定 library；没有 host、port、URL 或 transport 参数）：

```bash
literature-evidence-mcp --library ./local-library
```

它会在前台等待 MCP host 通过 stdin/stdout 发送协议消息，所以直接运行时没有普通交互提示。stdout 只用于 MCP 协议；可操作启动错误写入 stderr，且不回显内部路径或 traceback。host 每次调用都必须提供目标 `snapshot_id`，服务不会暗中选择“最新”或“当前”快照。

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
- [HTTPX2](https://pypi.org/project/httpx2/) `>=2.12,<3`：仅用于测试管理页，不是运行依赖；BSD-3-Clause。

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

## 最小实施顺序

1. **阶段一（已完成）**：导入、冻结快照、哈希/schema/数量核验、SQLite BM25 与 CLI 预览。
2. **阶段二（已完成）**：本机 `127.0.0.1` Starlette 静态管理页；写操作只能由页面中的明确按钮触发。
3. **阶段三（已完成）**：八类 closed-world stdio 只读 MCP 能力；`search_documents` 与 `find_in_document` 只走本地 BM25，不含 combo、API Key 或 Tunnel。
4. **阶段四**：macOS Apple Silicon 小白入口；自包含启动器和 App/DMG 再单独评估。

v0.1 暂不加入远程 combo、阿里云、Secure MCP Tunnel、OCR、Zotero、Windows/Linux、自动更新、完整聊天界面、Electron/Tauri/Flet。

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
