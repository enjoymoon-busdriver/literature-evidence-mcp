# 最小架构合同

本合同冻结 `0.1.0.dev0` 阶段二的边界。阶段二只在阶段一
`build_snapshot` / `verify_snapshot` / `search_snapshot` 核心之外增加固定资料库应用层和本机管理页，不改变快照格式、SQLite schema、分块或 BM25 算法。

## 信任边界

- Uvicorn 只绑定 IPv4 回环地址 `127.0.0.1`。浏览器入口固定为
  `http://127.0.0.1:<port>`，没有可配置的 `--host`。
- 启动命令只固定一次 `library` 根。HTTP 请求不能提供或更改 library 路径，也不能提供快照路径或任意本机源路径。
- 浏览器必须由用户明确选择 `.md`、`.markdown` 或 `.pdf`。所谓“上传”只是浏览器把字节发送到 `127.0.0.1` 的本机进程；不是云上传。
- 后端保留原文件名，把字节复制到一次性受控临时目录，再调用阶段一
  `build_snapshot`。不安全文件名、符号链接语义、错误类型和资源超限均拒绝；成功和失败都清理临时目录。
- 只读应用层只枚举 `<library>/snapshots` 的直接子目录，只接受阶段一生成格式的 `snapshot_id`，不跟随 library、snapshots 或候选快照符号链接。

## 浏览器安全合同

- `Host` 必须精确等于 `127.0.0.1:<port>`；请求一旦带 `Origin`，它必须精确同源。所有 POST 必须带同源 Origin。
- 进程启动时生成随机密钥。浏览器会话 Cookie 名绑定已验证端口，值是随机 session ID 加 HMAC 签名，设置 `HttpOnly`、`SameSite=Strict`、`Path=/`；CSRF token 使用不同域的 HMAC，并以常量时间比较。
- 项目不配置 CORS，也不返回 `Access-Control-Allow-*`。CSP 只允许同源脚本、样式和连接，并禁止 frame、object、base 与表单导航。
- 请求体先在 ASGI 接收层计数，再在文件复制层复核。任何错误只返回中文可操作信息，不返回 traceback、临时路径或固定 library 的绝对路径。
- Uvicorn 不信任代理头、不写访问日志、只运行一个 worker。前台进程由 Ctrl+C 停止；停止后必须释放端口。

## 动作与端点

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

阶段二没有删除、覆盖、激活、监控、自动导入、任意文件读取、任意路径、快照迁移、OCR、PDF 转 Markdown、embedding、reranker、远程 combo、云模型、API Key、Tunnel、Zotero、聊天界面或 MCP。

## 阶段三只读合同（计划，不在本阶段实现）

阶段三计划八类能力：

1. `search_documents`：搜索文献；
2. `get_excerpt`：取得一个证据片段；
3. `get_multiple_excerpts`：批量取得受控证据片段；
4. `get_document_metadata`：读取文档元数据；
5. `get_document_toc`：读取文档目录；
6. `read_document_section`：读取受控章节；
7. `find_in_document`：在单篇文档内查找；
8. `retrieval_status`：读取检索与快照状态。

这八类能力统一只接受受控 `snapshot_id` 和必要的文档/片段 ID，只访问该快照，检索仅用本地 SQLite BM25，保持 closed-world。MCP 不得暴露 build、library/快照/源文件路径、删除、覆盖或迁移能力。
