# 最小架构合同

本合同前半部分记录 `0.1.0.dev0` 历史阶段一至阶段三的已实现边界。阶段二在阶段一
`build_snapshot` / `verify_snapshot` / `search_snapshot` 核心之外增加固定资料库应用层和本机管理页；阶段三再增加八个本地 stdio、closed-world、只读 MCP 工具。历史阶段四只增加 macOS 开发版启动入口，没有改变快照格式、SQLite schema、分块规则或 BM25 算法。文末“下一轮产品化阶段 0 合同”冻结后续阶段 1–9 的目标；阶段 1–6 已实现，阶段 7–9 尚未实现。前半部分的固定单库 HTTP/MCP schema 只保留为历史阶段记录；当前运行合同以产品化“阶段 2–6 实现”小节为准。

## 信任边界

- Uvicorn 只绑定 IPv4 回环地址 `127.0.0.1`。浏览器入口固定为
  `http://127.0.0.1:<port>`，没有可配置的 `--host`。
- 启动命令只固定一次 `library` 根。HTTP 请求不能提供或更改 library 路径，也不能提供快照路径或任意本机源路径。
- 浏览器必须由用户明确选择 `.md`、`.markdown` 或 `.pdf`。所谓“上传”只是浏览器把字节发送到 `127.0.0.1` 的本机进程；不是云上传。
- 后端保留原文件名，把字节复制到一次性受控临时目录，再调用阶段一
  `build_snapshot`。不安全文件名、符号链接语义、错误类型和资源超限均拒绝；成功和失败都清理临时目录。
- 只读应用层只枚举 `<library>/snapshots` 的直接子目录，只接受阶段一生成格式的 `snapshot_id`，不跟随 library、snapshots 或候选快照符号链接。
- MCP 进程由 `literature-evidence-mcp --library <固定根>` 启动。命令没有 host、port、URL 或 transport 选项；服务只调用 SDK 的 stdio transport，不监听 TCP、UDP 或 Unix socket。
- MCP 请求不能提供或改变 library 根，也不能提供快照目录、SQLite 路径、源文件路径或任意 URI。每次调用必须明确提供受格式约束的 `snapshot_id`，服务没有 active/latest 快照概念。

## 浏览器安全合同

- `Host` 必须精确等于 `127.0.0.1:<port>`；请求一旦带 `Origin`，它必须精确同源。所有 POST 必须带同源 Origin。
- 进程启动时生成随机密钥。浏览器会话 Cookie 名绑定已验证端口，值是随机 session ID 加 HMAC 签名，设置 `HttpOnly`、`SameSite=Strict`、`Path=/`；CSRF token 使用不同域的 HMAC，并以常量时间比较。
- 项目不配置 CORS，也不返回 `Access-Control-Allow-*`。CSP 只允许同源脚本、样式和连接，并禁止 frame、object、base 与表单导航。
- 请求体先在 ASGI 接收层计数，再在文件复制层复核。任何错误只返回中文可操作信息，不返回 traceback、临时路径或固定 library 的绝对路径。
- Uvicorn 不信任代理头、不写访问日志、只运行一个 worker。前台进程由 Ctrl+C 停止；停止后必须释放端口。

## 本机管理页动作与端点

| 动作 | 端点 | 文件系统语义 |
|---|---|---|
| 状态 | `GET /api/status` | 只读；签发内存会话/响应 Cookie，只安全计数候选目录，不创建 library 或核验快照内容 |
| 列表 | `GET /api/snapshots` | 只读；直接枚举并逐个核验安全候选 |
| 核验 | `GET /api/snapshots/{snapshot_id}/verify` | 只读；只接收受控 ID |
| 搜索 | `POST /api/search` | 只读；核验后只在内存 SQLite 镜像上执行本地 BM25 |
| 构建 | `POST /api/build` | 唯一写动作；必须有会话、CSRF 和明确 build intent |

`POST /api/search` 使用 POST 只是为了不把查询放入 URL；它仍是只读动作。
除 build 外，没有端点会创建目录、写缓存或产生 SQLite sidecar。

## 明确不存在的能力

阶段三仍没有删除、覆盖、激活、监控、自动导入、任意文件读取、任意路径、快照迁移、OCR、PDF 转 Markdown、embedding、reranker、远程 combo、HTTP/SSE MCP、云模型、API Key、Tunnel、Zotero、聊天界面、App/DMG、自动更新或付费调用。MCP 只新增下面八个本地只读工具。

## 历史阶段三只读 MCP 合同

启动命令：

```bash
literature-evidence-mcp --library <library-root>
```

stdio 的 stdout 只承载 MCP 协议。启动失败只向 stderr 写固定、无内部路径的信息；工具预期错误通过 `CallToolResult(isError=true)` 返回一个简短 JSON 错误对象，不输出 traceback。每个成功结果是一个 JSON 对象，放在单个 `TextContent` 中；本阶段未声明 output schema。

服务用官方 `mcp>=2.1.1,<3` 的低层 `Server`。每个输入 schema 根对象都设置 `additionalProperties=false`，线上 handler 还会独立拒绝缺参、多余参数、错误类型、布尔值冒充整数、超限、非法 ID 和错配，不能只依赖客户端遵守 schema。八个工具 annotations 均为 `readOnlyHint=true`、`destructiveHint=false`、`openWorldHint=false`。

### 工具、参数和返回

| 工具 | 严格参数 | 返回语义 |
|---|---|---|
| `search_documents` | 必填 `snapshot_id`、原始长度 1–400 且空白归一化后非空的 `query`；可选 `top_k` 1–10，默认 5；`excerpt_chars` 1–1200，默认 1000 | 复用 `FixedLibrary.search` → `search_snapshot`；返回 `found/results/snapshot_id/retrieval_mode`。命中项按现有 `score,chunk_id` 排序并携带完整证据投影；无命中真实返回 `found=false,results=[]` |
| `get_excerpt` | 必填 `snapshot_id`、`document_id`、`chunk_id`；可选 `max_chars` 1–1200，默认 600 | 仅当 chunk 属于该文档和快照时返回一个 `result` 与 `truncated`；缺失或错配是工具错误 |
| `get_multiple_excerpts` | 必填 `snapshot_id`、`document_id`、1–5 个唯一 `chunk_ids`；可选 `per_item_chars` 1–1200，默认 600 | 全部 ID 通过归属核验后按请求顺序返回，每项标记 `truncated`；任一缺失/错配使整次调用失败，不产生部分结果 |
| `get_document_metadata` | 必填 `snapshot_id`、`document_id` | 返回白名单文档字段和资产 ID、提取方式/状态、页数、提取字符数、chunk 数；不返回路径、源文件 SHA 或内部 SQLite 信息 |
| `get_document_toc` | 必填 `snapshot_id`、`document_id`；可选 `max_items` 1–100，默认 100 | 按 asset 与 chunk ordinal 派生导航。Markdown 连续相同标题路径形成一节；PDF 每个已索引页形成一节。返回稳定 `section_id`、文档/资产/首尾块 ID、anchor 范围和 `truncated`，并明确不是作者目录 |
| `read_document_section` | 必填 `snapshot_id`、`document_id`、由 TOC 产生的 `section_id`；可选 `max_chars` 1–1200，默认 1200 | 重新派生并核验 section 归属，按 chunk 顺序返回证据项；全部 excerpt 字符总和不超过上限，并返回 `returned_chars/truncated` |
| `find_in_document` | 必填 `snapshot_id`、`document_id`、原始长度 1–400 且空白归一化后非空的 `query`；可选 `top_k` 1–10，默认 5；`excerpt_chars` 1–1200，默认 600 | 在 FTS `MATCH` SQL 内先绑定 document_id，再用同一表达式、BM25 和 `score,chunk_id` 排序；合法无命中返回 `found=false,results=[]` |
| `retrieval_status` | 必填 `snapshot_id` | 重新核验该快照，返回 path-free manifest/corpus/database SHA-256、schema、数量、服务/SDK 版本、只读与 closed-world 状态、未逐个核验的快照候选目录数 `snapshot_candidate_count` 和已知限制 |

ID 格式固定为阶段一生成的 `snapshot_id`、`doc_<24 hex>`、`chunk_<24 hex>`；派生章节为 `sec_<24 hex>`，其哈希身份包含 snapshot、document、asset、导航类型和首尾 chunk，所以只能用于生成它的快照与文档。

### 证据投影和空结果

搜索、摘录、章节和文内查找的每个证据项按适用性返回：`document_id`、`asset_id`、`chunk_id`、标题、identifiers、`source_name`、媒体/材料类型、语言、topics、evidence role、Markdown 标题路径与行号或 PDF 页码、`anchor_label`、`fulltext_verified`、`formula_verified`、有界 excerpt，以及搜索 score。投影不含 `snapshot_path`、`stored_path`、library/SQLite/源文件路径或 `source_sha256`。

只有合法 `search_documents` 或合法文档内的 `find_in_document` 无 FTS 行时返回正常空结果。不存在或错配的 snapshot/document/chunk/section、批量中的任一坏 ID、schema 外参数及其他边界错误均返回工具错误，不能伪装成“没有证据”。

### 每次调用的只读链

1. 固定 library 只解析严格 `snapshot_id` 的直接子目录，并拒绝符号链接；
2. 复用现有全量 manifest、源文件/数据库/schema 哈希、计数、完整性和 v0.1 行语义核验；
3. 把同一已核验 SQLite 镜像反序列化到内存，确认 `temp_store=MEMORY` 与 `query_only=ON`；
4. 安装现有 SQLite authorizer，仅读取绑定参数查询；FTS 内部只特许无值的 `PRAGMA data_version`；
5. 关闭内存连接，用白名单字段组装响应。服务不创建 sidecar、缓存或日志文件。

annotations 只是 host 的行为提示，不承担安全控制；上面的固定根、逻辑 ID、核验、内存只读数据库、authorizer 和投影才是实际边界。

### 已知限制

- 每次调用都会重新读取并核验快照，没有缓存；大快照会有额外本地 I/O 和内存成本。
- FTS5 `unicode61` 没有中文分词，连续无空格中文召回有限；服务不会改写查询。
- Markdown/PDF 导航从现有 chunk anchor 派生，不是正式原文目录；重复标题靠首尾 chunk 进入 section 身份区分。
- TOC 只返回前 100 节且没有分页；章节读取只返回该节开头最多 1200 字符且没有续读游标。`truncated=true` 只表明剩余内容未返回。
- 空检索不能证明资料库中绝对不存在相关证据。
- 文本提取不会自动提升全文或公式核验状态。

## 下一轮产品化阶段 0 合同

### 状态与冻结基线

下一轮使用阶段 0–9 编号，与上文已经完成的历史阶段一至四不是同一组阶段。阶段 0 只冻结合同，不修改运行时代码、快照、SQLite schema 或 MCP schema；阶段 1–6 已按顺序实现，阶段 7–9 仍须依次实现和验收，不能把后阶段能力提前混入前阶段。

冻结前基线是 commit `641eceb5fd5786f3da83c7c948e396c3740a0341`。该基线的 README、本文档和当前运行时 MCP schema 已只读核对；MCP 运行时恰好列出八个工具，使用 Python 3.12 并强制加载该工作树源码时，完整测试为 62/62 通过。

### 不可变产品边界

- MCP 工具总数必须始终恰好为八个，保留现有工具名。八个工具都必须保持 `readOnlyHint=true`、`destructiveHint=false`；不得增加第九个发现工具，也不得暴露导入、构建、创建、重命名、激活、删除、迁移、垃圾回收、密钥管理或任意文件写入工具。
- AI 只能发现已有资料库和快照、显式选择稳定 ID 并读取证据。AI 不得创建、修改、激活或删除资料库、快照、对象、索引、配置或凭据。管理页或 CLI 中由用户明确触发的本地写操作不因此变成 MCP 能力。
- `library_id` 是与名称和路径分离的稳定逻辑 ID；重命名或移动资料库不得改变它。`snapshot_id` 在所属资料库内一经发布不得改变、重用或指向另一份内容。证据身份至少由 `(library_id, snapshot_id)` 确定，不能用显示名称、路径、`latest` 或其他隐式别名代替。
- 现有第八个工具 `retrieval_status` 最终同时承担发现与核验：无 `library_id` 和 `snapshot_id` 时列资料库；只有 `library_id` 时列该库快照；同时提供二者时核验具体快照。只给 `snapshot_id` 必须拒绝。其余七个内容工具最终都必须显式接收 `library_id` 与 `snapshot_id`，再核验 document/chunk/section 等下级 ID 的归属。
- “当前快照”和“上次成功快照”是本地产品状态，必须指向已完整发布且可核验的快照；AI 读取它们不等于有权修改它们。MCP 内容调用仍必须带明确 ID，不能暗中使用当前或最新快照。失败构建不得替换上次成功快照。
- 快照是逻辑完整、冻结且可独立核验的成员清单。它可以引用共享不可变对象，但不得只保存“相对上一快照的增量”而依赖上一快照才能解释；发布后不得改写成员清单、对象身份或哈希。核验必须每次重算，损坏时可以由成功变为失败，但不得把结果写回冻结快照。
- 去重只在同一资料库内部进行，不跨资料库共享用户内容。原文件按原始字节内容寻址；解析结果和文本块的身份必须包含上游原文件/解析文本身份，以及解析器、分块器的实现版本和精确配置；语料向量的身份必须包含精确文本、供应商、模型及版本、维度、输入角色或指令、预处理规则及其版本。只有这些身份全部相同才允许复用。
- 第一版允许每个快照保留独立的小型 BM25 词法索引，以保证冻结排序并控制实现复杂度；不得因此重复保存已可安全复用的原文件、解析块或同配置语料向量。
- 不实现自动删除或垃圾回收（GC，Garbage Collection）。共享对象即使暂时没有新快照引用，也不得在后台自动移除；任何未来删除设计都需要单独合同和用户明确动作。
- 旧私人项目及其文献、数据库、向量、报告、运行态、凭据、Tunnel 二进制和配置继续完全隔离；不得复制、迁移、修改或把它们作为新项目测试夹具。

### 本地 BM25 与联网增强

- `mode="bm25"` 必须是新安装和未明确选择增强时的默认、离线、零密钥、零模型调用完整路径。它不得读取模型配置、静默改写查询、访问网络，或因增强配置存在而改变既有 BM25 语义。
- `mode="enhanced"` 必须由用户或调用方显式选择，并清楚标明会联网。三模型角色固定为问题改写、向量召回和候选重排；第一版只接一套供应商链路，但具体供应商和模型 ID 在阶段 6 按当时可用配置决定，不写死为阶段 0 的事实。
- 增强模式失败必须明确失败，不能静默回退成 BM25 后冒充增强成功；本地 BM25 仍可由用户另行显式调用。实际调用次数、发送的问题/文献内容边界和错误必须可见；不得自动重试，第一次外部错误即停止。
- 增强搜索仍只能返回所选冻结快照中的可追溯证据。若 `search_documents` 在后续 schema 中承载联网增强，其 `openWorldHint` 必须诚实反映外部能力；其余只读证据工具保持 closed-world。

### 现有数据兼容前置

代码仓库本身不能证明用户是否曾把 `--library` 指向仓库外的其他位置，阶段 0 也不扫描任意磁盘位置或读取导入内容。因此不预先发明迁移层。阶段 1 在改变持久化布局前，只读核对明确配置的资料库根和候选快照元数据：没有旧快照时直接采用新布局；存在旧快照时先给出非破坏性兼容方案，至少保留原快照可读且不改写、不覆盖、不删除，再实施经批准的最小兼容。

### 阶段 1 多资料库实现

阶段 1 使用固定用户应用根 `~/Library/Application Support/literature-evidence-mcp/`。新增布局为 `registry.json` 与 `libraries/<library_id>/`；原单库路径和现有 `--library` 入口保留，不删除、不迁移。注册表只保存格式版本、`selected_library_id`、稳定 ID、显示名称和描述，不保存路径。`library_id` 是一次随机生成的 `lib_<32 lowercase hex>`，名称和描述只作为元数据，允许同名且永不参与路径派生。

本地应用层提供 `create/list/select/rename/update_description` 与按 ID 解析物理根；CLI 对应 `literature-evidence libraries ...`。只有 create/select/rename/describe 是显式写动作，list 在空状态下不创建目录。应用根、`libraries`、注册表和各 ID 直接子目录都拒绝符号链接或错误文件类型；注册表用同目录临时普通文件、`fsync` 和原子替换持久化。本地写事务用 `.registry.lock` 非阻塞文件锁串行化；锁冲突明确失败，不等待或自动重试。每个物理根继续复用现有单库快照实现，因此相同名称、文件名或内容仍存放在不同库目录中。

本阶段不改变快照 manifest、SQLite schema、HTTP 路由、管理页流程、MCP 工具名称/数量/schema 或 `FixedLibrary` 单库语义。注册表中的所选库不会被 HTTP 或 MCP 暗中使用；MCP 发现资料库和 `library_id + snapshot_id` 双级参数属于阶段 2。

### 阶段 2 快照生命周期与双级 MCP 实现

每个资料库根新增独立 `snapshot-catalog.json` 与 `.snapshot-catalog.lock`。目录册只登记已经完整构建、全量核验并 no-replace 发布的快照，按 `snapshot_id` 持久绑定发布时的 `manifest_sha256`；后续列表、激活、搜索和 MCP 读取都必须同时通过目录册归属、实时完整核验及 manifest 绑定。目录册用同目录临时普通文件、文件 `fsync`、原子替换和目录 `fsync` 保存；每库非阻塞文件锁串行化构建与激活。若目录册写入在提交前失败，新发布目录会回滚或至少保持未登记、对产品/MCP 不可见，两个指针不变。目录册已经登记的 ID 即使物理目录丢失也不得重用。

首次成功构建同时设置 `current_snapshot_id` 与 `last_successful_snapshot_id`。后续成功构建只更新 `last_successful_snapshot_id`，不会自动切换 current；只有本地核心或 `literature-evidence snapshots activate` 的明确用户动作才能移动 current，并且目标必须属于同库、已登记且重新核验通过。MCP 只有读取权，没有构建、激活、创建、修改或删除入口。

`build_snapshot` 与本地 CLI 保留“仅给 sources 就建立完整新快照”的原行为，并增加明确 `base_snapshot_id`、新增 sources、按旧 `document_id` 替换和移除。继承读取保留 manifest 中的原始 `source_name`，最终 manifest 仍列出完整成员；每个新快照继续复制所有冻结源、生成独立 SQLite/BM25。阶段 2 不实现内容寻址对象区、共享对象、硬链接、增量物理复用或 GC，也不声称节省存储；这些仍属于阶段 3。目录册出现前已有快照时，本阶段明确停止而不自动迁移、改写、覆盖或删除。

MCP 启动改为固定应用注册表根（`--application-root`，省略时使用标准 Application Support 目录），不再固定单个任意 library 路径。工具名和数量仍恰好八个，annotations 仍统一为只读、非破坏、closed-world。七个内容工具都必填 `library_id + snapshot_id`，先由注册表核验库归属，再由每库目录册核验成功快照及 manifest 绑定，最后核验 document/chunk/section；成功结果顶层回显两级 ID。派生 `section_id` 也包含 `library_id`，不能跨库重放。

`retrieval_status` 的四种组合固定为：无 ID 时白名单列出资料库及 current/last；只有 `library_id` 时列该库所有成功登记快照和实时核验状态；两级 ID 同时存在时核验具体快照；只给 `snapshot_id` 明确拒绝。发现输出不含 `library_root` 或其他路径。注册表 selected、current 与 last 都只作为可发现状态，任何内容工具都不得暗用。

### 阶段 3 库内内容寻址增量存储实现

每个资料库根拥有自己的 `objects/`，下分 `source`、`parsed`、`chunks` 三层；对象以 SHA-256 命名的不可变目录保存唯一 `payload`。对象先在同层临时目录写满并 `fsync`，再 no-replace 原子发布。已有目标只有在普通文件、单链接、字节数和摘要全部匹配时才算复用；缺失的已登记对象或任何损坏、摘要错配都明确失败，不覆盖、不补写，也不修改旧快照。不同资料库即使对象摘要相同，仍各自保存独立物理对象，不跨库去重或引用。

原始 `source` 对象身份就是原始字节 SHA-256。`parsed` 使用规范 JSON，身份包含 source 摘要、parser 实现版本和精确配置；PDF 配置还包含实际 pypdf 版本。`chunks` 同样使用规范 JSON，身份包含完整 parsed 摘要、chunker 实现版本和精确配置。parser 变化会新增 parsed 及下游 chunks；仅 chunker 变化只新增 chunks。旧对象不删除，本阶段没有 GC、hardlink、迁移或跨库对象层。

每个 snapshot 的 manifest 仍列出全部成员，并为每个成员完整记录三层对象的固定路径、摘要、字节数和该次新增/复用状态；解释一个 snapshot 不需要读取 base snapshot。snapshot 自身只保存 `manifest.json`、`schema.sql` 和独立 `evidence.sqlite`，因此 BM25 结果仍冻结且可独立选择。SQLite schema version 升为 2，只移除 `asset.stored_path` 的唯一约束，以允许同一快照内多个逻辑文档引用同一个 source 对象；检索表、BM25、证据字段和八工具合同不变。

构建结果和 manifest 固化 `object_references`、`new_objects`、`reused_objects`、`logical_object_bytes`、`new_object_bytes` 与 `reused_object_bytes`。统计只计算对象 payload，不把每快照 manifest/catalog/BM25 固定开销混入对象节省量。对象可在 catalog 最终提交前安全写入；若后续构建或 catalog 提交失败，完整但未引用的对象可以保留，成功快照目录、current 和 last-successful 不会改变，也不会把孤立对象当成成功快照。

### 阶段 4 同配置向量复用实现

阶段 4 只增加离线、确定性假 embedder 下的向量身份、对象复用、完整映射和核验能力。它不改变八个 MCP 工具、BM25 语义或管理页流程，不读取真实密钥，不发真实请求；阶段 5 管理页也不读取或展示向量配置。

### 阶段 5 小白多资料库导入界面实现

HTTP 服务启动时只固定一个 `application_root`。浏览器 API 只能提交严格 `library_id` 和适用的严格 `snapshot_id`；服务每次通过注册表把 ID 解析到物理库，既不接受任意路径，也不调用 selected/current/latest 作为内容动作的隐式目标。`GET /api/status`、`GET /api/libraries`、某库的快照列表与双级核验保持只读；create/select/build/activate 都是页面明确按钮触发的 POST，并继续要求同源会话、Origin、CSRF 和与按钮匹配的 action intent。

管理页从空应用根创建并命名/描述资料库，以名称为主、稳定 ID 为辅显示和显式切换。导入同时支持原生拖放与文件选择器，只接受 `.md`、`.markdown`、`.pdf`；动态文本仅用安全 DOM `textContent`/`createElement`，没有 CDN、前端框架或新依赖。构建请求必须明确 `build_mode=blank|inherit`：blank 禁止基础 ID，inherit 必须提供明确 `base_snapshot_id`，两种模式都必须有至少一个新增文件。网页阶段 5 暂不暴露替换或移除控件；核心/CLI 的既有能力不变。

上传层在受控临时目录逐文件预检。任一文件失败时服务返回结构化文件状态：可归因文件标“失败、未发布”，同批其他文件标“未发布”，且不会调用发布；后续批次级错误则所有文件明确失败。成功响应把每个上传文件标为“已进入成功快照”。这不是流式后台任务；页面只显示“待提交→本机接收/构建中→成功”或“失败、未发布”的诚实同步阶段。构建仍复用阶段 2–3 的非阻塞锁、完整核验、no-replace 发布和 catalog 原子提交；不重试，失败不得改变已有成功快照、current 或 last-successful。

快照列表对所选库的所有登记快照实时核验，显示 current、last-successful、完整 path-free 成员投影，并允许明确激活已核验快照。相对记录的 base，界面显示 added/inherited/replaced/removed 的成员差异；构建成功同时显示阶段 3 的对象引用、新增/复用对象和字节统计，并明确不把每快照独立 BM25 固定开销计入节省量。搜索始终由页面明确提交 `library_id + snapshot_id`，只走离线本地 BM25。

macOS `.command` 保留无终端命令输入的用户体验，但受控持久目录改为标准多资料库应用根，并启动 `serve --application-root ...`。阶段 5 没有打包、签名、发布、浏览器自动化、真实模型、真实 Keychain 或 Tunnel 行为；MCP 名称、数量、schema、annotations 和只读边界均未改变。

### 阶段 6 三角色增强搜索离线链实现

`EnhancedSearchService` 是 Web、`FixedLibrary` 和 MCP 共用的唯一增强链。构造时显式注入非秘密的一个 provider、三个明确 `model_id`、精确 vector profile、候选/字符边界和一个 provider-neutral transport；运行时代码不读取 env、argv、Git、网页请求、快照或日志中的密钥，也没有真实供应商 HTTP adapter。生产 Web 与 stdio 入口不注入 transport，因此显式 enhanced 明确失败为尚未配置，并带 `call_count=0` 审计。

显式增强在任何 transport 调用前固定 `library_id + snapshot_id`，重新核验快照目录册和完整 SQLite，再用阶段 4 `load_verified_vectors` 核验精确 profile/artifact、完整 chunk 映射、payload 摘要、维度与有限数。配置中的向量 provider/model 必须与 artifact profile 一致。核验失败时调用数为 0；不会补写、重算、缓存或修复 artifact。

成功链固定且串行为 `query_rewrite → vector_recall → candidate_rerank`。改写只接收有界原问题；查询向量只接收有界改写问题，并严格校验类型、维度、有限数和非零范数；本地用 cosine 与 `chunk_id` tie-break 在完整 artifact 上召回。默认重排边界为最多 10 个候选、每候选正文最多 600 字符、候选正文合计最多 4000 字符，构造时可在更小的测试/部署边界内显式收紧；返回只能是候选集合内唯一 `chunk_id + finite score`。每个角色恰好调用一次、零自动重试，任一 transport 或输出错误立即停止，已发生几次就只审计几次，绝不静默回退 BM25。

增强成功或失败审计只含调用数、顺序、角色/provider/model、发送类别、查询字符数、候选 ID/数量和候选总字符数；不含原问题、改写文本、候选正文、绝对路径、完整 profile 或秘密。最终证据继续使用现有 path-free 投影，保留明确两级 ID、document/asset/chunk、anchor、核验状态与有界摘录，且只可能来自请求选择的快照；重排返回空列表时允许真实 `found=false, results=[]`。

HTTP 搜索只接受严格 `mode=bm25|enhanced`，省略仍为 BM25。状态接口公开 `enhanced_available` 与非秘密角色/model 摘要；静态页面用原生模式选择并在决策点说明三次潜在外发，没有 CDN 或不安全 DOM。MCP 仍恰好八工具，只给 `search_documents` 增加可选 mode 并设 `openWorldHint=true`；其余七工具 schema 不变、`openWorldHint=false`，`find_in_document` 仍只走 BM25。阶段 6 没有真实模型调用、真实密钥/Keychain、真实 Tunnel、provider 配置 UI、质量评测、打包、签名、发布或阶段 7 内容；离线 fake 通过不能解释为真实链路已验证。

### 阶段 0–9 顺序与最小验收

| 阶段 | 最小交付 | 进入下一阶段前的最小验收 |
|---|---|---|
| 0. 合同冻结 | 本节及 README 的状态说明；不实现后续功能 | 八工具/62 项基线核对完成；文档明确 ID、只读、存储、搜索模式、顺序和停止闸门；完整测试仍通过 |
| 1. 多资料库（已完成） | 用户可创建、命名、重命名和描述资料库；持久保存稳定 `library_id` | 两个资料库重启后仍可独立识别和切换；同名内容不混库；AI/MCP 没有写入口 |
| 2. 快照生命周期与 AI 选库/选快照（已完成） | 候选、当前和上次成功快照状态；`retrieval_status` 完成“列库→列快照→核验”；其余工具显式使用两级 ID | 失败构建不影响旧成功快照；两个库有相似关键词时，八工具不能串库或串版本；MCP 数量仍为八 |
| 3. 增量存储（已完成） | 每库独立共享对象区；快照保存完整成员清单并引用内容寻址的原文件、解析文本和切块 | S1=A+B、S2=A+B+C 时共享对象区只新增 C（S2 自身清单和独立 BM25 索引除外）；S1 仍完整表示 A+B；修改 B 或解析/分块配置只新增变化对象，旧对象和旧快照不变且不得误复用 |
| 4. 同配置向量复用（已完成） | 冻结语料向量身份和缓存逻辑；只用确定性的离线假模型测试 | 同配置下 S2 只为 C 生成向量；任一文本、模型、版本、维度、指令或预处理变化都不得误复用；不发真实请求 |
| 5. 小白导入界面（已完成） | 在现有本地网页中创建/切换资料库，拖放或选择文件，显示逐文件阶段、失败原因、快照差异和新增/复用统计 | 从空白状态仅用界面建立两个库和多个快照；无需终端；部分失败不会发布残缺快照或破坏旧快照 |
| 6. 三模型增强搜索（已完成） | 问题改写、向量召回、候选重排的一套可替换传输层；BM25 与增强模式分离 | 用离线假服务验证调用次数、外发边界、首错停止、无自动重试和无静默回退；真实模型、真实密钥和真实钥匙串不在本轮验收 |
| 7. 搜索质量验收 | 公开或合成的多库、中英文、跨语言正例和负例；BM25/增强对比与指标报告 | 离线证明不串库/快照、引用可回到页码或 anchor、负例可真实为空，且评测流程可重复；不得把假模型结果宣称为真实模型质量提升 |
| 8. 本地 MCP 向导 | 为支持 stdio 的 host 生成可复制配置，提供八工具本地自检和连接状态；不自动改其他软件配置 | 用项目内本地 stdio 测试客户端和离线 host 替身验证生成配置可依次列库、列快照、核验、搜索和读证据；自检保持只读且不产生资料库写入，真实 host 配置留待后续用户复核 |
| 9. ChatGPT Secure MCP Tunnel 向导 | Tunnel 配置、权限提示、启动/停止与健康状态流程；生产适配器和离线替身分离 | 仅用离线替身验证状态机、脱敏错误和停止流程；模拟结果只能标为“模拟通过”，不得显示真实“已连接”；到此停止，不启动真实 Tunnel |

### 阶段 9 后的停止闸门

阶段 0–9 整体路线只允许本地实现、公开/合成夹具、离线假模型、假钥匙串和假 Tunnel 验收。以下动作都不属于这条路线的当前授权：真实模型或付费 API 调用、读取或写入真实钥匙串、启动真实 Secure MCP Tunnel、打开或操作 ChatGPT 网页、提交真实认证/轮询、修改其他应用配置，以及任何需要操作用户电脑的复核。

到达任一真实外部动作前必须停止并报告：准备调用的问题数和总调用数、会发送的查询/正文范围、模型及成本可见性、零自动重试与首错停止规则，以及需要用户完成的电脑操作。阶段 9 完成后不自动进入首次使用、真实链路排障、总验收、App/DMG 打包、签名、公证或发布。
