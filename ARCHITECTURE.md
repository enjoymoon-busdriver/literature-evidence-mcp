# 最小架构合同

本合同冻结 `0.1.0.dev0` 阶段三的边界。阶段二在阶段一
`build_snapshot` / `verify_snapshot` / `search_snapshot` 核心之外增加固定资料库应用层和本机管理页；阶段三再增加八个本地 stdio、closed-world、只读 MCP 工具。三个阶段均不改变快照格式、SQLite schema、分块规则或 BM25 算法。

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

## 阶段三只读 MCP 合同

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
