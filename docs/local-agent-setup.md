# 把这段提示词交给你的本地 Agent

这份流程只适用于 **Apple Silicon（arm64）macOS** 上的源码开发版，不是已签名、公证的 App/DMG/pkg。Agent 的文件和终端工具必须作用于要安装软件的同一台 Mac；浏览器控制是可选的。这里的“本地”不要求 Agent 的模型离线运行，但只有云端工作目录、不能操作这台 Mac 的 Agent 无法完成安装。

把下面整个文本块复制给本地 Agent。过程中只需要你做明确授权，以及亲自完成登录、系统确认和密钥输入；不要把 Key 发进聊天。一次提示词也不等于无人值守安装保证，更不等于已经在一台全新 Mac 上验收。

## 完整提示词

```text
你要在当前这台 Mac 上为普通用户安装并验收 literature-evidence-mcp 源码开发版。
你负责执行所有终端命令；不要让用户复制执行命令。用户只负责做选择、批准有影响的动作，
以及亲自在本机可信界面完成登录、系统授权和密钥输入。

可信项目仓库：https://github.com/enjoymoon-busdriver/literature-evidence-mcp
OpenAI MCP 官方说明：https://learn.chatgpt.com/docs/extend/mcp?surface=cli
Secure MCP Tunnel 官方说明：https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

目标：先交付零 Key、离线 BM25、合成样例和八个只读 MCP 工具的本机自检；然后只配置用户
明确选择的一条连接路线：本机 stdio，或 ChatGPT 网页 Secure MCP Tunnel。增强模型完全可选。

总规则：
1. 先只读盘点，再说明将写入哪里、是否联网和会改变什么，得到确认后才执行写操作。
2. 使用实际发现并解析后的绝对路径，所有含空格路径都正确引用；不要写死任何人的用户名、
   个人目录或 /tmp worktree。不要从旧项目猜路径，也不要扫描或导入用户私人目录。
3. 保留现有资料库、快照、.venv、连接状态和客户端设置。不得删除、迁移或覆盖不明对象；
   发现冲突先报告。不得顺手修改其他项目、shell 配置、PATH、后台服务或系统安全设置。
4. 默认始终是本地 BM25。只使用仓库内的 demo/synthetic-evidence.md 做首次验收；
   不自动导入真实 PDF/Markdown，不读取旧私人 RAG、文献目录或其他历史项目。
5. 第一个错误即停止当前阶段，不自动重试模型调用，也不把“离线通过”写成真实链路通过。
6. 缺少任何 Key 不算整套安装失败；应完成并交付本地管理页、BM25 和本地 STDIO 自检。

阶段 0：只读检查环境与已有安装
- 先确认 Agent 确实在目标 Mac 本机；读取系统与架构，要求 Darwin + arm64。
- 查找用户已经提供或当前打开的项目根；完整根必须同时包含 pyproject.toml、
  启动文献证据管理页.command、scripts/macos_launcher.py 和 src/literature_evidence_mcp/。
- 动态确定 PROJECT_ROOT、当前用户 HOME，以及标准 APPLICATION_ROOT：
  HOME/Library/Application Support/literature-evidence-mcp。这里的 HOME 必须来自当前登录用户。
- 只读检查仓库来源、Git 工作树、当前 ref/commit、pyproject 版本，以及已有 .venv；
  检查 APPLICATION_ROOT 是否已有 registry.json、libraries、connections.json、mcp-server 和
  bin/tunnel-client。不要读取 macOS 钥匙串，也不要显示或搜索任何 Key。
- 若本机客户端已经有名为 literature-evidence 的 MCP 设置，只查看该项的存在与入口；
  不读取、重写或整理其他客户端配置。先把所有发现和可能冲突告诉用户。

阶段 1：取得完整可信源码与明确版本
- 若当前已有完整 Git 仓库，确认 origin 对应上面的 GitHub 所有者与仓库（HTTPS/SSH 地址均可）；
  不仅凭文件夹名称判断来源。来源不符时停止，不执行代码。
- 若尚无源码，先让用户选择安装目录；再从可信仓库取得完整项目，不在已有目录上覆盖。
- 优先选择用户明确指定的 Release/tag；没有 Release 时，展示将使用的完整 commit SHA，并说明
  这是开发快照，得到确认后才检出。不要静默跟随会移动的 latest/main/默认分支。
- 取得后记录仓库 URL、tag/ref、完整 commit SHA 和 pyproject 项目版本，再核对上述四个必需入口。
  下载、克隆或切换版本都是写操作；执行前先说明目标目录和网络访问范围并等待确认。

阶段 2：预检并准备受控环境
- 从实际 PROJECT_ROOT 调用项目已有入口，不另写安装器，不手工创建替代 venv。先执行：
  /bin/zsh "$PROJECT_ROOT/启动文献证据管理页.command" --preflight-only
- 预检必须确认 macOS arm64、Python 3.11+、SQLite serialize/deserialize 往返和 FTS5；
  --preflight-only 应保持只读，不创建文件、不安装、不启动服务。
- 若缺 Python 或需安装器/系统许可，只给出 Python.org 官方来源、影响范围与所需权限；
  等用户确认后由你执行下载/启动安装，密码、Touch ID 和系统确认由用户本人完成。
- 在执行 --prepare-only 前，单独检查 APPLICATION_ROOT/mcp-server。该入口若是符号链接或
  非普通文件就停止；若普通文件已指向另一份项目/.venv，说明会替换的精确范围并等待确认。
  对已有同名客户端配置也一样：此阶段不替换它。
- 说明 --prepare-only 可能在 PROJECT_ROOT/.venv 安装当前源码和开源依赖、访问默认 PyPI，
  创建标准 APPLICATION_ROOT，并原子安装 0700 的 mcp-server shim；它不应读取文献或 Key。
  用户确认后执行 --prepare-only，不用 sudo，不装 Homebrew，不修改 PATH/shell 配置。
- Git 代码更新不代表运行程序已经更新。准备后核对 .venv 安装标记中的 source_sha256 与
  当前 launcher 的 source_fingerprint(PROJECT_ROOT) 一致；再用 .venv/bin/python -I -B
  核对实际导入的包位于该 .venv 内，包版本与当前源码一致。未匹配就不要启动或做自检。

阶段 3：启动本机管理页
- 检查默认 8765 是否可用；需要换端口时选一个明确空闲的 1024–65535 端口并记录。
- 在你能持续管理的前台终端会话中执行启动入口的 --no-browser --port <实际端口>；
  确认服务只监听 127.0.0.1。若用户希望正常 Finder 体验，可改为启动不带 --no-browser 的入口。
- 打开 http://127.0.0.1:<实际端口>/。有浏览器控制权限时可操作非秘密步骤；没有时只打开页面
  并逐步告诉用户点哪里，不要求用户运行终端命令。保持前台终端，Ctrl+C 是管理页停止开关。

阶段 4：只用合成资料完成本地验收
- 告知用户将只在 APPLICATION_ROOT 新建一个清楚标为演示的资料库和冻结快照，确认后继续。
- 在管理页创建演示资料库，选择“从空白建立”，只导入 PROJECT_ROOT/demo/synthetic-evidence.md；
  等构建成功后核验并选择该快照。不要选择用户的真实文件或浏览私人目录。
- 保持搜索模式为 BM25，用 “Shannon entropy” 验证真实命中、稳定 document/chunk 与章节锚点；
  再用一个合成文件中不存在的唯一词验证 found=false、results=[]。全程应为零 Key、零模型调用。
- 在“连接本地 AI / MCP”区域运行“本地离线 STDIO 自检”。要求报告通过、工具数恰为 8，
  并完成列库→列快照→核验、默认/显式 BM25 和 get_excerpt；它使用隔离 fake HOME，
  通过不代表真实客户端已配置。失败时停止，不用旧 .venv 重跑来掩盖版本不匹配。

阶段 5：让用户只选一条连接路线
- 先询问并等待选择：A. 暂不连接，只保留本地管理页；B. 本机 stdio；C. ChatGPT 网页 Tunnel。
  默认 A。不要同时配置 B 和 C，也不要把无 Key 的 A 当失败。

路线 B：本机 stdio
- 先重读上面的 OpenAI MCP 官方说明。明确告知：同一 Codex host 上的 ChatGPT desktop、
  Codex CLI 与 IDE 扩展共享本机 MCP 配置；ChatGPT web 不读取它。用户不同意共享影响就不配置。
- 只采用管理页当次生成的 CLI 或等价 TOML，不自行拼另一条路径；它应通过 /bin/zsh -fc
  启动标准 APPLICATION_ROOT 内同一个 mcp-server shim，且不含 Key、env 或项目绝对路径。
- 用客户端正常列表或 codex mcp list 只读检查同名项。若已存在，展示旧入口与拟替换范围并等待
  明确批准；不得无审批覆盖 ~/.codex/config.toml、项目级配置或其他 MCP server。
- 批准后由你执行管理页生成的精确 CLI，或在用户选定客户端的 MCP 界面填同一 command/args；
  重新加载该 MCP 连接；若必须重启整个客户端，先确认没有未保存工作并征得同意。
  核对本项目恰好八个工具，再对演示快照运行 retrieval_status 和一次 BM25 查询。
  不调用 enhanced。把当前客户端提供的禁用/移除方法作为关闭开关交付，不猜不存在的命令。

路线 C：ChatGPT 网页 Secure MCP Tunnel
- 先重读上面的 Tunnel 官方说明。说明它是私有、出站 HTTPS 的开发者模式连接，不开放入站端口；
  它不支持公开插件提交/分发，公开 GitHub 源码也不等于 Tunnel 已成为公开插件。
- 创建/编辑 Tunnel 需要 Tunnels Read + Manage；运行客户端或创建 app 时选择 Tunnel 需要
  Tunnels Read + Use；目标 Platform 组织与 ChatGPT 工作区必须正确关联，developer mode 是
  独立权限。登录、组织选择、Tunnel 创建、app/插件安装及工具权限都先说明范围并逐项等确认。
- 经确认后，只从 Platform Tunnel 设置下载链接或 openai/tunnel-client 官方最新 Release 取得
  与 macOS arm64 相符的二进制。若 APPLICATION_ROOT/bin/tunnel-client 已存在，不无审批覆盖。
  最终路径必须正好是该位置的普通可执行文件，不能是 symlink；核对架构和 help 后再继续。
- 不照抄官方示例把 CONTROL_PLANE_API_KEY 放进环境变量，也不写明文 YAML、文件或 argv。
  本项目要求用户亲自在本机管理页的密码框输入运行 Key，保存到 macOS 钥匙串；运行时应用
  通过匿名 pipe 交给 tunnel-client。你不得要求 Key 出现在聊天，也不得读取截图、DOM、剪贴板、
  密码管理器、日志或密码框值；输入前暂停，等用户说已保存后只检查“已配置”状态。
- 可协助打开 Platform 页面并走到创建运行 Key 的最后一步，但最终“生成/创建 secret”必须由
  用户本人点击并自行复制、填写。Tunnel ID 不是 secret，但保存前仍核对它属于本项目。
- 让用户明确决定是否接受官方 tunnel-client 的断线退避重连；这是传输重连，不是模型重试。
  保存 Tunnel ID 与该选择后，启动真实 Tunnel、检查本地健康，再到 ChatGPT Plugins 创建
  developer-mode app，选择该 Tunnel，展示将授予的插件/工具权限并等用户确认后完成。
- 核对 ChatGPT 实际发现八个工具，并只对演示快照做一次默认 BM25 查询。没有看到工具或首个
  认证/传输错误就停止并先点“停止真实 Tunnel”，不自动重启；本地健康不能代替工具发现。

阶段 6：可选付费增强，不能夹带进连接验收
- 只有用户另行选择增强模式时，才让用户亲自在本机管理页填写阿里云按量付费 Key；沿用上述
  secret 规则。先点“查看发送量与调用数”，展示将发送的块数、字符数、计划调用数和可能费用；
  等明确同意后才点“明确发送并构建向量”。预览本身不得调用模型或读取 Key。
- 向量完成后，先说明演示查询、会发送的候选范围及最多三次模型请求，得到确认后只做一次
  enhanced 查询；核对返回的实际调用审计与原文出处。它依次做改写、查询向量、候选重排，
  模型调用零自动重试、首错停止。官方 Tunnel 的退避重连不改变这一模型零重试规则。

阶段 7：交付与开关
- 报告实际仓库 URL/ref/commit/版本、PROJECT_ROOT、APPLICATION_ROOT、端口、源码与安装匹配证据、
  合成 BM25/STDIO 自检结果、唯一选定的连接路线、实际配置变更、网络/模型调用与未验收项。
- 开：双击完整项目里的 .command，或由你用该入口启动；本机 stdio 由客户端按需拉起。
- 关：真实 Tunnel 路线先点“停止真实 Tunnel”，然后在前台终端 Ctrl+C 停管理页；关闭管理页
  也应停止它拥有的 Tunnel。本机 stdio 用用户选定客户端的禁用/移除开关，不删资料库或 shim。
- 搜索开关默认 BM25；只有用户主动选择 enhanced 才会调用模型。没有 Key 时交付本地模式即可。
- 最后明确写出：这次结果只证明本机所做的具体检查；不得宣称“一段提示词全无人值守成功”、
  “一般检索质量已证明”或“已在全新 Mac 上验收”。完成这些后停止，不扩展到发布或其他项目。
```
