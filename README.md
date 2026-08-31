# literature-evidence-mcp

一个面向普通用户的本地文献证据工具：把用户明确选择的 Markdown 或带文本层 PDF 写入资料库自己的内容寻址对象区，生成完整冻结快照，记录来源哈希、SQLite schema 与数量，再用本地 SQLite FTS5/BM25 提供可追溯的搜索结果。

项目目标是“可审计的本地文献证据工具 + 八个只读 MCP 工具”，不是通用聊天软件。当前已完成历史阶段一至阶段四、下一轮产品化阶段 0 合同以及阶段 1–7；下一轮阶段 8–9 尚未实现。历史阶段四提供的是源码项目里的 Apple Silicon macOS 开发版 `.command` 入口，不是自包含、已签名或已公证的 App/DMG/pkg。

## 当前已实现：历史阶段一至阶段四与产品化阶段 1–7

- 显式导入 UTF-8 Markdown 和带文本层 PDF。
- 每次 `build` 都新建快照；不覆盖或修改旧快照。
- 每库独立保存 raw source、规范化 parsed text 和 chunks 三层不可变对象；不同库不共享对象。
- 快照自带独立 `evidence.sqlite`、`schema.sql` 和完整 `manifest.json`；不依赖基础快照解释成员。
- `manifest.json` 记录三层对象引用、源文件/数据库/schema 哈希、软件版本、document/asset/chunk/FTS 数量和真实新增/复用对象统计。
- PDF 结果保留页码 anchor；Markdown 结果保留标题路径和行号 anchor。
- `fulltext_verified` 与 `formula_verified` 默认均为 `false`。成功提取文本不会自动提升核验状态。
- 本地 BM25 预览严格返回真实空结果：`found=false, results=[]`。
- 搜索前重新核验快照，并用 SQLite `mode=ro&immutable=1`、`query_only` 和 authorizer 保持只读。
- 演示资料完全合成，来源记录在 `demo/provenance.json`。
- 固定应用根的 `127.0.0.1` Starlette 管理页：创建/切换资料库、拖放或选择文件、空白/继承构建、逐文件状态、差异/复用统计、快照核验/激活和本地搜索。
- HTTP 内容动作只接收受控 `library_id + snapshot_id`，构建还必须明确选择 `blank` 或严格 `base_snapshot_id` 的 `inherit`；不接收资料库、快照、源文件或其他任意本机路径。
- 严格 Host/Origin、随机签名会话和 CSRF 防护；没有 CORS，静态 HTML/CSS/JS 全部随包安装。
- 上传文件先进入一次性受控临时目录，无论构建成功或失败都清理；每次构建仍只新增快照。
- 每个资料库根持久保存独立 `snapshot-catalog.json`，把每个成功 `snapshot_id` 绑定到发布时的 manifest 哈希，并分别记录“当前快照”和“上次成功快照”。首次成功同时成为二者；后续成功只推进上次成功，当前只能由本地用户显式激活。
- 从明确 `base_snapshot_id` 构建时可继承完整成员，并显式新增、替换或移除文件；不变 raw/parsed/chunks 对象在同库复用，新快照仍生成独立 BM25 SQLite。
- parser 身份包含上游 raw 摘要、实现版本和精确配置；chunker 身份包含完整 parsed 摘要、实现版本和精确配置。配置变化只让对应层及其下游失效。
- 构建返回对象引用、新增/复用对象、逻辑对象字节和实际新增/复用 payload 字节；本阶段不删除孤立或旧对象。
- 一个独立的 `literature-evidence-mcp` 命令通过本地标准输入/输出（stdio）暴露恰好八个只读工具；服务固定应用注册表，而不是固定或暗选一个资料库。
- 七个内容工具都必须提供受控 `library_id + snapshot_id`，再提供适用的 `document_id`、`chunk_id`、`section_id`、查询和数量上限；工具不接受资料库、快照、SQLite 或源文件路径，也不接受任意 URI。
- 第八个 `retrieval_status` 无 ID 时列库、只给 `library_id` 时列该库成功快照、同时给两级 ID 时核验具体快照；只给 `snapshot_id` 明确拒绝。
- 每次 MCP 内容调用都重新执行现有快照哈希、schema、数量和语义核验，再在 `query_only` 内存 SQLite 镜像上安装只读 authorizer；不会写缓存、日志或 SQLite sidecar。
- `find_in_document` 只使用现有 SQLite FTS5/BM25；`search_documents` 省略 mode 或显式 `mode="bm25"` 时仍走同一完整本地路径。真实无命中返回 `found=false, results=[]`，不会强行补 top-k。
- 显式 `mode="enhanced"` 可使用构造参数注入的三角色链：原问题改写一次、改写问题查询向量一次、所选快照的有界候选重排一次。三次串行、零自动重试、首错停止，且不以 BM25 暗中补候选或回退。
- 增强链在第一次 transport 前核验明确 `library_id + snapshot_id`、完整快照和精确阶段 4 vector profile/artifact；结果只从该冻结快照投影，并返回不含正文、路径或秘密的调用审计。
- 运行时代码只定义 provider-neutral transport 合同，以及构造注入的非秘密 provider、三个 model ID、精确 profile 和候选/字符边界；没有任何真实供应商 HTTP adapter、密钥读取或真实模型 ID 配置。生产 Web/stdio 入口未注入 transport，显式 enhanced 会明确返回“尚未配置或不可用”。
- 独立 Stage 7 入口用一次性临时应用根生成两库、三快照和中英/双向跨语言合成题集，实际运行本地 BM25，并让增强模式完整经过 Stage 6 的三调用 recording fake；不加入 Web 或八个 MCP 工具。
- Stage 7 JSON 报告按模式记录 hit@k、recall@k、MRR、真实空准确率、库/快照隔离违规和 anchor/页码可追溯覆盖，并固定标为 `offline_simulated`、真实模型调用 0、网络调用 0。它只验收离线管线，不能推出真实模型质量提升。
- Finder 可双击的 [`启动文献证据管理页.command`](./启动文献证据管理页.command)：从自身位置确定完整项目，路径含空格也可使用。
- 首次运行先核对 macOS、arm64、Python 3.11+、SQLite `serialize/deserialize` 和 FTS5，再在项目 `.venv/` 中做非 editable 安装。
- 双击入口使用稳定的用户应用根，端口绑定成功后才打开默认浏览器；终端前台运行，`Ctrl+C` 即停止。
- 固定应用根中的多资料库注册表：每个库使用随机、稳定、与名称和路径无关的 `library_id`，物理数据位于相互隔离的 `libraries/<library_id>/`。
- 本地 `libraries` CLI 可显式创建、列出、选择、重命名和修改描述；允许同名显示名称，重命名、修改描述或重启不会改变 ID。

## 读写边界

`build` 是用户主动执行的本地写操作：CLI 读取用户逐个列出的源文件；管理页只接收浏览器明确选择的文件字节，并在每次构建中显式携带目标 `library_id`。二者都只在目标 library 中新增快照，不修改源文件或旧快照。`snapshots activate` 只接受同库、已成功发布且重新核验通过的快照。管理页中的 create/select/build/activate 以及 CLI 的写命令都要求明确动作；列表、状态、核验和搜索保持只读。

`status`、`list`、`verify`、`search` 与八个 MCP 工具是只读操作：它们不会创建 library、重建数据库、写回快照或创建 SQLite sidecar。MCP 不暴露导入、重建、激活、删除、监控、迁移或任意文件读取能力。完整边界见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

用户导入的 PDF/Markdown、快照、数据库和运行态不属于项目代码，也不应提交到公开仓库。`.gitignore` 默认排除示例使用的 `local-library/`、常见 `library/runtime/snapshots` 目录、SQLite、虚拟环境、密钥和缓存；如果把 `--library` 指向仓库内的其他自定义目录，需要自行加入 ignore，或者把资料库放到仓库外。

## Apple Silicon macOS 最短流程

1. 保留完整项目文件夹，不要只单独移动 `.command` 文件。
2. 在 Finder 双击 [`启动文献证据管理页.command`](./启动文献证据管理页.command)。如果 macOS 首次拦截未签名开发脚本，可在 Finder 中右键它并选“打开”；不需要关闭系统安全功能。
3. 首次等待环境准备完成。端口 `8765` 成功绑定后，默认浏览器会打开 `http://127.0.0.1:8765/`。
4. 保持 Terminal（终端）窗口打开。用完后在该窗口按 `Ctrl+C`，服务停止且端口随即释放。

入口只支持 Apple Silicon macOS，需要 Python 3.11 或更新版本，且该 Python 的 `sqlite3` 必须能真实使用 `serialize/deserialize` 和 FTS5（Full-Text Search 5，SQLite 全文检索模块）。不满足时入口会给出中文提示；可从 [Python.org 的 macOS 下载页](https://www.python.org/downloads/macos/) 获取合适安装包，然后重新双击。入口不使用 `sudo`，不自动安装 Homebrew，不执行 `curl` 管道脚本，也不修改 `PATH` 或 shell 配置文件。

长期运行与资料数据使用以下明确目录：

- `<完整项目>/.venv/`：安装当前本地项目和免费开源依赖的受控虚拟环境；已被 `.gitignore` 排除。
- `~/Library/Application Support/literature-evidence-mcp/`：管理页、MCP 和注册表共同使用的固定应用根；位于项目外，不会被提交 Git。
- 应用根内的 `registry.json` 与 `libraries/<library_id>/`：持久化注册表和物理隔离多库。每库根另有 `snapshot-catalog.json`、目录册写锁、独立 `objects/` 和 `snapshots/`；全局注册表不保存任意路径。管理页和 MCP 的每次内容动作都要求两级 ID，不采用注册表中的 selected、current 或 last 作为隐式内容目标。

预检和创建 `.venv` 本身只是本机操作。首次安装可能访问默认的 [PyPI](https://pypi.org/) 软件包索引，下载 PEP 517 构建所需的 `setuptools` 以及项目声明的免费开源直接/传递依赖。为防止安装位置被外部配置改到 `.venv` 之外，入口会忽略用户的 `PIP_*`、`pip.conf`、`PYTHONPATH`、`PYTHONHOME` 和当前激活的虚拟环境，要求 `.venv` 明确禁用系统 site-packages，并把安装前缀固定为项目 `.venv`；因此不会使用用户配置的私有索引、其中的凭据或项目外 Python 包。这些对外请求只用于依赖解析和软件包下载；不发送文献或资料库内容，不主动请求 Keychain（钥匙串）凭据，不下载模型，不调用云模型或付费 API。pip 缓存已禁用；pip 可能对暂时网络失败做有限自动重试，不会由启动器另加循环或无限重试。本地构建可能在项目中生成已被 `.gitignore` 排除的 `build/` 和 `src/literature_evidence_mcp.egg-info/`；它们是包构建产物，不含文献或资料库。当本地项目内容没变且环境完整时，后续双击不再运行 pip、不检查更新；管理页只监听 `127.0.0.1`。

## 管理页与 MCP 的区别

双击入口只启动本机管理页。管理页用于用户明确创建/切换资料库、选择 PDF/Markdown、从空白或明确基础快照新增冻结快照、核验/激活和本地搜索；create/select/build/activate 都是明确写操作。MCP 则是给支持本地 stdio（标准输入/输出）的 MCP host 调用的八个只读工具。双击入口不会自动启动 MCP，也不会修改任何 Codex、ChatGPT 或 Claude 配置。

需要 MCP 时，由用户在支持 stdio command/args 的 host 中手工设置下列等价命令；它固定应用注册表根，没有 host、port、URL 或其他 transport 参数：

```bash
"<完整项目>/.venv/bin/python" -m literature_evidence_mcp.mcp_server \
  --application-root "$HOME/Library/Application Support/literature-evidence-mcp"
```

等价 console command 是 `literature-evidence-mcp --application-root <application-root>`；省略参数时使用当前用户的标准 Application Support 目录。上面用 `python -m` 写法，是为了让虚拟环境的项目路径含空格时也能稳定启动。MCP 会在前台等待 host 通过 stdin/stdout 发送协议消息，所以直接运行时没有普通交互提示。stdout 只用于 MCP 协议；可操作启动错误写入 stderr，且不回显内部路径或 traceback。七个内容工具每次都必须显式提供目标 `library_id` 与 `snapshot_id`，服务不会暗中选择注册表 selected、当前或最新快照。

只安装 wheel 不会得到 Finder `.command`；这个双击入口属于完整源码项目的开发版外层。wheel 仍包含两个正式 Python CLI 入口和管理页静态资源。

### 八个 MCP 工具

所有下级 ID 都必须属于同一个调用指定的 `(library_id, snapshot_id)`；错误类型、多余参数、超限、重复批量 ID 或跨库/跨快照/跨文档错配会返回 `isError=true`。成功结果是一个 JSON 对象，编码在单个 MCP `TextContent` 中。

| 工具 | 参数 | 成功返回语义 |
|---|---|---|
| `search_documents` | `library_id`、`snapshot_id`；`query` 1–400 字符；`mode="bm25"|"enhanced"`（默认 BM25）；`top_k` 1–10（默认 5）；`excerpt_chars` 1–1200（默认 1000） | BM25 保持离线原语义；显式增强仅在注入 transport 时执行固定三角色链，并返回实际调用审计；两种模式都只返回所选快照的有界证据 |
| `get_excerpt` | `library_id`、`snapshot_id`、`document_id`、`chunk_id`；`max_chars` 1–1200（默认 600） | 一个 `result` 及 `truncated`；先核验块确属该库、快照和文档 |
| `get_multiple_excerpts` | `library_id`、`snapshot_id`、`document_id`；1–5 个唯一 `chunk_ids`；`per_item_chars` 1–1200（默认 600） | 按请求 ID 顺序返回全部 `results`，每项含 `truncated`；任一 ID 缺失或错配时整次拒绝，不返回部分结果 |
| `get_document_metadata` | `library_id`、`snapshot_id`、`document_id` | 白名单化的文档元数据和资产 ID/提取状态/页数/chunk 数；不返回路径或内部 SHA |
| `get_document_toc` | `library_id`、`snapshot_id`、`document_id`；`max_items` 1–100（默认 100） | `items`、`truncated`；Markdown 按已索引标题路径、PDF 按页码派生导航，并明确不宣称是作者目录 |
| `read_document_section` | `library_id`、`snapshot_id`、`document_id`、由 TOC 返回的 `section_id`；`max_chars` 1–1200（默认 1200） | 章节描述、证据 `results`、`returned_chars` 和 `truncated`；所有摘录合计不超过上限 |
| `find_in_document` | `library_id`、`snapshot_id`、`document_id`、`query` 1–400；`top_k` 1–10（默认 5）；`excerpt_chars` 1–1200（默认 600） | 只在该文档内执行同一 FTS5/BM25 排序；返回真实命中或 `found=false, results=[]` |
| `retrieval_status` | 可选 `library_id`、`snapshot_id`，但禁止只给 `snapshot_id` | 无 ID 列库；仅库 ID 列成功快照及 current/last；两级 ID 重新核验具体快照并返回 path-free 身份、数量和状态 |

工具 annotations 均为 `readOnlyHint=true`、`destructiveHint=false`。因为 `search_documents` 承载显式 enhanced 的潜在外部调用，它的 `openWorldHint=true`；其余七个工具仍为 `false`。这些 annotations 是给客户端的提示；真正的只读边界来自固定应用注册表、两级目录册归属、完整快照核验、只读内存 SQLite、authorizer 和输出白名单。

## 命令行与贡献者用法

需要 Python 3.11 或更新版本，并要求该 Python 的 `sqlite3` 提供可用的 `serialize/deserialize` 和 FTS5。贡献者可手工建立 editable 环境：

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -e .
```

管理多个本地资料库（这些命令固定使用用户 Application Support 下的受控应用根；不接受任意 `--app-root`）：

```bash
literature-evidence libraries create "研究资料" --description "本地合成测试与公开文献"
literature-evidence libraries list
literature-evidence libraries rename <library_id> "新名称"
literature-evidence libraries describe <library_id> "新描述"
literature-evidence libraries select <library_id>
```

创建结果和 CLI 列表会返回本地 `library_root`，可继续显式传给核心 `build --library`。注册表的资料库选择只持久化本地产品状态；HTTP/MCP 的内容动作都不会隐式采用它。

启动管理页（这条命令固定 application root 和端口；没有 `--host` 参数）：

```bash
literature-evidence serve \
  --application-root "$HOME/Library/Application Support/literature-evidence-mcp" \
  --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。页面里的“上传”只把文件发送到这个本机回环地址，不是云上传。按 Ctrl+C 停止，端口随后释放。

贡献者安装测试 extra 后，可在仓库根目录运行全部测试：

```bash
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
```

运行 Stage 7 合成离线搜索质量验收（JSON 报告写到标准输出，成功退出码为 0，验收失败为非 0）：

```bash
PYTHONPATH=src python -m literature_evidence_mcp.quality_eval
```

该命令只在系统临时目录建立可分享的合成 Markdown/PDF、资料库、快照和离线向量，结束时自动清理；不会读取或写入用户真实应用根、仓库业务数据、密钥或网络。报告中的 simulated enhanced 命中由逐题脚本 fake 驱动，只能说明三调用、隔离、真实空和引用边界按合同工作。每题同时记录请求选择与实际返回的库/快照身份，以及目标 chunk 在冻结数据库中的精确 anchor、页码或行号；身份缺失/错配、定位字段变化或检索异常都会使验收失败，异常不会冒充真实空。逐题 `case_pass` 表示合同完成或已知 BM25 局限被如实记录，不表示预期文档一定命中；质量观测要看分模式的 positive hit@k 与 MRR。报告内的“小白说明”解释这些区别。

自动验收可显式指定测试用 Python，但该变量不会写入 shell 配置，也不会修改 `PATH`：

```bash
LITERATURE_EVIDENCE_PYTHON=/absolute/path/to/python3.12 \
  ./启动文献证据管理页.command --preflight-only
```

`--preflight-only` 只读，不创建文件。自动验收还可用 `--prepare-only` 只准备环境和空的多资料库应用根，或用 `--no-browser` 启动服务但不打开浏览器；普通 Finder 双击不需要这些参数。

也可以继续使用阶段一 CLI。用合成 Markdown 建立新快照（这一步会在 `local-library/` 新增文件）：

```bash
literature-evidence build \
  --library ./local-library \
  ./demo/synthetic-evidence.md
```

从明确基础快照继承完整成员并新增、替换或移除文件时，使用 `--base-snapshot-id`、`--replace <document_id> <file>` 与 `--remove-document-id <document_id>`。后续成功构建不会自动切换当前快照；显式查看或激活使用：

```bash
literature-evidence snapshots list --library ./local-library
literature-evidence snapshots activate --library ./local-library <snapshot_id>
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

资源上限的选择依据是 2026-08-31 在 Apple M5 / 16 GiB、macOS arm64、Python 3.12.14、SQLite 3.53.1 上对完全合成 Markdown 的阶段 2 历史测量；表中容量不是阶段 3 对象库的当前基准：

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
<library>/
├── snapshot-catalog.json
├── .snapshot-catalog.lock
├── objects/
│   ├── source/<sha256>/payload
│   ├── parsed/<sha256>/payload
│   └── chunks/<sha256>/payload
└── snapshots/<snapshot-id>/
    ├── manifest.json
    ├── schema.sql
    └── evidence.sqlite
```

快照不会保存原始绝对路径，只保留源文件名和本库对象引用。每个 manifest 都列出完整成员，不需要沿 `base_snapshot_id` 读取历史链；每个快照的 BM25 SQLite 仍独立冻结。在相同 parser/chunker 配置、Python、SQLite 与 pypdf 版本下，相同输入再次构建会得到新的 snapshot ID 和目录，但 corpus hash、数据库内容、排序与搜索结果应保持一致。

## 历史实现基线

1. **阶段一（已完成）**：导入、冻结快照、哈希/schema/数量核验、SQLite BM25 与 CLI 预览。
2. **阶段二（已完成）**：本机 `127.0.0.1` Starlette 静态管理页；写操作只能由页面中的明确按钮触发。
3. **阶段三（已完成）**：八类 closed-world stdio 只读 MCP 能力；`search_documents` 与 `find_in_document` 只走本地 BM25，不含 combo、API Key 或 Tunnel。
4. **阶段四（已完成）**：macOS Apple Silicon 开发版 `.command` 入口、首次预检/受控安装、标准用户应用根和前台管理页启动。自包含、签名/公证 App、DMG 或 pkg 仍属范围外。

下一轮阶段 0–9 与上面的历史编号分开：阶段 0 冻结多资料库、稳定 ID、完整冻结快照、库内增量复用、BM25/增强分离和八工具演进合同；阶段 1–7 已依次实现多资料库核心、双级快照生命周期、库内对象复用、离线同配置向量复用、小白多资料库导入界面、离线可替换三角色增强链和合成离线搜索质量验收，阶段 8–9 尚未实现。阶段 6–7 都没有真实 provider adapter、真实模型/密钥、真实 Keychain 或真实链路质量结论。完整顺序、每阶段最小验收和真实模型/钥匙串/Tunnel/ChatGPT 停止闸门见 [ARCHITECTURE.md](./ARCHITECTURE.md#下一轮产品化阶段-0-合同)。后续路线做到阶段 9 的本地实现与离线模拟即停止，不把模拟通过表述为真实链路通过。

OCR、Zotero、Windows/Linux、Intel Mac、自动更新、文件夹监控、完整聊天界面、自动删除/垃圾回收、App/DMG 打包、签名、公证和发布不在阶段 0–9 范围内。

## 已知限制

- SQLite `unicode61` 没有专门中文分词；连续无空格中文的词法召回可能较低。
- 管理页在连续中文空结果时只提示尝试加入空格，不会静默改写查询或改变 tokenizer。
- MCP 每次工具调用都重新核验快照并建立只读内存 SQLite 镜像；这是明确的完整性边界，也意味着大快照上的单次调用会有额外本地读取开销。当前不添加缓存。
- `get_document_toc` 的 Markdown 标题与 PDF 页码导航来自现有 chunk 索引，不等同于作者提供的正式目录；`section_id` 只对生成它的资料库、快照与文档有效。
- TOC 当前一次最多返回前 100 节且没有翻页参数；章节读取一次最多返回该节开头 1200 字符且没有续读游标。`truncated=true` 只诚实标记截断，不代表可由本工具继续翻页。
- MCP 成功结果当前用单个 JSON `TextContent` 返回，没有另行声明 output schema。
- 产品化阶段 2 不迁移目录册出现前的旧快照；目录册缺失而检测到既有快照时会明确停止，既不改写也不覆盖。
- 管理页阶段 5 暂不暴露替换/移除控件；核心和 CLI 仍可在明确 `base_snapshot_id` 下使用现有替换/移除能力。管理页只支持从空白建立，或继承完整基础快照后新增文件。
- 产品化阶段 3 不迁移阶段 2 的快照内 `sources/` 布局；对象库缺失、对象损坏或摘要错配都会明确失败，不静默重建。自动删除和 GC 仍不存在。
- pypdf 能提取文本不等于排版、全文或公式已经人工核验。
- 无文本层 PDF 会明确拒绝，当前不会静默 OCR。
- 一个导入文件暂视为一个文档；多资产合并和人工元数据编辑留待后续。
- 上传资源边界限制输入字节数，不隔离 pypdf 的进程内解析内存；特别复杂的合法 PDF 仍可能比合成测量更耗资源。

## 许可证与发布状态

旧项目源码没有可确认的公开许可证，本项目因此按行为规格 clean-room 重写，没有复制旧数据库、文献、向量、报告、运行态、凭据、虚拟环境、Tunnel 或 App 二进制。

除另有标注外，本项目原创代码、文档与合成演示资料采用 [Apache License 2.0](./LICENSE)。该许可证允许使用、修改和再分发（包括商业用途），但再分发时必须遵守许可证中的保留许可证、变更说明和相关归属信息等条件。

该许可证不替用户导入的 PDF/Markdown、由其生成的快照或其他第三方文献授予版权或再分发权，也不改变第三方依赖各自的许可证。项目当前为早期开发版，尚未发布稳定版本。
