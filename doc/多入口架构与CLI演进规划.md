# Piece 多入口架构与 CLI 演进规划

> 日期：2026-09-08  
> 状态：实施中，2026-09-10 第五次会话完成 GUI 免登录引导令牌与密码可发现性（含托盘一键复制，见第 18 节）；`tests/automated` 206 项全绿。M1 剩窗口/托盘与三系统实机，M4 剩 skill 场景验收，M5 剩三系统实机、真实外部依赖与发布。新会话先读第 18 节。  
> 本次修订：按用户说明以空知识库为前提，取消旧知识库数据兼容和迁移要求。  
> 用途：为后续开发提供目标、边界、分阶段任务和验收依据；不是当前功能说明，也不是已完成记录。

## 0. 阅读约定与决策状态

### 0.1 本次讨论的目标

将 Piece 演进为：

**共享业务层 + 独立核心运行时 + CLI 自动化入口 + 两个工作流 skill + 保留 GUI/MCP。**

不是把原来的“GUI/MCP 主导业务实现”替换成“CLI 主导业务实现”。业务规则只能维护一份，GUI、CLI、MCP 是不同入口，skill 是 CLI 的使用指南。

用户允许为达成目标继续进行必要的较大架构调整，但不以目录重组、代码搬迁或增加架构层本身作为成果。

### 0.2 已确认的产品选择

**业务 CLI 必须连接已经运行的服务。**

- 执行 `piece search`、`piece file import` 等业务命令时，不得隐式执行 `serve` 或拉起后台服务。
- 服务没有运行时，返回明确、可机器识别的错误，并提示用户先启动 `piece serve`。
- 两个 skill 不得通过自行后台启动进程来绕过这一选择；只有用户明确要求启动服务时，才指导或执行启动操作。
- 保留显式的 `piece serve` 和 `piece autostart install/uninstall`；后者仍须由用户主动调用，不默认注册登录自启。
- 现有无参数启动、双击 exe 等桌面兼容入口，属于用户主动启动应用，不等于业务命令隐式启动服务。本规划不要求移除这些入口。

**本轮按空知识库实施，不承担旧知识库数据兼容。**

- 用户已说明知识库文件已经删除，本轮不要求保留或迁移旧文件、卡片、向量、集合关系、原文关联、历史任务及其 ID。
- 可以按目标模型直接定义新的数据库和任务结构，从新库初始化；不需要为旧 schema、旧任务负载建立兼容桥或迁移脚本。
- 这只免除重构前历史数据的兼容要求，不免除新版本投入使用后的持久化、重启恢复、防重复写入和误删保护。
- 现有配置、密钥和对外接口另行保护，不因知识库文件已删除而自动重置。

### 0.3 建议默认方案

下列工程选择是本规划的建议默认值，不应表述为用户已经逐项确认。实施阶段 M0 应核对并记录必要调整：

- 继续使用 Python、SQLite、现有解析和检索技术，不同时换语言、换数据库、重写整个 GUI。
- 本机个人知识库优先，不在本轮增加远程多用户管理、账户体系、云端托管或分布式任务系统。
- 保留现有有效配置和密钥；配置保护与旧知识库内容兼容是不同要求，不因按新库实施而自动清空或轮换。
- 保留既有 MCP 服务与客户端接入方式，优先通过适配保持兼容。
- 核心、CLI、GUI、MCP 保持同一仓库和统一版本，允许拆分可选依赖，不急于拆成多个独立项目。
- 不以完整复刻每个 GUI 按钮为目标；优先覆盖数据操作和可自动化工作流。

### 0.4 与上一轮规划的关系

`doc/架构改造方向.md` 记录的是上一轮已经实现的服务常驻、窗口解耦、唯一数据库写入方、解析 helper 隔离及平台适配。

本文件是新增产品目标驱动的下一阶段规划，不覆盖、不改写上一轮的完成记录，也不把上一轮尚未完成的实机与发布验收自动标记为完成。

**本文件中的命令、API、模块划分和字段扩展，除明确说明已有的部分外，均为目标设计。**

---

## 1. 最终效果

### 1.1 普通用户继续使用 GUI

用户仍然可以打开管理界面，上传文件、查看和编辑卡片、进行原文对比、检索诊断和设置管理。

最终应满足：

- CLI/MCP 提交的文件和任务能在 GUI 中看到。
- GUI 修改后的内容，CLI/MCP 随后读取到同一份结果。
- 关闭窗口、刷新页面或断开浏览器连接不影响已受理任务。
- GUI 不承担启动或维持核心业务的必要职责。

### 1.2 脚本用户不打开 GUI 即可完成核心业务

目标示例，具体参数名称在 M0/M3 冻结：

```bash
# 终端 A：用户显式启动服务
piece serve --no-tray

# 终端 B：向已经运行的服务提交操作
piece file import "./资料/论文.pdf" --collection "论文" --wait --json
piece search "这个方法有哪些限制" --collection "论文" --json
piece chunk get 123 --json
piece file export 10 --format markdown --output "./导出/"
```

注意：当前 `--no-tray` 只表示不启动托盘，不表示不加载 GUI。真正无 GUI 的运行方式需要本轮实现，最终开关命名在 M0/M1 确定。

不打开前端即可完成：

**配置 → 导入 → 等待索引 → 检索 → 取正文 → 编辑/归类 → 重新索引或导出。**

### 1.3 AI 使用两个工作流 skill

- 检索 skill：发现文件/集合、限定范围、搜索候选、取回正文、按需取图或原页、引用来源。
- 建库 skill：导入原始文档或直接写卡片、等待任务、处理部分失败、读回验证、在确认后执行维护操作。

skill 不操作 GUI，不直接写数据库，也不临时重建一套解析/索引脚本。

### 1.4 MCP 客户端继续工作

保留检索和索引两个 MCP 入口。已有客户端继续使用标准工具协议，不必具备 shell 或 skill 支持。

MCP 工具是共享业务能力的适配，不再成为某项业务规则唯一的实现位置。

---

## 2. 当前基线与主要差距

以下定位基于本轮代码检查，实施时仍须核对当前版本。

| 当前基础 | 代码定位 | 后续处理 |
|---|---|---|
| CLI 已有 `serve/open/autostart`，帮助入口轻量 | `app/cli.py` | 保留兼容，增加业务命令与稳定输出 |
| Worker、MCP 和 UI 目前由 NiceGUI 启动流程组织 | `app/server.py` | 核心运行时接管生命周期，GUI 改为可选入口 |
| 服务生命周期独占数据库 | `app/platform.py:329` | 保留，不让新 CLI 绕过 |
| 索引编排在服务进程内，原生解析使用 helper | `indexing/worker_manager.py`、`indexing/services/parser_helper.py` | 保留隔离边界，不重新拆出业务 Worker 进程 |
| 文件、卡片、集合、任务已有服务层 | `indexing/services/` | 优先复用，补充完整业务用例 |
| 上传的格式检查、查重、保存、登记、归类和入队仍由 UI 串联 | `app/ui/handlers/file_handlers.py:593` | 提取共享导入用例 |
| 重新索引的冲突检查和覆盖提示仍在 UI handler | `app/ui/handlers/file_handlers.py:392` | 共享业务负责校验，入口负责展示和确认 |
| 配置保存、生效判断和嵌入实例刷新仍由 UI 串联 | `app/ui/handlers/settings_handlers.py:79` | 提取统一配置更新流程 |
| 批量卡片受理规则在 MCP 工具模块内 | `indexing/mcp/tools/chunk_tools.py:123` | 下沉为共享批量写入能力 |
| 部分任务类型和输入借用 `error_message` 编码 | `indexing/services/chunk_service.py:301`、`indexing/services/task_service.py` | 分离任务类型、输入、结果和错误 |
| 平台基础函数在 `app.platform`，被索引和数据库调用 | `indexing/settings.py`、`indexing/database.py` 等 | 按职责整理公共基础设施，不仅为换目录而搬迁 |
| GUI 和 MCP 依赖目前随主项目安装 | `pyproject.toml` | 支持核心/CLI 与可选入口的依赖边界 |

本轮此前实际运行的 CLI、平台、自启测试为 69 项通过；这不代表本规划的业务 CLI、核心运行时或三系统实机已经验收。上一轮其他测试结果见原规划记录，不当作本轮重新运行的结果。

---

## 3. 目标架构与依赖规则

### 3.1 四类职责

| 部分 | 职责 | 不应承担 |
|---|---|---|
| 共享业务 | 文件、卡片、集合、检索、任务、配置等完整业务操作及规则 | GUI 提示、CLI 参数解析、HTTP/MCP 协议格式 |
| 核心运行时 | 配置装配、数据库锁与连接、任务编排、helper、后台同步、安全启停 | 依赖窗口或 UI 会话才能存活 |
| 入口适配 | 本地 HTTP API、CLI、GUI、两个 MCP 的输入输出与身份校验 | 复制业务实现、绕过业务规则直接操作数据库 |
| 工作流 skill | 任务选择、命令组合、结果解释、确认与引用规范 | 数据库访问、解析引擎、独立重试/并发框架 |

CLI 是独立进程，通过本地 API 访问服务。GUI/MCP 若在服务进程内，可以直接调用共享业务用例；不强制它们绕一次 HTTP。

GUI 不调用 CLI 子进程来执行业务；CLI 不导入 GUI handler 或 MCP 工具实现来复用业务。

### 3.2 不变约束

1. **同一知识库只有一个持有锁的核心服务负责数据库写入。**
2. **业务规则不能因入口不同而不同。** 输入类型、权限和显示形式可以不同，实际校验与写入行为必须一致。
3. **原生解析 helper 不连接数据库，不创建任务。**
4. **任务在服务端持久化受理后，不由客户端连接存活控制。**
5. **核心与 CLI 不要求安装或导入 NiceGUI、pywebview、pystray。** MCP 依赖也不得成为不使用 MCP 时的业务调用前提。
6. **只读帮助、版本查询不初始化数据库、配置或日志目录。** 保留现有轻量入口性质。
7. **错误不能被包装成成功。** 受理、完成、部分失败、等待超时必须明确区分。

### 3.3 模块组织原则

先确定职责和依赖方向，再调整目录。允许逐步收敛到统一包结构，但不要求第一步全仓重命名。

- 业务层不得反向导入 GUI、CLI 或 MCP 适配器。
- 平台路径、进程辅助、配置模型等公共模块不得顺带加载 GUI。
- 对跨入口共享的输入校验可复用现有 Pydantic；不要为每个函数建立一套空接口和工厂。
- 不引入无实际需求的依赖注入容器、通用命令总线、插件系统或消息总线。
- 代码调整期间可保留薄兼容入口，但必须标记用途和移除条件，不能长期保留两份业务实现。

---

## 4. 运行模型与本地接口

### 4.1 显式服务模型

- `piece serve`：用户主动启动核心服务。
- `piece open`：打开已运行服务的 GUI；服务未运行时不代为启动。
- 业务命令：只连接服务，未运行时失败并给出启动提示。
- `autostart install/uninstall`：继续是显式的系统集成操作，不因安装程序、首次检索或 skill 使用而自动执行。

不开发业务命令隐式启动、闲置自动退出、双套独立 Worker 或“找不到服务就直接写库”的回退路径。

### 4.2 GUI/MCP 可选运行

核心运行时必须支持：

- 仅本地 API + 业务能力的无 GUI、无 MCP 模式。
- 本地 API + MCP，不启用 GUI。
- 本地 API + GUI + MCP 的完整桌面模式。

这些是组合能力，不要求设计复杂的 profile 框架。入口开关可以由简单配置及启动参数表达。

- 旧完整安装的启动体验优先保持兼容。
- 精简安装如何确定默认启用项，在 M0/M1 明确；不能靠导入异常静默猜测。
- 未启用 GUI 时，`open` 给出可操作提示，不偷偷重启有在途任务的服务。
- 不支持 GUI 时，CLI 不能静默安装 GUI 依赖。
- 窗口和托盘是界面能力，不属于核心服务可用性的条件。

### 4.3 本地 API

建议在核心服务上提供一组有版本的薄 HTTP 接口，复用现有 ASGI/HTTP 技术。具体路由在 M0/M3 定稿。

- 不新增独立的“CLI 服务器”进程。
- 优先复用管理端口，保留已有端口与配置的兼容映射，避免要求用户无意义修改端口。
- 两个 MCP 继续保留独立入口和读写权限；不能以合并传输为理由混合权限。
- API 调用共享业务用例，不创建另一套业务层。
- 对文件导入、卡片修改、删除、配置更新等建立明确接口，不暴露任意 Python 函数执行或任意数据库语句执行入口。

### 4.4 实例身份与连接目标

CLI 不能只判断“端口有人监听”就认为连接正确。

握手/状态信息应能确认：

- 对端确实是 Piece，API 版本兼容。
- 当前数据/配置目标与用户指定目标一致。
- 核心服务和必要组件是否就绪。

端口被其他程序占用、连接了另一知识库、权限不足、服务版本不兼容，应分别返回明确错误。不能静默连接默认知识库或自行创建另一份数据目录。

### 4.5 生命周期

核心运行时拥有启动与关闭顺序，而不是由 GUI 回调间接持有：

- 启动：确认配置与实例目标 → 获得数据库独占锁 → 新库初始化或目标结构校验 → 数据库资源 → 编排与 helper → 可选入口 → 宣布就绪。
- 关闭：停止接收新的业务写入 → 收尾 API/MCP 在途请求 → 停止同步和索引继续领任务 → 等待或安全取消在途工作并回收 helper → 关闭数据库资源 → 释放锁。

需要继续区分“核心服务失败”和“某个可选入口失败”。可选入口不可用时应报告降级状态，不能假报全部启动成功。

不承诺尚未验证的固定秒数退出。同步外部调用能否立即取消，必须以实际实现和超时边界为准。

---

## 5. 共享业务与数据可靠性要求

### 5.1 文档导入

统一处理格式与转换器检查、大小限制、内容查重、保存、文件登记、集合归类和索引入队。

- 不同入口对相同文件采用相同的查重和命名规则。
- 重复导入应返回已存在文件信息，不盲目创建第二份索引。
- 并发导入不能仅依赖“先查再插”的无保护流程，应由共享逻辑和数据库约束共同保证一致性。
- 文件保存、数据库登记、任务受理失败时，要有临时文件清理与必要补偿；不能误删用户原件。
- CLI 递归导入默认不跟随目录符号链接，报告跳过、重复、成功与失败项，避免目录循环及静默漏项。
- 复制文件和建立索引是不同阶段，结果须分别可诊断。

### 5.2 卡片和集合

- 支持按稳定 ID 读取、修改和删除，不要求 AI 只凭可能重名的标题定位。
- 卡片更新同时覆盖标题和正文能力，保留原页关联规则。
- 批量写入的数量、文本大小和逐项结果由共享业务统一校验。
- 文件/卡片删除后的级联行为、最后一张卡片删除的语义须明确，并与现有行为兼容或显式说明变化。
- 集合改名、删除、归类和文件属性修改均通过统一入口。

### 5.3 重新索引

- 服务端统一检查同文件冲突，不把 UI 按钮禁用当作并发保护。
- 明确从原件还是工作文件重新解析，以及是否覆盖手工修改。
- 用户确认只发生在交互入口；业务层仍必须校验操作条件。
- 目标安全要求：新一轮索引失败不破坏已有可用索引。实现时评估暂存结果、成功后发布及清理方案，不提前承诺复杂的多版本管理平台。
- 正常重新索引可能产生新的卡片 ID，应向调用方说明并返回新结果的定位信息，不承诺重新索引永远保留卡片 ID。

### 5.4 任务模型

将任务类型、输入、状态、进度、结果、错误分离，避免继续把任务输入编码到 `error_message`。

需要明确：

- 同一文件的冲突写入由服务统一排序或拒绝，不能由各客户端自己协调。
- 受理结果必须在任务输入持久化后返回。
- 已受理任务返回 `task_id`；完成结果能定位文件/卡片，不要求再搜索猜测结果。
- 批量操作允许部分受理时，保留所有已受理 ID，只重试明确失败且允许重试的项。
- 卡片新增等非幂等操作需要请求去重机制，或明确禁止不确定状态下盲目重放；具体机制在 M2 定稿并配套测试。
- 等待中的 CLI 退出或超时，不等于任务取消。
- 服务重启后，未领取任务保留；原处理中任务要么按已验证机制恢复，要么明确标记中断失败，不能笼统承诺自动断点续跑。
- `cancel/retry` 在基础任务契约稳定后实现。运行中取消须等待安全边界，不能只把数据库状态改成取消而工作仍继续写入。

任务结构直接按新模型设计，不迁移旧任务记录，也不保留将 `CHUNK_ADD`、`CHUNK_UPDATE` 等负载编码到 `error_message` 的旧格式兼容分支。新模型创建的任务仍须满足上述持久化、结果可追溯和重启恢复要求。

### 5.5 配置

统一校验并返回变更的生效方式：

- 立即生效。
- 需要重启。
- 需要重新索引或其他数据操作。

尤其不能把更换嵌入模型/维度等同于旧向量已自动适配。

- 服务运行时，配置修改通过服务完成，避免磁盘配置和进程缓存分裂。
- 初始配置或停机维护允许显式离线操作，但不得在 API 连接失败后自动回退为离线写配置。
- 离线修改前确认实例未占用相关资源；端口暂时连接失败不等于服务已经停止。
- `config show` 默认脱敏；敏感值通过标准输入或受控输入文件提供，不要求写入命令行历史。

---

## 6. CLI 功能范围与契约

### 6.1 P0：核心工作流

命令名为建议，统一命名在 M0/M3 定稿。避免为同一能力同时维护多套同义命令。

| 命令组 | 首批能力 | 关键结果 |
|---|---|---|
| `status` / `doctor` | 实例状态、本地环境、组件和依赖诊断 | 知道连的是哪个库、缺什么、能做什么 |
| `config` | 初始化、脱敏查询、更新、连接/可用性测试 | 不打开设置页也能配置使用 |
| `file` | 导入、列表、详情、创建空笔记 | 文件 ID、重复项、任务 ID |
| `search` | 查询、文件/集合过滤、候选限制 | 候选卡片 ID、标题、来源信息 |
| `chunk` | 列表、详情、新增、标题/正文更新、批量新增 | 稳定 ID、正文、写入任务及结果 |
| `collection` | 列表、创建、归类 | 可发现和组织知识范围 |
| `task` | 列表、详情、批量查询、等待 | 进度、终态、失败项、产物 ID |

查询和业务命令要求服务运行。帮助、版本、本地诊断、显式离线配置等例外应逐条列明，而不是提供通用的直写数据库回退模式。

### 6.2 P1：主要维护能力

- 文件重新索引、原件和 Markdown 导出；需要时同时导出引用资源。
- 文件/卡片单个与批量删除，支持影响范围预览和确认。
- 集合改名、删除、批量归类；文件属性更新。
- 召回诊断，例如分词、三路召回、融合统计、耗时和过滤范围。
- 原页/插图获取，以文件或资源结果表达，不建设终端并排预览界面。
- 云同步状态、主动同步及结果查询；沿用现有同步范围，不引入多端 SQLite 合并。
- 日志过滤、故障定位和客户端 MCP 配置生成。
- 在可安全定义的范围内增加任务取消/重试。

### 6.3 统一输入输出

必须支持：

- 人类可读输出，以及显式 `--json` 的机器输出。
- JSON 模式 stdout 只承载命令结果；日志、诊断和进度写 stderr。
- 中文路径和内容使用明确的 UTF-8 编解码。
- 长正文/批量数据通过文件或标准输入传入，不要求大量 shell 转义。
- 列表分页、结果数量控制；检索默认不返回整个知识库正文。
- 帮助和版本信息可在未初始化知识库时查询。
- `--wait` 和有界等待超时，由 CLI 统一实现，不由 skill 重写轮询脚本。

建议沿用现有 `success/message/data` 基础结构，在新 API/CLI 中明确结构化错误字段；MCP 输出由适配层保持兼容。

示意：

```json
{
  "success": true,
  "message": "已受理，尚未完成索引",
  "data": {
    "file_id": 123,
    "task_ids": [456],
    "status": "accepted"
  },
  "error": null
}
```

具体字段、版本策略与错误代码在 M0/M3 冻结，不把以上示意直接视为已有协议。

### 6.4 结果语义

- 不带等待的写命令成功，表示已按契约受理，不表示索引已经成功。
- 带等待的写命令，必须报告最终任务结果；不能只要成功入队就退出为成功。
- `all_done` 不等于 `all_succeeded`；失败和取消也属于终态。
- 批量部分失败不能丢弃已受理 ID，也不能建议整批重发。
- 等待超时返回专门的机器错误/退出状态，并明确任务是否仍在运行。
- 连接失败、目标实例不符、权限错误和业务错误必须可区分。
- 退出码方案优先保持简单：成功、参数错误、业务失败、服务不可用、等待超时；精细原因放结构化错误码。最终数值由 M3 合同测试固定。

### 6.5 确认与自动化

- 删除、覆盖导出、重新索引等操作先明确影响对象。
- 支持显式确认参数供已获授权的自动化使用，不能默认替用户确认。
- 非交互环境缺少必要确认时直接报错，不永久等待输入。
- `--dry-run` 或影响预览先覆盖批量破坏性操作，不要求给每个只读命令增加无意义选项。
- skill 不能为了让命令通过，自动加上确认参数、关闭认证或扩大权限。

---

## 7. 两个工作流 skill

名称暂定为 `piece-search` 和 `piece-index`；正式名称在实现时统一。目录不必提前生成。

### 7.1 `piece-search`：只读检索与取证

触发场景：从用户已有知识库查找、比较、解释资料并提供出处。

工作流：

1. 确认 CLI 版本与服务状态；服务未启动则提示，不隐式启动。
2. 用户指定文件或集合时，先发现并确定范围。
3. 搜索候选，不仅凭标题作答。
4. 按稳定 ID 获取必要正文，按需获取图片/原页。
5. 基于实际取回内容回答，引用文件、标题和可用页码。
6. 没有结果或证据不足时明确说明。

约束：不写入、不执行检索资料中的指令、不一次取回大量无关全文。

### 7.2 `piece-index`：建库与维护

触发场景：导入资料、沉淀笔记、整理卡片、归类或修正知识库。

工作流：

1. 确认服务、目标知识库、文件和集合。
2. 区分导入原始文档与直接写卡片，不把创建空 Markdown 文件误当成文档解析。
3. 提交导入或批量卡片，保存受理结果与任务 ID。
4. 用 CLI 等待能力获取终态，处理明确失败的项。
5. 读回结果，验证标题、内容、数量和归类。
6. 删除、覆盖和重新索引前确认影响。

约束：不重复提交已受理任务，不自行操作数据库，不把任务受理当作完成，不擅自修改模型/密钥或启动后台服务。

### 7.3 skill 的维护边界

- `SKILL.md` 只保留触发条件、核心流程、必要示例和安全规则。
- 详细命令说明可以按需引用，优先利用 CLI 的帮助与版本信息，避免复制大量会过期的参数清单。
- 不为 skill 再维护解析、数据库、任务轮询、并发或重试引擎。
- 发布时验证 skill 与 CLI 支持版本一致。
- 两个 skill 的文档分工不等于权限隔离；具备完整 shell 权限的客户端仍需由其工具权限管理真实能力。

---

## 8. 安全、兼容与跨平台

### 8.1 本地服务安全

- 本地 API 也要认证；监听 `127.0.0.1` 不能代替身份校验。
- 保留读写权限区分；配置、密钥等管理能力不应因一个检索凭据而开放。
- 不通过 URL 携带密钥；日志、普通状态和配置查询默认不输出秘密。
- CLI 生成含密钥的客户端配置时，必须是显式请求，不能混入诊断日志。
- GUI 浏览器请求需考虑来源校验和 CSRF；不能让其他网页借本机服务执行写入。
- 原件、工作文件、图片和页图的获取应有受控边界，不把无鉴权的任意静态目录当作新的业务 API。
- 导入路径由用户明确提供并校验；按 ID 修改/删除知识库对象，不提供任意文件删除接口。

### 8.2 新库初始化与接口兼容

- 本轮以新库为起点，不要求旧知识库内容、历史任务或旧 ID 保留；不建设旧 schema 读取兼容、旧库迁移脚本及迁移专用备份/恢复流程。
- 数据库和任务结构可以直接按目标模型设计，重点验证空库初始化、约束和索引建立，以及新写入数据的读写和重启恢复。
- 开发与测试优先使用独立的临时数据目录，按新模型生成样例，不准备旧库升级样例。
- “无需旧数据兼容”不等于每次启动都可以清库。新版本产生的数据必须正常持久化；遇到不支持的已有结构应明确提示，不静默删库或猜测兼容。
- 如开发中需要重建当前库，仍须确认目标、停止占用服务并持有必要锁；若发现重新导入或新写入的数据，不得以本文件的空库前提直接覆盖。
- 现有有效配置和密钥不视为已删除，不自动清空、轮换或改用另一数据目录。
- MCP 保留服务名、既有工具名和参数/结果语义；新增字段尽量可选。接口兼容与旧知识库数据兼容是不同要求。
- 保留现有 CLI 公开命令和便携 exe 行为。内部 helper/窗口入口的打包路径也要回归。
- 新版本投入使用后的后续结构升级，按届时实际数据情况另行制定策略，不为本轮预建旧库迁移框架。

### 8.3 平台和依赖

- Windows、macOS、Linux 的业务命令语义一致。
- 平台差异集中在路径、进程、锁、系统转换器和可选桌面能力。
- GUI 可选不等于解析无系统依赖：LibreOffice、SQLite 扩展加载能力等仍须诊断和验证。
- 核心/CLI 的直接运行依赖要显式声明，不能偶然依赖 GUI 包传递安装。
- 保持单项目、统一版本，可选依赖的具体名称在 M5 定稿。
- 原有完整安装作为兼容形态保留；新增精简安装必须实际验证不是从源码目录偷加载缺失模块。

---

## 9. 分阶段开发计划

每个里程碑必须产生可运行、可验证的结果。允许代码调整期间逐个替换入口，不允许长时间同时维护两套业务规则。

### M0：目标、契约和回归基线

- [x] 核对本规划，冻结显式服务模型、空知识库前提与本轮非目标。
- [x] 盘点 GUI、16 个索引 MCP 工具、3 个检索 MCP 工具的实际行为；数量以实施时版本为准。
- [x] 确定 CLI 命令命名、连接目标规则、JSON/错误契约和对外接口兼容策略。
- [x] 确定配置初始化与显式离线修改的边界。
- [x] 准备空库初始化及按新模型创建的文件、卡片和任务样例，使用不含用户秘密的临时数据目录。
- [x] 补齐必要的业务行为和对外接口回归基线，记录现有失败与未验证项；不要求旧知识库数据兼容测试。
- [x] 确定正式测试和规划文档的版本控制范围。

验收：能明确列出哪些行为必须保留、哪些是新能力、哪些仍未验证；后续重构可对比基线，而不是靠肉眼判断。

### M1：独立核心运行时

- [x] 把核心资源的启动/停止所有权从 GUI 启动流程中抽出。（`tests/automated/test_runtime.py` 8 项：启停顺序、停止失败不关池、锁保留）
- [x] 让 GUI、MCP 可选启用，核心不依赖其导入和安装。（`tests/automated/test_install.py`：精简安装无 nicegui/fastmcp 包时核心服务可运行）
- [x] 保留数据库独占锁、单一写入方、helper 隔离及安全收尾。（同库第二实例 `INSTANCE_BUSY`、停止后无 processing 残留）
- [x] 整理公共平台/配置依赖，保持轻量 CLI 帮助与内部子进程入口。（未配置不建目录、轻量命令不初始化）
- [ ] 验证 NiceGUI 挂载方式、当前锁定版本的生命周期，以及窗口/托盘退出如何通知核心。（服务级挂载与生命周期已由 `test_full_mode.py` 验证；窗口/托盘退出与浏览器内 WebSocket/上传/下载交互未实机验证）
- [ ] 从本阶段开始运行三系统基础和无显示会话测试。（仅 Windows）

验收：无 GUI/MCP 的核心服务可运行；完整桌面模式仍可用；关窗不停止任务；服务退出不遗留使用数据库的工作。（前三项后半与最后一项已验证；"关窗不停止任务"未实机验证）

### M2：共享业务和任务收拢

- [x] 提取完整导入用例，统一查重、存储、登记、归类与入队。（`tests/automated/test_business.py`）
- [x] 提取重新索引、配置变更、批量卡片和必要导出用例。（`test_business.py`、`test_reliability.py`：重索引预览校验、导出 ZIP 含资源）
- [x] 统一任务类型、输入、结果、错误及同文件冲突处理。（`test_business.py` conflicts 用例）
- [x] 确定并实现需要的任务去重与中断恢复规则。（请求键去重与重放幂等、停止后 pending 保留/processing 清零）
- [x] 用薄适配替换 GUI/MCP 中的业务编排，保持外部兼容。（`test_migrated.py` 迁入的 MCP/GUI 合同）
- [x] 按新模型建立数据库和任务结构并验证空库初始化；不实现旧库/旧任务迁移，重启恢复测试针对新模型创建的数据。（`test_reliability.py` 空向量表重建、`test_service_process.py` 重启断言）

验收：同一业务从不同入口调用获得一致结果；不存在只有 GUI 或某个 MCP 工具才执行的关键保护规则。（API/CLI/MCP 三入口已验证；浏览器级 GUI 交互未实机验证）

### M3：本地 API 与 P0 CLI

- [x] 实现受控本地接口、实例身份确认、认证和版本检查。（错误对端不发凭据、目标不符拒绝）
- [x] 实现 P0 命令与一致的文件/标准输入、JSON、退出码、分页和等待行为。（`test_cli.py` 41 项：退出码矩阵、UTF-8、确认提示 stderr、超时、Ctrl+C）
- [x] 服务未运行、端口错误或目标库不符时明确失败，不启动服务、不直写数据库。（`SERVICE_UNAVAILABLE` 退出码 3、未配置不建目录）
- [x] 增加命令级合同测试与跨入口真实服务测试。（`test_service_process.py` 真实进程闭环）

验收：不打开 GUI 即可完成“配置 → 导入 → 等待 → 检索 → 取正文”，以及“创建笔记 → 批量写卡片 → 等待 → 读回验证”。（`test_service_process.py` 两个闭环测试）

### M4：维护能力与两个 skill

- [x] 补齐 P1 中优先的重新索引、导出、删除、集合和属性维护。（reindex 只读预览、`--with-resources` 导出、删除 `--dry-run`、集合维护）
- [x] 接入召回诊断、同步、日志和 MCP 配置生成。（`search --diagnostics`、`logs`、`mcp-config` 命令合同；`test_sync.py` 内存 WebDAV 替身）
- [x] 在安全语义明确后增加任务取消/重试。（仅取消未领取任务；重试幂等去重；服务层与 CLI 合同均有测试）
- [x] 制作两个薄 skill，使用已稳定的 CLI，不通过附加脚本弥补缺失业务能力。（`skills/piece-search`、`skills/piece-index` 已重新打包）
- [ ] 用正常、失败、超时、部分受理和未启动服务场景验证 skill。（skill 引用的 CLI 行为已由测试覆盖，但未做真实 AI 客户端场景验收）

验收：真实工作流可完成；skill 不重复写入，不把未完成任务报成完成，不擅自启动服务或执行危险操作。（CLI 工作流已验证；skill 场景验收未做）

### M5：接口兼容、打包与发布验收

- [x] 验证空库安装初始化、现有配置/密钥保护、既有 MCP 客户端和 CLI/桌面入口；不执行旧知识库数据兼容验收。（`test_install.py` 空库初始化、`test_reliability.py` 旧配置补写保留有效密钥、`test_migrated.py` MCP 合同）
- [x] 验证完整安装与精简安装，以及仓库外的已安装入口。（`test_install.py`：仓库外 venv、无偷加载、doctor 健康、核心服务启停）
- [ ] 在 Windows/macOS/Linux 实机验证锁、路径、转换器、服务停止和必要桌面能力。（仅 Windows）
- [ ] 验证真实外部依赖、慢网络、大文件和原生组件异常，不将替身测试当成实机结果。（嵌入/WebDAV 仍是替身）
- [x] 更新 README、CLI 帮助、skill 兼容说明和本文件进度。
- [ ] 在用户授权后进行真实自启、外部服务调用及制品发布操作。

验收：安装后的程序与源码环境具有相同核心能力；每个平台和分发形态有明确的通过、失败或未验证记录。

---

## 10. 最低验收矩阵

| 类别 | 必测情形 |
|---|---|
| 显式启动 | 服务未运行时业务命令失败，且没有产生新服务进程、知识库或隐式配置写入 |
| 目标识别 | 错误端口、其他应用、其他知识库、版本不兼容不被误认成目标服务 |
| 核心独立 | 无 GUI/MCP 依赖时核心与 CLI 可用；帮助不初始化数据 |
| 跨入口 | CLI 写入后 GUI 可见、MCP 可检索；GUI/MCP 修改后 CLI 可读取 |
| 导入 | 重复内容、同名不同内容、并发重复导入、中文/空格路径、无转换器、部分失败 |
| 任务 | 受理后断连、CLI 等待超时/Ctrl+C、服务中断、批量部分失败、同文件并发、缺失任务 ID |
| 新库初始化 | 空数据目录建库、约束和索引建立、新任务结构正确、重启后新写入数据保留 |
| 数据保护 | 删除确认、导出覆盖保护、重索引失败保护、卡片原页关联，不包含旧库/旧任务迁移 |
| 配置 | 脱敏、输入秘密不进日志、生效分类、运行中与离线更新互斥、嵌入变更边界 |
| 权限 | 检索凭据不能写、未认证不能调用管理接口、静态资源和浏览器写入边界 |
| 生命周期 | 关窗不停止任务；关闭服务先排空/停止工作再关数据库；helper 被正确回收 |
| 分发 | 仓库外安装运行、可选依赖缺失、PyInstaller 内部入口、三系统路径与进程行为 |
| skill | 只取必要正文、引用真实来源、正确等待、不整批重发、不擅自启动服务或确认破坏性操作 |

单元测试、替身服务、模拟平台分支、真实进程集成、外部依赖测试和实机验收必须分别记录，不能互相冒充。

---

## 11. 本轮不做

- 旧知识库数据兼容、旧 ID 保留、旧 schema/任务负载兼容桥、旧库迁移及迁移专用备份/恢复验收。
- 业务 CLI 隐式启动/复用失败后自动新建服务、闲置自动退出。
- CLI 每条命令独立启动完整索引运行时，或直接连接数据库绕过服务。
- 远程多用户管理、账户系统、云端托管、跨机器共享 SQLite。
- Redis/Celery、分布式 Worker、独立向量库。
- 为架构统一重写整个 GUI，或建设复杂 TUI 来复刻 GUI。
- 目录监控、网页采集等本轮之外的新业务。
- 通用插件平台、消息总线、命令总线、复杂 profile 框架。
- 为 skill 建第二套业务实现，或把 skill 文档当作权限隔离。

后续确有需求时单独立项，不混入本轮核心解耦与 CLI 建设。

---

## 12. 实施前仍需定稿的工程细节

这些可以在对应里程碑内由开发根据代码与测试确定；若改变用户体验、兼容承诺或安全边界，再回到用户确认。

1. 核心包与兼容入口的最终目录组织，不先承诺全仓一次性改名。
2. 无 GUI/无 MCP 开关、完整安装与精简安装的默认启用行为。
3. 本地 API 路由、实例身份、凭据存储和既有端口映射。
4. JSON/退出码最终契约，以及 CLI、API、MCP 的版本兼容范围。
5. 新数据库/任务模型、请求去重、重索引结果发布及失败恢复实现，不包含旧字段/旧数据迁移。
6. 可选依赖名称、正式测试和规划文档纳入 Git 的具体范围。

### 文档维护要求

每个里程碑完成后追加：变更范围、真实验证命令、运行环境、结果、未验证项及下一步。只有实际验收通过的项目才勾选，不以“代码已写”代替“功能已验证”。

M0 已修改 `.gitignore`：只放开本规划和 `tests/automated/`，其余本地 `doc/*`、`tests/*`、`.claude/` 和用户数据仍忽略。新增交付 skill 位于 `skills/`。截至本次交接，所有变更均未暂存、未提交；不要强制加入其他本地文档、手工测试、worktree 或用户数据。

## 13. 首次暂停时的实施记录（2026-09-08，历史快照）

> 本节保留首次交接时的实现和测试记录。**当前状态以第 14 节为准**：13.1、13.3、13.5、13.6 中“运行时尚未合入”等描述已有变化，不要据此再次覆盖主树；未明确完成的业务边界与旧回归失败仍须处理。

### M0 冻结的工程合同

- 管理 API：`/api/v1`，复用 8689；业务 CLI 只连接既有服务。`serve --no-gui --no-mcp --no-tray` 明确只运行核心。完整安装默认启用 GUI/MCP，缺可选依赖时明确要求安装 extra 或关闭入口，不静默猜测。
- 目标：配置目录与 `data_path/kb.db` 的规范化路径共同生成 target ID；先握手确认 Piece/API v1/目标，再发送凭据，业务请求继续校验目标。CLI 读取已有配置不创建目录。
- API/CLI 返回 `success/message/data/error`，错误含 `code`；退出码 0 成功、2 参数错误、1 业务/权限/协议失败、3 服务不可用、4 等待超时。受理、终态成功、部分失败独立表达。
- 新 schema v1：任务 `task_type/input/result/error` 分离，非空请求键去重，同文件排队；重索引与其他写入冲突时拒绝。未领取任务保留，重启时 processing 标记中断；不迁移旧任务或旧库。
- 配置初始化和 `config update --offline` 必须显式执行并取得配置锁及数据库锁；API 失败不自动离线写。配置查询默认脱敏；敏感输入走 UTF-8 文件/stdin。旧配置/密钥保留。
- 只将本规划和 `tests/automated/` 的正式测试放开忽略；其他本地 doc/tests/.claude/用户数据仍忽略。两个交付 skill 放 `skills/`，不公开本机工具设置。
- 现有契约盘点：索引 MCP 16 工具，检索 MCP 3 工具；保留工具名、批量 50 项/200000 字符限制、部分受理 ID、检索标题/图像/来源语义。
- Windows 10、现有 uv Python 环境，基线命令：`uv run --no-sync pytest tests/test_platform_cli.py tests/test_autostart.py tests/test_mcp_batches.py tests/test_mcp_servers.py tests/test_mcp_auth_config.py tests/test_chunk_images.py -q` → **234 passed，169 个第三方弃用警告，27.39s**。此前一次命令使用了不存在的测试文件名，退出码 4，未运行测试；以上为纠正后的实际结果。
- 未纳入离线运行：真实 VLM、硬编码用户文档路径的手工脚本；未验证 macOS/Linux 实机、制品、自启和外部模型服务。旧本地 smoke 仍有 14 工具的过时断言，新合同测试按实际 16 个验证。

### 13.1 本次暂停点与里程碑状态

用户要求先更新进度、另开会话继续。本节是接续入口，不是发布完成记录。

| 阶段 | 当前状态 | 不能当作已经完成的部分 |
|---|---|---|
| M0 | 已完成基线盘点、合同选择和正式测试范围确认 | 后续改造的全部验收不包含在基线通过数中 |
| M1 | 核心运行时实现位于独立 worktree，**尚未合入主工作区** | 主服务集成、NiceGUI 实际挂载、关闭顺序与可选安装 |
| M2 | 主工作区已实现共享业务、新任务结构和暂存发布；有新测试通过 | 扩大回归仍有失败，GUI/配置兼容和异常恢复仍需补齐 |
| M3 | 主工作区已有 API、HTTP 客户端和 CLI 初稿 | CLI 与 API 有已知不一致，未跑真实服务完整工作流 |
| M4 | 维护服务与两个 skill 已编写；skill 格式检查和打包通过 | CLI 参数/等待尚未稳定，skill 场景验收未完成 |
| M5 | 未开始完整验收 | README、uv.lock、仓库外安装、三系统实机及发布均未完成 |

**不要直接把当前主工作区用于真实知识库。** 主树 `app/server.py` 仍是旧 NiceGUI 启动实现；新 CLI 已向它传入 `with_gui/with_mcp`，目前接口尚未接上。优先处理 M1 合入和下面的 CLI 阻塞项。主树分支仍为 `main`，基线 HEAD 为 `ad87066eede359a115d0ac24678c0c500bf376f9`，没有本轮提交。

### 13.2 已写入主工作区的实现

以下为代码状态，只有 13.4 中明确列出的测试算实际验证。

- **数据库与任务**：`indexing/database.py`、`indexing/repositories/task_repository.py`、`indexing/services/task_service.py`。
  - `PRAGMA user_version=1`；遇到不支持的已有结构报错，不迁移或删除旧库。
  - 任务独立保存 `task_type/input_json/result_json/request_key/error_code/error_message`；新增 `staged_chunks`、`files.working_dirty`。
  - 新增/更新卡片任务请求键去重，同文件任务顺序领取；`file_index` 与其他活跃写任务冲突时拒绝。
  - 仅取消 pending；只重试无已发布结果的 failed/cancelled，重试也有去重键。
  - `get_db_cursor` 不再隐式初始化连接池，调用方必须由核心显式初始化。
- **文件业务**：`indexing/services/file_service.py`。
  - 共用导入快照、哈希查重、文件名/大小/转换器检查、受控副本、归类及事务入队；异常补偿只清理本次副本。
  - 新建笔记、重新索引、导出来源定位及受控路径校验。
  - 明确变化：文件有活跃任务时暂不允许删除，需等待或取消未领取任务；不再仅把运行中的任务标成取消后立即删文件。
- **卡片与发布**：`indexing/services/chunk_service.py`、`indexing/services/processor.py`、`indexing/worker_manager.py`。
  - 批量校验/逐项受理下沉，保留部分受理的所有 task ID；标题与正文可一起更新，并保留原页 heading 关联。
  - 卡片写入和任务完成结果在同一事务发布，工作 Markdown 副本通过 dirty 标记补偿重建。
  - 文件解析先写 `.staging/task-ID` 与暂存表，成功后发布到 `files/working/.generations/task-ID/`；DB 中索引、工作路径和任务结果一次切换。失败保留旧索引。
  - 保留“删除最后一张卡片同时删除文件及原件副本”的语义；批量删除对重复 ID 去重。
- **配置与维护**：新增 `indexing/services/config_service.py`、`maintenance_service.py`、`errors.py`，修改 `indexing/settings.py`、集合/属性/同步服务。
  - 新管理密钥 `api.admin_key` 与 MCP 凭据分离；已有 MCP 密钥不自动轮换。旧配置缺 API 节时会补写配置，因此不再保证旧 JSON 文件逐字节不变。
  - 原子保存、默认脱敏、显式离线锁、生效分类；运行中的 `data_path` 等重启项不立即替换进程有效配置。
  - 当前简化边界：有已索引内容时拒绝直接切换嵌入模型/维度/地址，不宣称旧向量自动适配；需明确选择空库等后续操作。
  - 删除影响预览、属性/集合维护、原页/图片、同步状态/触发、日志和 MCP 配置生成已有服务/API 接入；尚未全部 CLI 验证。
- **共享检索**：新增 `retrieval/service.py`；`retrieval/tools/resolve_keywords.py` 只保留薄导出兼容入口。
  - CLI/API 返回稳定 `chunk_id/file_id`、标题、文件名及页码候选，不默认返回全部正文；支持召回诊断。
  - 明确收紧：指定范围无匹配或交集为空时返回空结果，不能回退到全库。现有 MCP 工具名保留，但这一范围行为不再沿用原来的宽松回退。
- **入口适配**：新增 `app/api.py`、`app/client.py`，扩大 `app/cli.py`；GUI handlers 与 MCP tools 已部分改为调用共享业务。
  - API 使用 `/api/v1`、握手 target ID、Bearer 的 read/write/admin 分权，拒绝跨来源与错误 Host。
  - GUI 安全目前实现为浏览器原生 Basic 登录（用户名 `piece`，密码为配置中的 `api.admin_key`），成功后发 HttpOnly/SameSite=Strict cookie；尚未与真实 NiceGUI 全流程验收。
  - GUI 卡片及召回视图已适配按当前工作文件所在代目录解析图片。注意新代目录对云同步/导出资源的影响仍需验证。
- **正式测试和 skill**：新增 `tests/automated/{conftest.py,test_business.py,test_api.py}`；`skills/piece-search/SKILL.md`、`skills/piece-index/SKILL.md`。
  - 两个 skill 只包含薄工作流，无自建数据库/解析/轮询脚本；初始化脚本生成的示例占位文件已删除。
  - 本地打包产物在被忽略的 `dist/skills/piece-search.skill` 和 `piece-index.skill`；它们的格式通过不等于 CLI 工作流已通过。

### 13.3 必须保留的独立 worktree

通过 `git worktree list --porcelain` 可再次核对位置。两个 worktree 的修改都没有提交，**不能直接按某个 commit cherry-pick，也不要先清理 worktree**。

1. **核心运行时待合入**：`.claude/worktrees/agent-ae30502ae57e68e1c/`
   - 新增 `app/runtime.py`、`app/gui.py`。
   - 修改 `app/server.py`、`app/tray.py`、`app/mcp_servers.py`、`pyproject.toml`、`Piece.spec`。
   - 设计：FastAPI/Uvicorn 主服务；配置锁后数据库锁；Runtime 外层包裹 GUI lifespan；MCP 可选降级；托盘通过回调退出，GUI 关闭不拥有 Worker。
   - 最终 worktree 已去掉 NiceGUI 3.3.1 `ui.run_with` 不支持的 `host/port/reload` 参数；托盘和自动开窗放入 `on_ready`；GUI 即使未 `--open` 也设置开窗回调。
   - 依赖调整只在此 worktree：基础显式声明 FastAPI 0.141.1、Uvicorn 0.52.4 和 PyYAML，GUI/MCP 转成 `gui/mcp/desktop` extras；**主树 pyproject.toml 和 uv.lock 尚未调整**。
   - 协作实现报告自身 compile/import/lifespan smoke 通过，但没有与主树 `app/api.py` 和新业务层组成完整服务。主代理只做了阅读/差异检查，不能把报告当作主树集成验收。
   - 合入前继续审查：锁所有权有重复的 `_lock_already_held` 分支；关闭异常不应吞掉后继续关库；readiness/MCP 运行后退出状态需准确；NiceGUI `app.shutdown()` 使用的 `Server.instance` 绑定需实际确认。可简化无实际调用者的兼容函数，不必原样接收全部代码。
2. **CLI 来源快照**：`.claude/worktrees/agent-a7d3d5d7f20dea41e/`
   - `app/cli.py`、`app/client.py` **已经复制到主树**。
   - 来源快照之后主树又做了三项小修改：reindex 的 source 默认改为自动选择；解析器命令 `file content` 改名 `file export`；移除 `task retry --request-id` 参数。
   - 这些修改尚未完成对应分派调整。**不要用旧 CLI worktree 再覆盖主树**；以后以主树为准修复。

### 13.4 本会话真实验证记录

环境：Windows 10，复用项目 `.venv`，`uv run --no-sync`。已安装版本核对为 NiceGUI **3.3.1**、FastAPI **0.141.1**、Uvicorn **0.52.4**、FastMCP **2.14.4**、Starlette **1.6.0**、pytest **9.1.1**。已查 NiceGUI 官方集成说明，并读过本地 3.3.1 的 `ui_run_with.py`；不能用最新文档签名替代此版本。

| 验证 | 实际结果 | 范围/限制 |
|---|---|---|
| M0 六文件旧回归基线（见上面的完整命令） | 234 passed，169 warnings，27.39s | 改造前行为基线；不是新 CLI 完成证明 |
| `uv run --no-sync python -m compileall -q app indexing retrieval` | 通过 | 当时主树语法检查；发生在 CLI 合入及若干后续编辑之前 |
| `uv run --no-sync pytest tests/automated -q` | **18 passed，1 warning，4.04s** | 临时 SQLite、确定性嵌入替身、真实 ASGI TestClient；不是实际 Uvicorn 进程/外部模型/真实 GUI 测试 |
| 扩大既有回归（下列命令） | **18 failed，235 passed，156 warnings，43.72s** | 失败尚未全部解决，不能声称全量通过 |
| 两个 skill 的 `package_skill.py` | 格式校验通过，各生成一个 `.skill` | 只包含各自 SKILL.md；未验证实际命令工作流 |

扩大回归命令：

```bash
uv run --no-sync pytest tests/automated tests/test_page_render.py tests/test_office_images.py tests/test_chunk_images.py tests/test_office_convert.py tests/test_parser_helper.py tests/test_task_subscription.py tests/test_mcp_auth_config.py tests/test_mcp_servers.py tests/test_mcp_batches.py -q
```

这次 18 个失败的分类：

- `test_page_render.py` 6 项：旧夹具把工作文件放在受控 `files/working` 之外，新 `managed_path` 校验拒绝；应把正式夹具改到临时受控目录，不能撤掉路径保护。新的原页关联用例已在 `tests/automated/test_business.py` 验证。
- `test_task_subscription.py` 2 项：仍传 `error_message="CHUNK_ADD|..."` 或调用旧的 `cancel_tasks_for_file`；需要按新任务/安全取消语义更新。
- `test_mcp_auth_config.py` 5 项：旧配置逐字节不变的断言；3 项 GUI 表单测试只有内存设置、未初始化磁盘配置；非法 MCP 端口环境变量原先回退的断言。最后一项的回退行为已恢复，但**没有重跑验证**。补写兼容 index key 不应仅因表示形式变化就要求重启，已调整生效分类，也未重跑。
- `test_mcp_batches.py` 5 项：故障注入还 patch MCP 包装层而不是共享业务；仍创建无 file_id/旧负载任务；仍期待同文件多个 file_index 直接并发入队；旧 `CHUNK_ADD|...` 执行兼容断言。必须保留公共批量结果/部分受理验证，但不要恢复本轮明确取消的旧任务兼容分支。

**重要：最后一次绿色测试之后还有修改。** 包括 GUI 代目录图片解析、配置生效分类、管理凭据分离校验、禁止隐式初始化连接池、新建文件补偿保护、暂存发布流式遍历、CLI 合入及三项参数调整。交接时没有重新运行整套测试，也未进行任何新核心真实进程验收。

skill 打包命令（实际执行，使用已有 uv 环境）：

```bash
PYTHONUTF8=1 uv run --no-sync python .claude/skills/skill-creator/scripts/package_skill.py skills/piece-search dist/skills
PYTHONUTF8=1 uv run --no-sync python .claude/skills/skill-creator/scripts/package_skill.py skills/piece-index dist/skills
```

旧 `tests/test_vlm_client.py`、`test_large_pdf_conversion.py`、`test_markitdown_converter.py` 含外部服务或硬编码用户文档路径，不要直接跑 `pytest tests`。旧夹具未全部遵循新的配置/受控路径边界，后续先完善隔离再扩大测试；不要假定所有本地历史脚本都不会触及默认配置目录。

### 13.5 已知阻塞与待修正合同（先处理，不要当成可用功能）

**A. CLI 与 API 的明确不一致**

1. `app/cli.py` 已有等待 helper，但**没有注册/分派 `piece task wait`**；两个 skill 已引用它，必须补齐。
2. `file export` 解析器已经改名，但 `_api_command` 仍检查 `operation == "content"`。需统一为 `export`，并支持规划示例的输出目录/文件名选择；目前下载只接受已有父目录内的明确文件路径。
3. CLI `file reindex` 和 `sync run` 会发送 `dry_run`，当前 API schema 不接受；应真正实现相应只读预览，或移除不支持的参数，不能原样发送。`task retry` 仍发送 `request_key`，而 API 只接受 `task_id`，应删去多余字段，复用服务端重试去重。
4. 普通 `file list` 会发送 `collections=[]`，API 把它解释为空范围，导致没有结果。无过滤应发送 `null`/省略字段。
5. 递归导入在 `resolve()` 后才检查部分路径的 `is_symlink()`，因此可能跟随显式传入的链接或文件链接；需在解析目标前检查。非递归目录跳过及 `os.walk` 的读取错误也需要完整报告，不能静默漏项。
6. 下载接口失败会返回 `success=false`，但 file/page/image 分支仍可能返回退出码 0；批量图片下载最终失败的退出码也需按合并结果计算。
7. `_wait_for_tasks` 对权限等查询失败可能一直等到超时；多批结果、缺失 ID、异常和 Ctrl+C 应及时失败并保留已受理 task IDs。等待还要受总 deadline 限制，不能每次网络请求另等固定 10 秒。
8. 新增/批量请求键虽然生成了，网络结果不确定时还没有完整保留到错误结果；禁止自动重发，补上请求键及查询提示。导入的连接错误被逐项吞成普通业务失败，服务不可用应保持退出码 3。
9. `input()` 的确认提示会进入 stdout，破坏交互 `--json` 的纯 JSON；应将提示写 stderr。正文/stdin/stdout 的 UTF-8、失败时人类输出保留 IDs、参数/业务/服务/超时统一退出码还需命令级测试。
10. `doctor` 目前把缺 NiceGUI/FastMCP 当成整体不健康，需区分核心必需依赖和可选入口；握手的“非 Piece / API 版本不兼容”也应有可区分错误码。`app/client.py` 的未使用兼容别名可以删掉，不必新增多套名称。
11. 离线 `config init/show/update` 已在 `_client_for` 之前处理，这个修复已复制到主树；不要误以为仍须先连接服务。但新会话仍须真实测试锁冲突、无配置初始化和默认脱敏。

**B. 业务/运行时需要继续验证的边界**

- M1 合入后的真实启动/停止、锁互斥、helper 回收、MCP 降级，以及 GUI cookie/Basic 认证与 NiceGUI WebSocket/上传/下载是否兼容。
- 新工作代目录与云同步现有递归/路径逻辑；同步的原子下载、部分失败结果不应假报成功；导出 Markdown 的引用资源尚未成套导出。
- 离线改变空库向量维度后，已有空 vec 表与新配置在下一次启动如何一致；异常回滚/重启后的 `.staging` 与未引用代目录清理。
- GUI 删除/新增等 handler 对新的 FILE_BUSY 校验是否正确展示错误；设置 GUI、在线配置与待重启配置之间是否存在覆盖或缓存分裂。
- 各 API 未找到对象不能返回 `success=true,data=null`；端口/版本/权限错误和结构化参数校验需要完整的 CLI 合同测试。
- 不要为通过旧测试重新放开任意物理路径、隐式数据库初始化、运行中强行标取消或旧任务负载解码。

### 13.6 新会话建议顺序

1. 阅读本节并检查 `git status`；保留主树全部未提交修改。通过 `git worktree list` 定位待合入运行时，阅读其最终文件后合入/简化，**不要把旧 CLI 快照覆盖回主树**。
2. 先让 `serve --no-gui --no-mcp --no-tray` 与 API 在独立临时目录启动，再修复 13.5 A 的命令合同。此时不要直接使用用户默认知识库，也不要清理任何已存在真实库。
3. 先跑 `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q`，补 CLI 命令级与真实本机服务集成测试，覆盖缺服务不建目录/不拉进程、身份错误、读写权限、中文路径、等待超时/中断和部分受理。
4. 把仍有效的旧接口/GUI 回归按新模型和临时路径更新，纳入已放开的正式测试目录；淘汰明确测试旧负载兼容的断言，记录而不是掩盖回归失败。
5. 验证完整 GUI/MCP 模式与核心精简模式；合入可选依赖后再统一更新 `uv.lock`，使用隔离环境验证仓库外安装，不让本地源码遮蔽缺失包。
6. 对照实际 CLI 修订/验证两个 skill，再更新 README 与本规划勾选。三系统实机、真实外部 API、自启和制品发布另按授权执行，不用替身测试冒充。

截至本次交接，没有提交、推送、发布或真实自启操作；没有以“空库前提”为理由执行清库。所有协作实现已停止，待合入修改保留在上述 worktree 中。

## 14. 第二次暂停与接续入口（2026-09-08，历史快照）

> 本节保留第二次交接的记录。**当前状态以第 16 节为准**：14.3 的临时服务已确认不存在，14.4 A/B 的多数条目已有对应修改但尚未在最终状态下重跑验证。

用户要求先更新文档、另开会话继续。本次只推进到**核心运行时初步接通 + 部分 CLI 合同修补 + 有限真实进程冒烟**，并没有完成 M1–M5 验收。第 9 节未通过完整验收的项目继续保持未勾选。

### 14.1 当前里程碑与工作区

| 阶段 | 本次进展 | 仍未完成 |
|---|---|---|
| M0 | 延用第 13 节的合同和回归基线 | CLI 当前实现出现退出码偏离，需修正，不能把实现错误当成新合同（第 16 节已按合同修正，待重跑确认） |
| M1 | 核心运行时、可选 GUI 挂载、托盘回调及依赖划分已写入主树；无 GUI/MCP/托盘的 Uvicorn 服务实际启动成功 | 正常/异常关闭、请求排空、helper 回收、真实 GUI/MCP、精简安装与跨平台验收 |
| M2 | 既有共享业务测试重跑为 18 项通过；另修了配置脱敏误遮蔽 `max_tokens` | 旧回归失败仍未重跑修复；脱敏最后修改也未重跑；其余 13.5 B 边界仍在 |
| M3 | 补上 `task wait`、修正 export/retry 分派、空集合过滤、下载退出码等；真实 CLI 已能读状态、创建集合和空笔记 | 本节 14.4 的合同缺陷；尚未新增 CLI 合同测试、真实服务自动化测试或完成完整工作流 |
| M4 | 沿用已有维护能力和两个 skill，本次未修改/重新打包 skill | 场景验收、可靠 dry-run、完整导出资源和安全维护 |
| M5 | `pyproject.toml`、`Piece.spec` 已加入运行时/可选入口调整 | `uv.lock`、README、仓库外安装、制品与三系统实机验证均未完成 |

- 当前分支仍为 `main`，HEAD 仍为 `ad87066eede359a115d0ac24678c0c500bf376f9`；本轮全部修改未暂存、未提交，没有推送或发布。
- 主树 `app/runtime.py`、`app/gui.py` 已存在；`app/server.py` 不再是旧 NiceGUI 主入口，已经接受 `with_gui/with_mcp`。
- 两个旧 worktree 仍保留且未清理。运行时 worktree `.claude/worktrees/agent-ae30502ae57e68e1c/` 现在是**实现来源快照**，不再是待整体合入版本；CLI worktree `.claude/worktrees/agent-a7d3d5d7f20dea41e/` 更不能覆盖回主树。继续开发以主树为准。
- 本次尝试的只读 GUI/MCP 协作核对因 API 错误提前结束，**没有可用结论**，不能当作已完成审查。交接时 `git worktree list` 仍只列出主树和上述两个旧 worktree。
- 没有按空库前提清理用户知识库，没有执行真实自启、外部模型调用或云同步。测试配置与样例使用 14.3 的独立临时目录。

### 14.2 本次实际写入的代码

**运行时与装配**

- `app/runtime.py`：持有数据库连接池、Worker/helper 和同步停止流程，记录组件状态及窗口/退出回调。当前配置锁、数据库锁统一由 `app.server.main` 外层持有，已去掉来源实现中重复的 `_lock_already_held` 分支。
- `app/server.py`：FastAPI/Uvicorn 主应用；`build_application` 先建 API，再可选挂载 NiceGUI，并把现有 GUI lifespan 包在核心运行时内；MCP 启动失败记录降级；可选桌面外壳由 `on_ready` 启动。
- `app/gui.py`：延迟导入 NiceGUI，挂载受主 API 中间件保护的 `/working`、`/pages` 和 GUI 根应用；使用已安装 NiceGUI 3.3.1 支持的 `ui.run_with` 参数。没有保留来源 worktree 中无调用者的 `gui.run/startup/shutdown/bind_server` 兼容函数。
- `app/tray.py`：退出通过 `on_shutdown` 回调设置主 Uvicorn 退出，不再导入 NiceGUI；`app/mcp_servers.py` 的信号所有权注释改为主服务。**MCP manager 的请求排空实现本次没有实质修正。**
- `pyproject.toml`：基础依赖显式增加 FastAPI 0.141.1、Uvicorn 0.52.4、PyYAML 6.0.3；NiceGUI/FastMCP/托盘/原生窗口改为 `gui`、`mcp`、`desktop` extras。`Piece.spec` 补入核心/API/GUI/MCP 延迟导入路径。**未同步 lock，也未重新安装或打包验证。**

**CLI 与客户端（以下均是代码改动，不代表场景验收完成）**

- 注册并分派 `piece task wait ID... --timeout N`，复用写命令等待逻辑；`task retry` 不再发送 API 不接受的 `request_key`。
- `file export` 的分派已与解析器同名；下载支持尝试从 `Content-Disposition` 获取目录输出文件名。单文件/page/image 分支按响应成功与否计算退出码，批量图片按合并结果计算。
- 未指定集合时，file list/create/import/search 发送 `null` 而不是误当空范围的 `[]`。
- 递归导入在 `resolve()` 前检查末级符号链接；记录非递归子目录跳过及 `os.walk` 读取错误。导入连接中断开始保留已受理 ID，但不确定项标注仍有错误，见 14.4。
- 等待收到失败 envelope 后立即结束；等待时捕获 Ctrl+C/客户端异常并保留已受理 task IDs；开始限制网络 timeout，但尚未实现严格总 deadline。
- 确认提示改写 stderr，不再通过 `input(prompt)` 污染 JSON stdout；doctor 区分基础与可选依赖。删除了客户端无调用者的 `compute_target_id/HttpClient/APIClient` 别名。
- 临时处理 dry-run：reindex 向服务发未确认请求，把 `CONFIRMATION_REQUIRED` 的影响信息转成预览；sync dry-run 只调用 sync status。**这还不是完整、准确的预览实现。**
- `indexing/services/config_service.py`：脱敏匹配改为字段名后缀，避免把非秘密字段 `max_tokens` 遮蔽成 `***`；此修改发生在最后一次绿色测试之后。

### 14.3 本次真实验证与未清理测试服务

环境：Windows 10，uv **0.11.2**，项目既有 `.venv`，Python **3.12.13**。本次重新核对安装版本：NiceGUI **3.3.1**、FastAPI **0.141.1**、Uvicorn **0.52.4**、FastMCP **2.14.4**、Starlette **1.6.0**。为避免未同步 lock 的依赖调整改变现有环境，执行使用 `uv run --no-sync`。

| 实际执行 | 结果 | 范围与限制 |
|---|---|---|
| `uv run --no-sync python -m compileall -q app` | 通过 | app 语法检查，不是功能验收 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **18 passed，1 warning，4.25s** | 仍是原来的临时 SQLite、嵌入替身及 ASGI TestClient 测试；没有新增 CLI/lifespan 测试；之后又修改了配置脱敏 |
| `piece config init --data-dir <临时目录> --json` | exit **0** | 首次输出脱敏配置，创建 config.json 与锁文件，未创建 kb.db；当时发现 `max_tokens` 被误脱敏 |
| 未启动服务时 `piece status --port 8791 ... --json` | `SERVICE_UNAVAILABLE`，exit **3** | 没有启动服务；当前提示尚需补上显式 `piece serve` 引导 |
| 未启动服务时 `piece doctor --server ... --json` | `DOCTOR_FAILED`，exit **1** | 能报告基础/可选依赖及服务连接失败 |
| `piece serve --port 8791 --no-gui --no-mcp --no-tray --data-dir <临时目录>` | Uvicorn 实际监听成功，核心/API ready | 使用完整已安装环境关闭可选入口，**不是卸载可选依赖后的精简安装验证** |
| 运行中 `piece status ... --json` | exit **0** | 正确返回临时目标、GUI/MCP disabled |
| 运行中 `piece doctor --server ... --json` | 输出前部显示 success=true | 命令输出经过截取，未独立验证完整 JSON 与 piece 自身退出码，不列为完整合同通过 |
| 运行中 `piece open ... --json` | `GUI_DISABLED`，exit **1** | 不自动重启或拉起 GUI |
| 空库 `piece file list ... --json` | exit **0**，files=[]，total=0 | 只验证了空库列表，未验证创建后无过滤列表能发现文件 |
| `piece collection create "论文" ... --json` | exit **0**，collection_id=**1** | 真实 API/SQLite 写入，仅测试样例 |
| `piece file create "项目 笔记" --collection "论文" ... --json` | exit **0**，file_id=**1** | 真实创建空 Markdown 笔记，不是文档导入或索引完成 |

上述 CLI 均为源码入口 `PYTHONUTF8=1 uv run --no-sync piece ...`。服务连接选项为 `--data-dir C:/Users/Admin/.claude/jobs/680fdcd9/tmp/kb1 --port 8791`。

**尚未实际执行成功的验证，不得据命令草稿声称通过：**

- 错误目标/错误端口、只读密钥拒绝写、错误密钥、同库第二服务锁冲突、运行时离线改配置互斥等组合命令被工具权限分类服务故障拦住，未产出这些场景的验证结果。
- 批量写卡片/部分受理/失败等待、缺失任务等待、检索、中文导出和覆盖保护、reindex/delete dry-run、在线配置、日志、MCP 配置等组合命令同样未执行完成。**没有完成“配置 → 导入 → 等待 → 检索 → 取正文”闭环。**
- 没有重跑 13.4 的扩大回归；之前的 18 个失败不能视为已经消失。没有跑全量 `pytest tests`，也不要下一会话直接跑含真实用户路径/外部服务的旧脚本。
- 两个 skill 本次没有重新打包或场景验收，GUI/MCP 没有实际启动，未进行真实外部嵌入/OCR/WebDAV 调用。

**交接时临时服务仍在运行，先处理这个遗留状态：**

- 本次测试目录：`C:/Users/Admin/.claude/jobs/680fdcd9/tmp/kb1`；其中已有上面创建的样例集合/笔记，不能把它称为仍空的目录。
- 管理地址：`http://127.0.0.1:8791`。它不是用户默认知识库，测试凭据保存在该临时目录的 config.json；不要把凭据写进日志、文档或命令行参数。
- 启动脚本：`C:/Users/Admin/.claude/jobs/680fdcd9/tmp/launch_serve.py`；日志 `serve1.log`，PID 文件 `serve1.pid` 均在同级目录。`CLAUDE_JOB_DIR` 并非每次 shell 都有值，下一会话不要盲目用它拼路径。
- 交接时通过 `Get-CimInstance Win32_Process` 重新核对：venv 启动器 PID **20252**，实际 Python/Uvicorn 子进程 PID **20676**，二者命令行均为上述临时目录、端口与精简模式；PID 文件只保存了启动器 PID。
- `os.kill(20252, signal.CTRL_BREAK_EVENT)` 停止尝试失败，报 **`OSError: [WinError 87] 参数错误`**；随后再次核对两个进程仍存在。**没有安全退出通过记录，也没有强杀或删除此目录。** 后续操作前重新确认 PID/命令行/目标，不能照抄旧 PID 或杀全部 Python 进程。
- 曾尝试用 psutil 查看进程，但环境没有该模块；随后已用系统 PowerShell 成功核对，不需要为此加项目依赖。

### 14.4 下次先修的已知问题（不是新产品决策）

**A. 生命周期与资源安全，是当前最优先阻塞**

1. `Runtime._stop_resources` 仍逐步捕获异常后继续关闭连接池，并标记 stopped；`build_application` 也会吞 MCP stop 异常。停止失败时不能假报完成，更不能在仍有工作访问数据库时释放连接池/独占锁。这一问题在合入前已指出，本次尚未修好。
2. 主服务与 MCP Uvicorn 均设置 `timeout_graceful_shutdown=5`。本次已读取安装版本的 `uvicorn/server.py`：超时会取消请求任务，然后进入 lifespan shutdown，**不会先等待所有被取消任务真正结束**。而 Piece 的 `run_sync` 取消时会继续等待同步线程，因此必须补真实在途写请求的收尾测试，避免关库早于线程结束。
3. 已读取 NiceGUI 3.3.1 的 `run.py`：`tear_down()` 对 thread pool 使用 `shutdown(wait=False, cancel_futures=True)`。仅把 NiceGUI lifespan 放在核心内层，并不能证明 GUI `run.io_bound` 线程已经停止。还需验证 GUI 回调、WebSocket/上传/下载、窗口/托盘退出及工作线程排空。
4. 核心 ready 目前在 Worker 启动后就置 true；MCP 状态只在启动时采样，不跟踪服务运行后退出；GUI `app.shutdown()` 与 `Server.instance` 绑定也未解决/验证。完整模式不要直接视为可用。

**B. CLI/client 剩余合同缺陷**

1. **退出码偏离冻结合同**：`main` 当前把 `TARGET_MISMATCH`、`PROTOCOL_ERROR` 也归为 exit 3；M0 约定它们应属业务/协议失败 exit 1。`CONNECTION_FAILED` 还可能指外部配置测试失败，不能一律当本地 Piece 不可用。应统一 envelope 和异常的退出码计算，并用命令测试固定。
2. **等待仍未严格有界**：`max(1.0, min(original_timeout, remaining))` 会越过最后不足 1 秒的预算，多批 `_query_tasks` 还重复使用同一 timeout，握手/业务请求/响应读取也没有共享总 deadline。查询失败虽然停止轮询，但 `_with_wait` 会把权限/协议/缺失 ID 统一包装成 `TASK_FAILED`，丢失精细错误码；多批查询的部分结果和所有任务 ID 也要保留。
3. **新增结果不确定仍会丢请求键**：chunk add/batch-add/update/reindex 的 POST 失败发生在进入 `_with_wait` 之前，当前错误结果没有保存生成的 `request_key` 和查询指引。禁止自动重发，需保存可用于定位的请求键及对象 ID。
4. **导入连接异常错误标注**：`_file_import` 将当前发生断连的 path 放进 `not_submitted`，但它可能已经被服务器受理；应区分“结果未知”和“确定尚未提交”，并列出后续未提交项。保留此前受理 IDs、全部逐项结果，服务连接失败退出 3。不要建议整批重导。
5. **中文目录导出尚有解析问题待测试修正**：`email.message.Message.get_param` 可能把 RFC 2231/5987 的 `filename*` 归一为 `filename` tuple；当前 helper 在 fallback 后只接受 str，没有正确处理这个 tuple。补中文 filename*、恶意路径名、下载 HTTP 失败、断流、目录/显式文件路径和覆盖并发测试；当前先检查 exists 再 `os.replace` 也不具备原子的“不覆盖”保证。
6. **握手错误码未统一**：doctor 自己区分 `NOT_PIECE/VERSION_MISMATCH`，但 `PieceClient.handshake` 仍都抛 `PROTOCOL_ERROR`。应在共享客户端确认 Piece/版本/目标后才发密钥，不在 CLI 重复实现两份校验。
7. **JSON/UTF-8/异常输出**：stderr 确认提示已改，但 stdin/stdout UTF-8 仍依赖调用环境；人类失败输出仍不显示 data 中已受理 IDs。显式离线配置的 `BusinessError` 会落为普通 `CLI_ERROR`，锁冲突的 RuntimeError 还可能直接 traceback。需要正式合同测试，不要只在 shell 设置 `PYTHONUTF8=1` 后声称已支持全部平台。
8. **dry-run 还只是过渡逻辑**：reindex 未确认预览只返回 file_info，没有统一校验 source 是否可用和任务冲突；sync dry-run 实际只读 status，却让用户误以为进行了同步影响检查。选择真正共享的只读预览，或者移除不支持的 dry-run 参数；不能保留误导性成功。
9. **缺失对象与链接**：`chunk/list` 调用的 `get_chunks_paginated` 对不存在文件返回 None，API register 仍可能封装成 success=true。链接检查目前主要覆盖末级符号链接，需检查 Windows junction、祖先路径链接和不可读条目的行为，不静默漏项。
10. **没有 CLI 新测试落地**：`tests/automated` 本次仍只有 conftest/test_business/test_api。以上修改必须补命令级以及本机真实服务测试，不能凭原有 18 项通过判断 CLI 合同稳定。

**C. M2/M4/M5 遗留继续保留**

- 第 13.5 B 的离线空向量维度切换、暂存/未引用代目录恢复、GUI FILE_BUSY 展示、待重启配置保护、原页关联、资源成套导出等还未解决或验收。
- 云同步需要重新核对代目录：本次阅读确认 `_get_local_files` 递归枚举，而 `_get_remote_files` 仍跳过目录，仅取平面文件名，并且抓取异常只日志后返回结果。与 `.generations/task-ID/` 的一致性、原子下载和部分失败不能假成功，仍须补齐。不要实际访问用户云端做验证。
- pyproject extras 已改，但 uv.lock/README/安装说明仍旧；当前环境本身有完整 GUI/MCP，关闭开关的冒烟不等于核心依赖隔离完成。后续保持 `uv run --no-sync` 直到统一更新 lock，避免意外移除可选依赖影响诊断。

### 14.5 建议接续顺序

1. 读本节、核对 git 状态并保留全部未提交修改。**不要重新合入旧 worktree 覆盖当前 runtime/CLI。** 重新核对 14.3 的临时进程并安全处理；以后真实服务测试要自带明确的启动/停止与 finally 清理，避免再次遗留后台服务。
2. 优先修生命周期：先停止接收写入并排空 API/MCP/GUI 工作，再停止同步、Worker/helper，最后关库和放锁；异常不能伪装成功。补正常/异常停止、在途同步写入及同库互斥测试。
3. 修 14.4 B 的客户端/CLI 合同并补 `tests/automated` 测试；以短命本机服务、临时配置和本地嵌入替身完成两个核心闭环，不调用用户外部模型。每个失败都保留已受理 IDs/请求键，检验超时与 Ctrl+C 不取消任务。
4. 按新任务模型/受控临时目录更新仍有效的旧回归，再验证真实 NiceGUI/MCP 模式。官方文档已查，但以已安装 NiceGUI 3.3.1 的签名和收尾实现为准，不用新版本行为推定本版本。
5. 最后统一 uv.lock、README、skill 和安装验证，再更新里程碑勾选。三系统实机、外部服务、自启和发布继续单独列为未验证/需授权项。

本次暂停后没有继续修改业务代码或执行更多业务测试；本文件是当前交接记录。

## 16. 第三次暂停与接续入口（2026-09-09，历史快照）

> 本节保留第三次交接的记录。**当前状态以第 17 节为准**：16.5 的第 1、2、4 步已执行完成，第 3 步的服务级验证已完成（浏览器实机交互仍属未验证项）。

用户要求先更新文档、另开会话继续。本次集中处理第 14.4 节的生命周期与 CLI 合同缺陷，并补齐 13.5 B 的多项业务边界；同时把旧回归中仍有效的合同迁入 `tests/automated/`。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库。

### 16.1 先核对的事实

- 第 14.3 记录的临时服务（PID 20252/20676、端口 8791）在本会话开始时已不存在：`Get-CimInstance Win32_Process` 无匹配进程，`Get-NetTCPConnection -LocalPort 8791` 无监听。无需再处理。
- 分支仍为 `main`，HEAD 仍为 `ad87066`；全部修改未暂存、未提交。两个旧 worktree 未动，仍不要用它们覆盖主树。
- 本会话中期 `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` 曾达到 **65 passed**（含新增 `test_runtime.py`、`test_cli.py`）。**此后又修改了大量文件并新增 4 个测试文件，最后一批修改之后的多次重跑均因工具执行被拦截而没有结果**。下一会话第一件事是重跑，见 16.5。

### 16.2 本次写入的代码（均未在最终状态下验证）

**生命周期与资源安全（对应 14.4 A）**

- `app/runtime.py`：新增 `mark_ready/begin_shutdown/shutdown_failed`、`safe_to_unlock`、`shutdown_error`。`_stop_resources` 改为同步停止失败或 Worker/helper 停止失败时抛 `ExceptionGroup`，**不再继续关连接池**；`stop()` 在已记录停止失败后直接抛错。`start()` 不再自行置 ready，改由 `server.build_application` 在 GUI/MCP 启动或明确降级后调用 `mark_ready`。
- `app/server.py`：`build_application` 捕获入口停止异常后调用 `runtime.shutdown_failed` 并重新抛出，Runtime 内层 lifespan 因此不关库；MCP 状态通过任务 done 回调持续刷新，不再只在启动时采样；主服务改用 `app/asgi_server.py` 的 `DrainingServer`，`handle_exit` 第二次 Ctrl+C 不再置 `force_exit`；`main` 用 `ExitStack` 持锁，停止失败时把锁栈放入 `_retained_locks` 保持到进程退出，并抛 `RuntimeError`；`setup_logging()` 移到取得配置锁之后（避免 `--help`/客户端命令建日志目录）；GUI 模式下把 Uvicorn 实例绑定到 NiceGUI 的 `Server.instance`。
- `app/asgi_server.py`（新增）：Uvicorn 0.52.4 的 `shutdown` 在超时后取消请求但不等待；`DrainingServer.shutdown` 取消后继续 `gather` 直到任务真正结束，再退出 lifespan。主服务和两个 MCP 服务都使用它，`timeout_graceful_shutdown=5` 只决定何时开始取消。
- `app/mcp_servers.py`：`stop()` 把任务异常和 lifespan 停止失败向上抛，不再吞掉；新增 `_RuntimeGate`，核心未就绪或停止中时 MCP HTTP 返回 503；`servers/tasks/stopping` 改为只读属性。
- `app/gui.py`：新增 `drain_threads()`（NiceGUI `run.thread_pool.shutdown(wait=True)`），在 GUI lifespan 退出后、关库前调用；`register` 把 `NICEGUI_STORAGE_PATH` 默认指到数据目录 `.nicegui`。
- `indexing/database.py`：连接池改为条件变量实现，`close_all` 等待所有借出连接归还后才关闭，关闭中拒绝新借用；`init_database` 对已有空 `vec_chunks` 与配置维度不一致时重建向量表，已有 chunks 时明确报错拒绝。
- `indexing/worker_manager.py`：`stop()` 在 helper 回收失败时抛错；启动时先 `recover_file_storage()` 再 `recover_working_files()`；无 Worker 时也停止 helper。
- `indexing/utils.py`：新增 `await_completion`，`run_sync` 基于它实现；GUI handlers、MCP 工具（`indexing/mcp/server.py`、`retrieval/server.py`）、召回视图全部从 `asyncio.to_thread`/`run_in_threadpool` 改为取消安全的 `run_sync`，GUI 上传保存也用 `await_completion`。
- `indexing/services/sync_service.py`：`start()`/`stop()` 配套，重启运行时可重新受理。

**CLI/client 合同（对应 14.4 B）**

- `app/client.py` 重写：握手统一区分 `NOT_PIECE/VERSION_MISMATCH/TARGET_MISMATCH`，目标不符时不发密钥；`time_budget()` 让握手、多批查询和响应读取共享总 deadline，超时抛 `WAIT_TIMEOUT`；用定时器关闭慢滴流的连接；`request_sent` 标记结果是否未知；Content-Disposition 解析改用 `get_params` + `collapse_rfc2231_value`，拒绝路径分隔符、控制字符和 Windows 保留名；下载完整落临时文件后非覆盖模式用 `os.link` 原子发布，`Content-Length` 不符视为失败。
- `app/cli.py`：统一 `_exit_code`（0/1/2/3/4；`TARGET_MISMATCH/PROTOCOL_ERROR/NOT_PIECE/VERSION_MISMATCH` 归 1，`SERVICE_UNAVAILABLE/NOT_READY` 归 3，`INVALID_ARGUMENT/INVALID_INPUT/INVALID_JSON` 归 2）；`_query_tasks` 多批失败立即停止并保留全部 ID 与已得结果；`_with_wait` 保留服务端错误码；新增 `_submit` 在新增/批量/更新/重索引/重试的 POST 失败时保留 `request_key`、对象 ID、`outcome_unknown` 和 `piece task list --request-id` 恢复指引；导入断连区分 `unknown` 与 `not_submitted`；链接检查覆盖祖先目录与 junction；stdin/stdout/stderr 强制 UTF-8；失败时人类输出也打印 data；`Ctrl+C` 统一为 `INTERRUPTED`；`sync run --dry-run` 移除；`file reindex --dry-run` 改为真正的服务端只读预览；`file export --with-resources`；新增 `piece stop`；`status` 在可选入口 degraded/failed 时返回 `DEGRADED`（退出码 1）；`serve` 启动时的 `InstanceBusyError` 以 `INSTANCE_BUSY` 码输出。
- `app/platform.py`：`database_lock` 抛 `InstanceBusyError(code="INSTANCE_BUSY")`。

**共享业务与 API**

- `app/api.py`：`identity` 使用 `runtime.database_path` 并附 `version`；`register` 对 `None` 结果返回 `NOT_FOUND`（修复 `chunk/list` 缺文件返回 success）；`file/reindex` 支持 `dry_run` 并调用 `files.preview_reindex`；`task/list` 支持 `request_key`（含批量子任务前缀）；新增 `POST /api/v1/shutdown`（admin）；导出改为 `export_snapshot` + `ExportResponse`（响应结束后删除临时目录），并增加 GUI 同源路径 `/gui/file/{id}/content`。
- `indexing/services/file_service.py`：新增 `preview_reindex`、`export_snapshot`（可选 ZIP 打包引用资源，缺失或越界资源报错）、`storage_owner`（`files/.instance` 标识）、`recover_file_storage`（只清理本机标识、任务已终态且未被引用的 `.staging`/`.generations` 目录）；`reindex_file` 对重复请求键不重复校验。
- `indexing/services/processor.py`：新工作代目录名改为 `task-ID-<uuid>`，暂存目录写入 `.piece-generation` 标识，避免与云端下载回来的同名目录冲突。
- `indexing/services/sync_service.py` 重写：先完整枚举两端目录树再执行任何写入，枚举失败即整体失败；远端递归枚举按 webdav4 的相对 `name`（不再用 `display_name` 平面文件名）；下载先落 `.sync-` 临时文件、校验大小后用 `os.link` 原子发布；新增上传使用 `If-None-Match: *`；部分失败不更新 `last_sync_time`；错误只记录异常类型；内部标识文件不同步；`_register_downloads` 失败会把结果标为失败。**尚未对真实 WebDAV 服务验证。**
- `indexing/services/config_service.py`：新增 `get_saved_settings`；`initialize_config` 先按磁盘配置确定数据库锁目标。`app/ui/handlers/settings_handlers.py` 改为基于已保存配置生成 diff patch，不再把进程内待重启旧值写回；`webdav.last_sync_time` 保留。
- GUI handlers：删除/新增/批量删除的 `FILE_BUSY` 等 `BusinessError` 改为可见提示，失败项不清空选中；批量删除改走 `maintenance_service.delete_files`；导出改为浏览器下载 `/gui/file/...`，不再直接暴露工作文件路径；任务失败提示不再按 `CHUNK_` 前缀隐藏。
- `indexing/repositories/task_repository.py`/`task_service.py`：`list_tasks(request_key=...)`、`get_task_by_request_key`。`chunk_service.create_chunks` 返回 `request_key`。

**依赖、打包、文档与 skill**

- `Piece.spec` 增加 `app.client`、`app.asgi_server` 隐式导入；`.github/workflows/release.yml` 改为 `uv sync --frozen --extra desktop`（**uv.lock 仍未更新，该工作流在 lock 更新前会失败**）。
- `README.md`：补充精简/完整安装 extras、GUI 登录方式、CLI 退出码与工作流示例、`piece stop`。
- `skills/piece-search/SKILL.md`、`skills/piece-index/SKILL.md`：补充退出码解释、`task list --request-id` 恢复、`unknown/not_submitted` 语义、`--with-resources` 与 `sync run --yes`。**未重新打包 `.skill`，未做场景验收。**

### 16.3 正式测试目录现状

`tests/automated/` 现有：`conftest.py`（新增 autouse 夹具把 `PIECE_DATA_DIR`、`NICEGUI_STORAGE_PATH` 指到临时目录并清除 `PIECE_API_KEY`）、`test_business.py`、`test_api.py`、`test_runtime.py`、`test_cli.py`、`test_reliability.py`、`test_sync.py`、`test_migrated.py`、`test_service_process.py`。

- `test_runtime.py`（8 项，曾通过）：启停顺序、停止失败不关池、MCP 停止失败阻断关库、失败后锁保留、重复取消等待同步线程、重复信号不跳过 lifespan、MCP 管理器上报停止失败。
- `test_cli.py`（曾通过）：错误码与退出码矩阵、错误对端不发凭据、未配置不建目录、轻量命令 UTF-8/不初始化、确认提示走 stderr、非交互缺确认直接失败、多批查询与总超时、慢响应头被 deadline 中断、Ctrl+C 不取消任务、断连保留请求键、导入 unknown/not_submitted、Content-Disposition 安全解析、中文目录下载与并发不覆盖、截断下载不污染、junction/祖先链接检查。
- `test_reliability.py`（**未跑**）：连接池关闭等待借出连接、离线改维度后启动重建空向量表、恢复只清理本机标识目录、重索引不与下载目录冲突、reindex 预览校验、缺文件 chunk/list 为 NOT_FOUND、请求键查询不泄露输入、导出 ZIP 含资源且删除后仍可读、越界资源拒绝、旧配置补写管理密钥保留有效密钥、脱敏不遮蔽 `max_tokens`、GUI 保存不覆盖待重启配置、GUI FILE_BUSY 可见、DrainingServer 排空后才退出 lifespan。
- `test_sync.py`（**未跑**）：内存 WebDAV 替身覆盖递归上传/下载、枚举失败不当空目录、部分失败不推进标记、原子下载、首次同步不覆盖、日常同步只删缺失项、越界路径拒绝。
- `test_migrated.py`（**未跑**）：从 `tests/test_task_subscription.py`、`test_mcp_batches.py`、`test_mcp_auth_config.py`、`test_page_render.py` 迁入的有效合同，已按新任务模型和受控路径改写；明确淘汰 `test_legacy_chunk_payload_still_runs`、`cancel_tasks_for_file`、`CHUNK_ADD|` 负载和逐字节配置不变断言。旧文件本身未删除，仍在被忽略的 `tests/` 下。
- `test_service_process.py`（**未跑**）：启动真实 `piece serve --no-gui --no-mcp --no-tray` 子进程（临时目录、本进程内 OpenAI 兼容嵌入替身），覆盖 导入→等待→检索→取正文→导出 闭环、笔记→批量卡片→部分受理→等待→读回、错误密钥/目标不符/第二实例/离线配置互斥、`piece stop` 后无 processing 任务残留。fixture 在 finally 中 `piece stop` 并等待退出，超时才 kill 并让测试失败。

### 16.4 已知未完成与风险

1. **最终状态未验证**：16.2 的全部修改在最后一次成功测试之后完成，可能存在导入错误或断言不匹配。尤其注意：`app/api.py` 的 `identity` 改用 `runtime.database_path`，`tests/automated` 中的替身 runtime 已补该属性；`test_migrated.py` 的 GUI 旧密钥测试改为直接写磁盘旧配置后 `reload_settings()`。
2. `uv.lock` 未更新；`uv sync` 会移除当前环境中的 GUI/MCP 包（它们已改为 extras）。继续使用 `uv run --no-sync`，或在确认后执行 `uv lock` 并 `uv sync --extra desktop --group dev`。仓库外安装、精简安装和 PyInstaller 制品均未验证。
3. 真实 NiceGUI/MCP 完整模式、WebSocket/上传/下载、托盘退出仍未实机验证；`gui.drain_threads` 与 `Server.instance` 绑定只做了静态阅读。
4. 云同步重写只在内存替身上设计，未对真实 WebDAV 验证；`If-None-Match: *` 依赖服务端支持。
5. `test_service_process.py` 的第二实例测试依赖 `serve` 对 `InstanceBusyError` 输出 `INSTANCE_BUSY`，且 `serve` 在端口占用检查后才取锁；启动失败路径中 `setup_logging` 现在在配置锁之后执行，锁冲突时不会创建日志目录，这是预期行为。
6. 旧 `tests/*.py` 未更新也未删除；扩大回归时不要直接跑 `pytest tests`。
7. 第 9 节里程碑勾选未变：M1–M5 均未通过验收。

### 16.5 下一会话建议顺序

1. `uv run --no-sync python -m compileall -q app indexing retrieval tests/automated`，然后 `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q --deselect tests/automated/test_service_process.py`，逐个修复失败；不要为了通过而放宽受控路径、恢复旧负载兼容或改回吞异常关库。
2. 单独运行 `uv run --no-sync pytest tests/automated/test_service_process.py -q -x`（会启动并停止真实服务子进程，使用临时目录）。若 fixture 报告服务未安全停止，优先查 `serve.log` 中的 `[Shutdown]` 记录。
3. 通过后再验证完整模式：`piece serve --no-tray --port <临时端口> --data-dir <临时目录>`，浏览器 Basic 登录、上传、导出、关窗、Ctrl+C，观察日志中的排空与关库顺序。
4. 更新 `uv.lock`，在隔离环境验证 `pip install .`（精简）与 `.[desktop]`；重新打包两个 skill 并按 7.1/7.2 场景验收。
5. 只有对应验收通过后，再勾选第 9 节的条目并更新本节。

## 17. 第四次会话执行记录（2026-09-09，历史快照）

> 当前状态以第 18 节为准。

按 16.5 的顺序执行：重跑正式测试、完整模式服务级验证、更新 uv.lock、仓库外安装验证、重新打包 skill，并按实际验证结果勾选第 9 节。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库。

### 17.1 环境与核对事实

- Windows 10，uv 0.11.2，项目 `.venv`（Python 3.12.13）；NiceGUI 3.3.1、FastAPI 0.141.1、Uvicorn 0.52.4、FastMCP 2.14.4。
- 分支 `main`，HEAD 仍为 `ad87066`；两个旧 worktree 未动，继续不要用它们覆盖主树。
- 第 14.3 的临时服务在第三次会话已确认不存在，本次无需处理；本会话所有服务进程均由测试 fixture 或带 finally 清理的脚本管理，无遗留。

### 17.2 真实验证结果（全部为本会话实际执行）

| 命令 | 结果 | 说明 |
|---|---|---|
| `uv run --no-sync python -m compileall -q app indexing retrieval tests/automated` | 通过 | 16.2 全部修改后无语法错误 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q --deselect tests/automated/test_service_process.py` | **188 passed** | 16.3 中标记未跑的 test_reliability/test_sync/test_migrated 全部通过 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated/test_service_process.py -q -x` | **4 passed** | 两个核心闭环 + 凭据/目标/第二实例互斥 + 停止无残留，真实服务子进程 |
| 完整模式验证（后转为 `tests/automated/test_full_mode.py`） | **4 passed** | 见 17.3；GUI/MCP 挂载、认证、安全停止 |
| `uv lock` | 成功 | 27 增 8 删；基础依赖不再含 GUI/MCP 包，extras 齐全 |
| `uv sync --extra desktop --group dev` | 成功 | 环境同步后全量复跑通过 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q`（最终） | **203 passed，2 warnings，121.20s** | 含本次新增 11 项（test_full_mode 4 + test_install 5 + test_cli 2） |
| 两个 skill 的 `package_skill.py` | 格式校验通过，各自生成 `.skill` | 内容与已验证 CLI 行为一致 |

### 17.3 新增正式测试（均在 `tests/automated/`）

- **`test_full_mode.py`（4 项）**：真实 `piece serve --no-tray` 子进程（GUI+MCP 启用，临时目录、动态端口、MCP 端口避开默认值）。验证：
  - status 就绪且 with_gui/with_mcp 为 True；
  - GUI 无凭据 401+`WWW-Authenticate`、错误密码 401、Basic 正确凭据 200 并发放 `HttpOnly SameSite=Strict` 会话 cookie、cookie 可复用；
  - 跨源 Origin 与错误 Host 被拒（403 `INVALID_ORIGIN`/`INVALID_HOST`）；
  - 两个 MCP 服务 initialize 拿会话后，`tools/list` 对无密钥/错误密钥/跨服务密钥一律拒绝、本服务密钥成功（检索与索引密钥独立；`initialize` 属协议握手不做认证，这是 FastMCP 工具级中间件的既有行为）；
  - fixture finally 中 `piece stop` 安全退出且退出码 0，日志顺序为 Worker 停止 → 连接池关闭。
- **`test_install.py`（5 项，需 `uv`）**：`uv build --wheel` 后在仓库外独立 venv 验证。精简安装：`--version`/`--help` 可用、`import nicegui`/`import fastmcp` 失败（无偷加载）、doctor 退出码 0 且报告两者为可选缺失、`serve --no-gui --no-mcp --no-tray` 就绪并安全停止（退出码 0）。完整安装 `wheel[desktop]`：nicegui/fastmcp 可导入、doctor 健康。
- **`test_cli.py` 新增 2 项合同**：`logs --level/--limit`、`mcp-config --service`（默认不含密钥）、`search --diagnostics` 的服务端 payload；`task cancel/retry` 的提交合同。补齐 M4“接入召回诊断、同步、日志和 MCP 配置生成”与“任务取消/重试”的命令级验证。

### 17.4 其他修改

- `uv.lock`：随 extras 调整更新；`.github/workflows/release.yml` 的 `uv sync --frozen --extra desktop` 在 lock 更新后可解析（CI 实际运行未验证）。
- `dist/skills/piece-search.skill`、`dist/skills/piece-index.skill`：重新打包（该目录仍被 git 忽略）。

### 17.5 仍未验证项（不勾选）

1. **浏览器实机**：NiceGUI WebSocket、上传/下载、关窗不停止任务、Ctrl+C 交互、托盘退出通知核心；GUI 内跨入口可见性（CLI 写入后浏览器内查看）。服务级挂载与 HTTP 认证已验证，交互层未验证。
2. **三系统实机**（macOS/Linux）与无显示会话测试。
3. **真实外部依赖**：真实嵌入服务、真实 WebDAV（云同步重写只有内存替身验证，`If-None-Match: *` 依赖服务端支持）、OCR、慢网络、大文件、原生组件异常。
4. **skill 场景验收**：未用真实 AI 客户端按 7.1/7.2 跑正常/失败/超时/部分受理/未启动服务场景。
5. **自启、制品发布**：需用户授权后执行；PyInstaller 制品未重新构建验证。
6. **`pytest tests` 合并运行**：~~两目录同进程合并运行均在约 63% 处停止~~ **已于 2026-09-10 修复，见第 20 节**（根因是 sse_starlette 的 `AppStatus.should_exit` 类级状态污染，与 `importlib.reload` 或端口单例无关）。日常与 CI 仍建议分目录运行，但合并运行已可用。

### 17.6 下一会话建议顺序

1. 浏览器实机验证完整模式：`uv run piece serve --no-tray --port <临时端口> --data-dir <临时目录>`，Basic 登录（用户名 `piece`，密码为该配置的 `api.admin_key`）、上传、导出、关窗、Ctrl+C，对照日志排空与关库顺序；确认关窗不停止任务。
2. 真实 WebDAV 验证同步重写（用户自己的测试网盘，不碰真实知识库）。
3. 按 7.1/7.2 用真实 AI 客户端做两个 skill 的场景验收，再决定是否勾选 M4 最后一项。
4. macOS/Linux 实机（或 CI 矩阵）跑 `tests/automated`，补三系统记录。
5. 用户授权后：真实自启、PyInstaller 制品构建与发布验收。

### 17.7 tests 目录整理记录（2026-09-09，同日追加）

用户解除了 `.gitignore` 对 `tests/` 的屏蔽，整个测试目录纳入版本控制。本次整理原则：有效测试保留、合同已迁移的旧断言删除、敏感内容脱敏或清除；不为通过测试恢复任何已明确取消的旧行为。

**删除的敏感内容**

- `tests/output/`：手工脚本的解析产物，含用户真实文档（医疗指南、学校文件等）的 PDF 与 Markdown——全部清除，并在 `.gitignore` 新增 `tests/output/` 规则防止再次产生时误入库。

**删除的文件（7 个）**

| 文件 | 理由 |
|---|---|
| `test_task_subscription.py` | 合同全部已按新任务模型迁入 `tests/automated/test_migrated.py`（subscription 夹具 7 项） |
| `test_deadly_overlap.py`、`test_overlap_conflict.py` | 0 个 assert 的纯打印诊断脚本；模块级 `TextIOWrapper` 包装 stdout 会破坏 pytest 捕获（曾导致整批收集崩溃）；边界保护行为由 `test_chunker_protection.py`（5 项）与 `test_table_chunking.py`（8 项）正式覆盖 |
| `smoke_mcp_app.py` | 14 工具断言已过时（现为 16）；隔离启动真实服务的行为由 `test_service_process.py` 与 `test_full_mode.py` 更完整覆盖 |
| `smoke_package.py` | 仓库外安装烟测已由 `tests/automated/test_install.py` 完整覆盖 |
| `test_large_pdf_conversion.py` | 一次性性能调研（硬编码用户 Downloads 真实文件名、依赖项目外 psutil、无断言）；PyMuPDF 流式转换的选型结论已固化 |
| `test_markitdown_converter.py` | 一次性分块策略调研（硬编码用户学术文件路径、无断言）；分块行为由 `test_table_chunking.py` 等正式覆盖 |

（注：`test_vlm_client.py` 保留——真实 VLM 解析链路的手工验证工具，从 `.env` 读配置、PDF 路径走命令行参数，无硬编码敏感信息，pytest 不收集；是 17.5 第 3 项真实外部依赖验证的既有工具。）

**删除的过时测试函数（合同已迁移或架构已变，不为通过恢复旧分支）**

- `test_platform_cli.py`：`test_cli_service_options`、`test_cli_open_and_occupied_port_do_not_start_service`、`test_cli_open_reports_request_failure`——旧 `app.server.main`/`X-Piece-CLI` 头合同，新 CLI/API 合同由 `test_cli.py` 与 `test_service_process.py` 覆盖。
- `test_mcp_auth_config.py`：3 个旧配置密钥轮换断言（含已明确淘汰的逐字节不变断言），合同已迁 `test_migrated.py::test_key_selection_and_empty_key_semantics`、`test_gui_form_materializes_legacy_key_and_rotates_one`。
- `test_mcp_batches.py`：5 个函数——4 个合同已迁 `test_migrated.py`（partial acceptance、task statuses、claiming atomic、payload 不泄露），`test_legacy_chunk_payload_still_runs` 断言已明确取消的 `CHUNK_ADD|` 旧负载兼容。保留 31 项通过的批量边界测试（50 项/200000 字符限制等）。
- `test_page_render.py`：`test_chunk_edit_preserves_source_location`（6 参数化）——旧夹具把工作文件放在受控目录外被新 `managed_path` 拒绝，合同已按新模型迁 `test_migrated.py` 同名测试。
- `test_window_lifecycle.py`：`test_service_owns_cli_window_and_closes_it_before_other_resources`——围绕旧 `server.app` 全局与 `server.setup()` 构建；关闭顺序由 `test_runtime.py` 覆盖，`/api/window/open` 新合同为 admin Bearer。
- `test_worker_lifecycle.py`：2 个函数——`test_start_recovers_processing...` 用旧 `create_task` 签名，恢复合同（processing→`INTERRUPTED`、pending 保留、终态不重放）已由 `test_business.py::test_conflicts_cancel_retry_and_restart` 覆盖；`test_sync_stop_skips_next_transfer...` monkeypatch 已在同步重写中删除的方法，新同步停止合同由 `test_runtime.py`/`test_sync.py` 覆盖。
- `test_reindex.py`：2 个导出测试断言旧 `ui.download` 直传文件路径，新实现为 `/gui/file/...` 受控 URL 下载且合同由 `test_reliability.py` 覆盖。

**修复（保留合同的测试按新实现更新）**

- `test_reindex.py`：夹具 mock 从旧 `task_service.create_task` 改为 `file_service.reindex_file`；`test_uploaded_file_warns_about_edits` 恢复通过（覆盖警告合同）。
- `test_rate_limiter.py`：4 个 async 测试改为 `asyncio.run` 驱动的同步测试，不再依赖未安装的 pytest-asyncio；保留手工运行入口。

**整理后验证**

| 命令 | 结果 |
|---|---|
| `PYTHONUTF8=1 uv run --no-sync pytest tests/ -q --ignore=tests/automated --ignore=tests/test_vlm_client.py` | **314 passed**（44.58s） |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **203 passed**（121.20s，17.2 已记录） |
| `PYTHONUTF8=1 uv run --no-sync pytest tests -q`（合并，两次尝试） | 均在约 63% 处停止（进程 CPU 0、无子进程），属合并运行的测试隔离缺陷，详见 17.5 第 6 条；修复前分目录运行 |

`tests/` 最终含 17 个根目录正式测试文件（解析、分块、Office 转换、页面渲染、平台/CLI、自启、窗口/Worker 生命周期、MCP 服务与密钥、批量边界、限流器、重索引提示）加 `tests/automated/` 11 个文件，全部可被 git 跟踪；`tests/output/` 与用户数据仍被忽略。

## 18. 第五次会话执行记录（2026-09-10，历史快照）

> 当前状态以第 19 节为准。

用户确认实施"方案一（开窗一次性引导令牌）+ 方案三（密码可发现性）"，目标：服务发起的 GUI 访问免登录，手动访问保留 Basic 登录，认证强度不降低。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库。

### 18.1 写入的代码

- **`app/runtime.py`**：新增 `BootstrapTokens`（进程内存、60 秒 TTL、单次消费、`secrets.token_urlsafe(32)`）；`open_window` 改为接收 URL 参数，由 API 层生成带令牌地址传入。
- **`app/api.py`**：`LocalSecurity` 新增 `GET /bootstrap?token=` 分支——校验令牌即发放现有 `piece_session` cookie 并 303 跳 `/`，令牌作废；失败回落 Basic（401 + `WWW-Authenticate`），不区分"过期/未知"避免探测。该分支前置 `with_gui` 检查与既有的 Host/Origin/sec-fetch-site 检查共用。`/api/v1/window/open` 在开窗前签发令牌拼入 URL。GUI 响应统一走 `_secure_send`（安全头 + 按需 cookie）。
- **`app/server.py`**：`serve --open` 开窗改为 `{UI_URL}/bootstrap?token=...`；首次运行（配置文件不存在）时日志打印密码所在路径（不打印密钥）；托盘启动传入 runtime。
- **`app/tray.py`**："打开界面"每次签发新令牌开窗；"复制登录密码"菜单项（`pystray.Icon.HAS_NOTIFICATION` 为 True 的后端才显示，Windows 支持、X11 不支持）把 `api.admin_key` 复制到系统剪贴板并经托盘通知反馈结果——通知不再携带密码本体，不留痕。剪贴板实现位于 `app/platform.py` 的 `copy_to_clipboard`（Windows 用 ctypes 原生 API，需显式声明 64 位句柄的 restype/argtypes；macOS 用 pbcopy，Linux 依序 wl-copy/xclip），失败时通知提示到配置文件查看。
- **`app/cli.py`**：`config init` 成功消息附带密码文件路径指引（不打印密钥本身）。
- **`README.md`**：登录说明更新为"服务发起的入口自动免登录，手动访问用 Basic"。

### 18.2 安全边界（与 §8.1 的关系）

- 令牌不是 admin_key 替代品：无派生关系、单次消费、60 秒过期、仅进程内存；admin_key 仍只存在 config.json 与 Bearer 头。
- 令牌会短暂出现在子进程命令行（`--app=URL`）与浏览器地址栏：同用户进程本可直接读 config.json，无新增暴露面；已有 `referrer-policy: no-referrer`、cookie `HttpOnly/SameSite=Strict` 消除常规泄露路径。
- 该设计按 §12 属"安全边界 adjoining 的 UX 决策"，已由用户在本会话明确确认（2026-09-10）。

### 18.3 真实验证（本会话实际执行）

| 命令 | 结果 | 说明 |
|---|---|---|
| `uv run --no-sync python -m compileall -q app indexing retrieval tests/automated` | 通过 | 语法检查 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated/test_api.py -q` | **10 passed** | 新增 2 项：令牌登录/重放拒绝、过期/跨源拒绝 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated/test_full_mode.py -q` | **5 passed** | 新增 1 项：真实服务 `piece open` 成功、`/bootstrap` 未知令牌 401 回落 Basic、Basic 路径不受影响 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **206 passed，2 warnings，135.39s** | 原 203 + 新增 3 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests -q --ignore=tests/automated --ignore=tests/test_vlm_client.py` | **316 passed，45.06s** | 原 314 + 新增 2 项剪贴板合同（Windows 真实写入读回含中文、非 Windows 命令选择回退）；两目录合并运行的既有隔离问题不变（17.5 第 6 条），仍分目录执行 |
| `copy_to_clipboard` 真实往返脚本 | 通过 | Windows 实机复制含中文文本并从剪贴板读回一致（初版被 ctypes 默认 int 截断 64 位句柄的问题已修复并测试固定） |

测试合同要点：令牌首次访问 303+cookie；重放/未知/过期令牌一律 401 Basic；跨源 Origin 到 `/bootstrap` 被 403；GUI cookie 不能调用管理 API；`test_full_mode.py` 保留原 4 项 Basic 认证/来源防护合同不变。

### 18.4 未验证项

1. 浏览器实机：令牌开窗 → 303 → NiceGUI 页面加载的完整链路（含独立 Chromium profile 下 cookie 存续）；托盘"复制登录密码"的通知反馈与剪贴板在托盘回调线程中的实际行为（`copy_to_clipboard` 本身已在主线程实测通过）。
2. 三系统实机：macOS/Linux 托盘（X11 后端无通知能力，菜单项自动不显示；pbcopy/wl-copy/xclip 路径只有命令选择合同测试）；`window_host` 原生窗口传带令牌 URL。
3. skill 无需变更（不涉及登录路径）；`.skill` 包未重新打包（本次未改 skills/ 内容）。

## 19. 第六次会话执行记录（2026-09-10，当前状态）

应用户要求实现 GUI 左栏的 Skill 导出功能：新增"Skill 导出"视图（左栏导航，位于 MCP 配置之后），中栏列出内置 Skill，右栏预览 SKILL.md 并导出到用户指定的本机目录。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库；没有改 skills/ 下的 Skill 内容。

### 19.1 写入的代码

- **`app/skills.py`（新增）**：skill 资源定位与导出。`skills_dir()` 依次尝试 `sys._MEIPASS/skills`（PyInstaller 制品）与 `app/` 上级的 `skills/`（源码树）；`list_skills()` 扫描 `*/SKILL.md` 并解析 frontmatter 的 name/description（解析失败回退目录名，不阻断）；`read_skill()` 拒绝含路径分隔符的 ID；`export_skills(skill_ids, target_dir, overwrite)` 以 `<目标>/<skill_id>/SKILL.md` 形式**按字节复制**（不重编码），目标目录不存在自动创建，已存在默认列入 `exists` 拒绝覆盖，`overwrite=True` 才覆盖，缺失 ID 列入 `missing`，返回 `{"exported", "exists", "missing"}` 供 UI 分支提示。
- **`app/ui/views/skill_view.py`（新增）**：中栏 Skill 清单（复用 MCP 客户端列表布局，含描述截断行）；右栏常显导出区（目录输入框、导出当前/全部按钮，未选中也可全部导出）+ 选中 Skill 的 SKILL.md 正文 Markdown 预览（frontmatter 剥离后经 `components.chunk_markdown` 渲染——复用切片正文的 `chunk-content` 紧凑排版与主题变量，标题/正文/代码字号与文件库切片一致，避免默认 Markdown 大标题破坏整体风格）。导出走 `run_sync` 不阻塞事件循环；`~` 展开、首尾引号剥离；`exists` 非空时 `confirm_dialog` 确认后以 `overwrite=True` 重发。`SKILLS` 为模块级常量（安装期资源，运行期不变）。
- **`app/ui/views/sidebar.py`**：新增"Skill 导出"导航按钮（`extension` 图标）与 `switch_to_skills` 回调参数。
- **`app/ui/pages.py`**：`current_view` 增加 `'skills'`；页面级状态 `selected_skill`、`skill_export_state`（导出目录切走再切回保留）；`select_skill` 回调与中/右栏渲染分支。
- **`app/i18n/locales/zh.json`、`en.json`**：新增 `sidebar.skills` 与 `skills.*`（标题、空态、导出提示、覆盖确认等 17 键）。
- **`Piece.spec`**：datas 加入 `(skills, 'skills')`，制品内导出可用（制品未重新构建，见 19.4）。
- **`tests/automated/test_skill_export.py`（新增 8 项）**：清单解析与目录缺失容错、frontmatter 缺失回退、非法/缺失 ID 读取拒绝、导出自动建目录且字节一致、默认拒覆盖与显式覆盖、`~` 展开与 missing 报告、`skills_dir` 源码树回退。

### 19.2 设计要点

- 导出目标限定为**服务所在主机的本机目录**（AI 客户端读取的是本机 skills 目录，如 `~/.claude/skills`），目录输入框 hint 已注明；浏览器"另存为"式下载不适配目录形式导出，未实现。
- 覆盖默认拒绝、显式确认后才覆盖；导出为字节复制，保证与仓库内置资源逐字节一致（无换行符转换）。
- 视图/交互模式复刻 MCP 配置视图（已上线模式），未引入新组件库或测试基建。

### 19.3 真实验证（本会话实际执行）

| 命令 | 结果 | 说明 |
|---|---|---|
| `uv run --no-sync python -m compileall -q app tests/automated` | 通过 | 全部修改无语法错误 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated/test_skill_export.py -q` | **8 passed** | 新增合同 |
| 渲染冒烟：`Client(page)` 上下文内调用 `render_skill_middle/right`（选中态 + 空态） | 通过 | 58 个元素创建无异常；`skills_dir`/清单/全部 i18n 键实际读出验证 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **214 passed，2 warnings，117.54s** | 18.3 基线 206 + 新增 8，无回归 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests -q --ignore=tests/automated --ignore=tests/test_vlm_client.py` | **316 passed，43.14s** | 根目录全绿。与 18.3 记录的 314 相差 2：工作区存在第五次会话遗留的未提交修改（`README.md`、`app/platform.py`、`app/tray.py` 的剪贴板复制密码实现及 `tests/test_platform_cli.py` 新增 2 项剪贴板测试），本会话未触碰这些文件，全部保留 |

### 19.4 未验证项

1. **浏览器实机**：视图切换、目录输入、导出按钮点击、覆盖确认弹窗、导出结果的交互层（仍属 17.5 第 1 条范围）。
2. **PyInstaller 制品**：spec datas 已加 `skills`，但制品未重新构建验证 `sys._MEIPASS/skills` 定位与导出。
3. **真实导出落地**：未向真实 AI 客户端 skills 目录（如 `~/.claude/skills`）导出验证可用性；测试全部使用临时目录。

## 20. 第七次会话执行记录（2026-09-10，历史快照）

用户测试发现官方云 PaddleOCR 的 job 接口支持图片扭曲矫正等解析参数（`optionalPayload` 字段透传），要求把可用参数做成设置：内核（共享业务层）建模参数，GUI 设置页暴露常用项。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库。

### 20.1 参数来源考证（两套 API 的关系）

- 项目用异步 job 接口 `POST /api/v2/ocr/jobs`，用户给的 `https://ai.baidu.com/ai-doc/AISTUDIO/Cmkz2m0ma` 是同步 `/layout-parsing`；两者共享同一套解析参数——job 接口把同步请求体包在 **`optionalPayload`** 里（multipart 时为 JSON 字符串字段），`fileUrl`/`pageRanges`/`batchId` 是 job 层自有字段。权威字段表为 PaddleX serving schema `paddlex/inference/serving/schemas/paddleocr_vl.py` 的 `InferRequest`（官方云服务按此接收）。
- 模型选择：`PaddleOCR-VL`、`PaddleOCR-VL-1.5`（官方 SDK 现默认文档解析模型）、`PaddleOCR-VL-1.6`、`PP-StructureV3`、`PP-OCRv5`/`PP-OCRv6`。本地下拉框补 `PaddleOCR-VL-1.5`，保留自由输入（with_input）适配自建服务。

### 20.2 写入的代码

- **`indexing/settings.py`**：新增 `OcrOptionalPayload`（`use_doc_orientation_classify`/`use_doc_unwarping`/`use_chart_recognition`/`use_seal_recognition`/`use_ocr_for_image_block` 均 `Optional[bool]` 三态——None 不发送由服务端默认；`markdown_ignore_labels` 列表；`extra_payload` 开放字典透传采样类参数）。`to_payload()` 转 camelCase 并剔除 None。`OcrSettings` 增 `payload` 字段。`visualize` 固定 `False`（项目不消费可视化图，官方文档明确"请求和配置均未设置时默认返回"——显式关闭减小 JSONL）。
- **`indexing/services/ocr_client.py`**：`submit_job` 增 `optional_payload` 参数，multipart `data` 里以 JSON 字符串携带（与官方 SDK 行为一致）；`iter_ocr_pdf_pages` 从配置读 `to_payload()` 结果传入，所有批次统一携带。
- **`indexing/services/config_service.py`**：`_merge` 对 `ocr.payload.extra_payload` 跳过已知字段递归校验（开放参数表整体替换）——否则 CLI 永远无法写入透传参数。
- **`app/ui/views/settings_view.py`**：PaddleOCR 表单新增「解析参数」区块——五个三态下拉（跟随服务/开启/关闭）、版面标签过滤输入框（逗号分隔）；模型下拉补 `PaddleOCR-VL-1.5` 并放开自由输入。
- **`app/ui/handlers/settings_handlers.py`**：表单字符串 ↔ 三态布尔互转（`_payload_form_value`/`_form_payload_bool`/`_build_ocr_payload`），`init_settings_form` 与 `save_settings_form` 双向接入。
- **`app/i18n/locales/zh.json`、`en.json`**：新增 12 键。
- **`tests/automated/test_ocr_payload.py`（新增 4 项）**：camelCase 转换与 None 剔除、config_service 嵌套 patch 往返（含 extra_payload 透传）、GUI 三态映射、submit_job 的 JSON 字符串格式与不传时字段不出现。

### 20.3 设计要点

- **三态语义**：None（不发送字段，服务端默认）与 False（显式关闭）有实际区别——官方示例显式传 false 提速说明云端默认可能是开的；默认"跟随服务"不改变既有用户行为。
- **分层暴露**：常用 5 开关 + 标签过滤进 GUI；采样类（temperature/topP/repetitionPenalty/minPixels/maxPixels/layoutThreshold 等）经 `extra_payload` 走 CLI/config.json——GUI 不堆进阶项，多入口各取所需。
- **旧配置兼容**：config.json 无 `payload` 字段时 pydantic 默认实例生效，`to_payload()` 返回 `{"visualize": False}`；考虑到"完全不传"与"只传 visualize:false"的服务端行为等价性未验证，提交时统一显式传（对自建服务更稳）。
- 明确不暴露：`restructurePages`/`mergeTables`/`relevelTitles`（破坏逐页页对齐——processor 按页数校验）、`useLayoutDetection`（版面还原是项目根基）。

### 20.4 真实验证（本会话实际执行）

| 命令 | 结果 | 说明 |
|---|---|---|
| `uv run --no-sync python -m compileall -q app indexing tests/automated` | 通过 | 全部修改无语法错误 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated/test_ocr_payload.py -q` | **4 passed** | 新增合同（首次运行抓到 `_merge` 拒绝 extra_payload 透传键的真实缺陷，修复后通过） |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **218 passed，2 warnings，122.24s** | 19.3 基线 214 + 新增 4，无回归 |
| `PYTHONUTF8=1 uv run --no-sync pytest tests -q --ignore=tests/automated --ignore=tests/test_vlm_client.py` | **316 passed，43.13s** | 根目录全绿，GUI/CLI 合同未受影响 |

### 20.5 未验证项

1. **真实云服务**：`optionalPayload` 各开关对官方云的实际效果（仅按官方文档与 PaddleX schema 实现，未对 `paddleocr.aistudio-app.com` 实测——尤其 `markdownIgnoreLabels` 的可用版面标签值需实测确认，文档示例为 `header,footer`）。
2. **浏览器实机**：设置页新增区块的渲染与交互（三态下拉、标签输入、保存）未实机操作（仍属 17.5 第 1 条范围）。
3. **服务端默认值**：云端对未传字段的默认行为（如 `useDocUnwarping` 是否默认开）无官方明示，"跟随服务"档位的实际效果取决于服务端。
4. **`visualize: false` 等价性**："完全不传 optionalPayload"与"只传 visualize:false"在自建 PaddleX 服务上的行为差异未验证（自建服务可能按配置文件默认返回可视化图，显式 false 更稳）。

## 21. 第八次会话执行记录（2026-09-10，当前状态）

用户要求排查并修复 tests 目录中存在的问题。定位并修复了 17.5 第 6 条悬置的"两目录合并运行 `pytest tests` 挂死"缺陷。**没有提交、推送、发布、真实自启或外部服务调用**；没有清理用户知识库。

### 21.1 根因（faulthandler 全线程栈定位）

- 挂点：合并运行至 `tests/test_mcp_batches.py::test_registered_tools_and_input_schemas`（约 63%，即根目录 MCP 系测试的第一项）时 `asyncio.run(check())` 收尾永等——栈显示 docket Worker 的 `_scheduler_loop` 卡在 redis `call_with_retry`，进程 CPU 0。
- 真正根因不在 MCP 测试自身：**`sse_starlette` 在 import 时把 `uvicorn.Server.handle_exit` 猴子补丁为 `AppStatus.handle_exit`**（`sse.py` 末尾），该包装置位**进程级类属性** `AppStatus.should_exit = True` 且从不清除。`tests/automated/test_runtime.py::test_repeated_signals_do_not_skip_lifespan` 只是调用了 `server.handle_exit(SIGINT, None)`（为验证重复 Ctrl+C 不跳过 lifespan 的合同），便污染了进程状态。
- 后续同进程内所有 `sse_starlette.EventSourceResponse`（MCP `mcp/server/streamable_http.py` 直接使用）被误判"服务器正在关闭"，POST 通道的 SSE 流立即进入告别排空，fastmcp 客户端 `list_tools()` 永不返回。两目录分开跑各自全绿是因为根目录单独跑时该污染测试未被先行执行。
- 二分过程印证：`test_runtime.py` 单文件 + 挂点测试即可 100% 复现；进一步最小化到"构造 `uvicorn.Server` + 调用一次 `handle_exit(任意信号值)`"；复位 `AppStatus.should_exit = False` 后挂起消失（多次运行验证）。

### 21.2 修复

- **`tests/automated/test_runtime.py`**：`test_repeated_signals_do_not_skip_lifespan` 用 `try/finally` 包裹，结束时复位 `AppStatus.should_exit = False`，并留注释说明机制（sse_starlette 的 import 时补丁、类级状态、污染后果）。测试断言与合同本身不变。

### 21.3 真实验证（本会话实际执行）

| 命令 | 结果 | 说明 |
|---|---|---|
| `PYTHONUTF8=1 uv run --no-sync pytest tests -q -o faulthandler_timeout=180`（合并，修复后） | **535 passed，153 warnings，160.72s** | 历史 17.5 第 6 条的合并挂死消除；此前两次复现均挂死在 45-300s |
| `PYTHONUTF8=1 uv run --no-sync pytest tests/automated -q` | **219 passed，124.51s** | 分目录基线不回归（19.3 的 214 + 第七次会话新增 test_ocr_payload 5 项） |
| `PYTHONUTF8=1 uv run --no-sync pytest tests --ignore=tests/automated -q` | **316 passed，45.91s** | 根目录基线不回归 |
| 反向顺序烟测（挂点测试在前 + test_runtime.py 在后） | **9 passed** | 顺序无关性抽验 |

### 21.4 其他观察（未改动）

- `tests/test_vlm_client.py` 是手工脚本（读 `.env`、命令行传 PDF 路径），pytest 收集为 0 项——17.7 已明确"保留"，仅注意文档记录根目录命令时曾习惯性加 `--ignore=tests/test_vlm_client.py`，实际不忽略也只是 0 项收集，无碍。
- 工作树另有第七次会话未提交的 OCR optionalPayload 修改（`test_ocr_payload.py` 等），本会话未触碰，全部保留。
- 日常与 CI 仍建议分目录运行（更快定位、历史惯例）；合并运行已可作为兜底手段。

## 22. 第九次会话执行记录（2026-09-10，当前状态）

用户要求排查打包版（dist exe）两个 MCP 子服务启动失败并修复，其余部署分发议题整理成文档。**没有提交、推送或外部服务调用**；工作树中第七次会话的未提交修改全部保留。

### 22.1 打包版 MCP 修复（已实施并验证）

- 根因：fastmcp 2.14.4 lifespan 无条件创建 Docket，其默认 `memory://` 后端动态导入 fakeredis 与 lupa；PyInstaller 未收集 `lupa.lua51`（.pyd 动态导入）与 `fakeredis/commands.json`（包数据），两个 MCP ASGI 应用 lifespan 崩溃。源码环境不受影响。完整分析见 `doc/打包版MCP启动失败原因分析.md`。
- 修复：`Piece.spec` 补 `collect_data_files('fakeredis')` 与 hiddenimports（`docket`/`fakeredis`/`lupa`/`lupa.lua51`）。仅改打包配置，未触及 Python 代码，`tests/automated/` 未重跑（下次代码改动合并打包时顺带回归）。
- 重建：用户清空 dist/ 与 build/ 后完整重打包（`uv run --no-sync python build.py`），制品 `dist\Piece_v0.1.0_20260910.zip`（178.8 MB）。旧 `dist\Piece\data`（当日测试数据）随清空删除，属用户有意放弃；新制品首启生成全新数据目录与密钥。
- 验证：`piece status` 显示 mcp 及 retrieval/index 双 ready；协议级 E2E（fastmcp Client + Bearer 密钥）retrieval 列出 3 工具并实调 `list-collections` 成功，index 列出 16 工具。

### 22.2 部署分发结论（记录，未实施）

产出 `doc/部署分发与入口策略.md`，要点：入口分层（人→CLI、agent→MCP、SKILL.md→纯策略文档）；skill 转向 MCP 的工具面缺口清单与实施顺序；开发者全局 CLI（`uv tool install -e ".[desktop]"` + `PIECE_DATA_DIR` 对齐）方案待实施；`build.py` 清空整个 dist 目录需先移出 data 的运维坑。

同日后续讨论产出 `doc/Windows分发修复与多端分发实施方案.md`（**方案已定，未实施**）：skill 导出时注入当前实例的 CLI 调用前缀（可执行文件 + `--data-dir` + `--port`）、新增控制台入口 `piece-cli.exe`、`build.py` 数据目录守卫、wheel 携带 skill 与 `piece skill` 子命令、三端通用的 `uv tool install` 路线。实测已确认：打包 exe 在管道下可作 CLI 但在 cmd/PowerShell 直接执行时不可用；wheel 目前不含 `skills/`；`uv tool install` 不读项目 `uv.lock`；PyPI 名 `piece` 已被占用。下一会话按该文第 7 节顺序实施，第一步是把日常服务搬出 `dist\`。

## 15. 参考

- 上一轮规划：`doc/架构改造方向.md`。
- 当前 CLI 和数据目录说明：`README.md` 的“源码运行与 CLI”。
- NiceGUI 官方集成说明：<https://github.com/zauberzeug/nicegui/blob/main/nicegui/llms.md> 中的 `ui.run_with`。该链接指向上游当前文档；项目当前锁定 NiceGUI 3.3.1，实施时必须核对已安装版本及生命周期，不用上游最新示例代替当前版本验证。
