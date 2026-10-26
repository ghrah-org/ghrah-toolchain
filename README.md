# ghrah-toolchain

**已知的限制：**
- `update` / `stop` 目前无法检查 Agent 是否仍在工作，可能在 Agent 循环中造成中断。
- 面向开发者的 `dev` 已可用，`create` 等其余分发命令仍在实现中。

九夏／ghrah 的独立工具链 CLI：提供安装与配置、源码组合的离线校验、构建，以及实例的后台启停与日志查询。

需要 Python **3.15.1 或更新版本**。推荐使用 uv 进行开发与构建；前端构建还需要 pnpm 和 Node ≥22。工具链自身的 Python 版本与被部署发行版的 Python 环境分别管理。

**从源码仓库安装／更新需要系统 Git**，以及构建所需的 uv、pnpm 和 Node。Git 不需要成为所有用户命令的依赖：已安装实例的 start/stop/status/logs 直接使用安装产物。缺少 Git 时源码安装会给出提示，`self doctor` 可查看依赖路径；工具链不自动安装系统 Git。

## 安装本工具链并安装九夏

目前仅支持从源码开始。先克隆本仓库并安装依赖：

```sh
git clone https://github.com/ghrah-org/ghrah-toolchain
cd ghrah-toolchain
uv sync
uv run ghrah --help
```

需要写 `~/.ghrah` 的命令（`install`／`start`／`dev`）请用装好的 `ghrah`，而不要用 `uv run ghrah`：

```sh
uv tool install --editable .   # 入口 ~/.local/bin/ghrah，工具 venv 在安装域外
ghrah --version
```

`uv run ghrah` 使用项目内 `.venv` 的解释器；在受限环境（沙箱、受控终端等）下它只能写工作区，写 `~/.ghrah` 会报 `Permission denied`——`dev` 起服失败时会直接给出这条提示。

从远程仓库安装九夏（需系统 Git）：

```sh
ghrah distro install generic --from https://github.com/ghrah-org/ghrah-monorepo.git --ref branch:main
ghrah distro start generic
```

本地源码仓同样可用：

```sh
ghrah distro install local --repo /path/to/ghrah-monorepo --ref commit:COMMIT
```

## 安装与开发

```sh
uv python install 3.15.1
uv sync --group dev
uv run ghrah --version
uv run python -m ghrah_toolchain --help
```
distribution 为 `ghrah-toolchain`，Python import 为 `ghrah_toolchain`，正式 CLI 为 `ghrah`。

需要写 `~/.ghrah` 的命令（`dev`／`install`／`start`）请使用上文 `uv tool install --editable .` 装好的 `ghrah`；`uv run ghrah` 的写入限制见「安装本工具链并安装九夏」。

## 检查源码组合

在包含 ghrah.toml 的工作区运行：

```sh
ghrah check
ghrah metadata --json
ghrah check --manifest-path /path/to/ghrah-monorepo/ghrah.toml --json
```

解析器支持 TOML 1.1，向上发现工作区，校验字段、组件原生身份、受控路径、构建依赖、入口、实例、接口导入导出与局部命名空间引用。schema 资源随 wheel 分发，校验不访问远程 schema，也不执行组件代码。

当前组件来源仅支持本地 path。metadata 是静态视图：不会启动组件、创建 Agent 授权或证明外部服务已经可用。构建／安装链目前继续使用已有产物流程，尚未改为由 ghrah.toml 驱动。

## 安装、启动和管理发行版实例

首次安装需要明确的运行时源码仓，可使用远程 URL 或已有本地路径：

```sh
ghrah distro install generic --from https://github.com/ghrah-org/ghrah-monorepo.git --ref branch:main
ghrah distro start generic
ghrah distro status generic
ghrah distro list
ghrah distro logs generic --tail 100
ghrah distro stop generic
```

create 与 install 使用同一安装流程，创建实例后不自动启动。安装的位置位于 ~/.ghrah。可用 GHRAH_HOME 显式指定安装路径。

`--repo` 与 `--from` 同义，支持本地 Git 路径、HTTPS／HTTP、SSH URL，以及 `git@host:org/repo.git` 形式；file URL 可用于本机 Git 远端。安装会将候选解析为完整提交 ID，再构建和准备运行环境，不是把远程仓直接放进运行目录。registry 和发行包归档安装器尚未提供。可指定 `--subject-port` 与 `--webui-port`，便于多实例并行。构建、环境准备与试运行阶段会输出进度；首次安装失败会清除本次生成的实例声明，成功后才报告已安装。

```sh
# 复用系统 Git 的 SSH 认证
ghrah distro install generic --from git@github.com:ghrah-org/ghrah-monorepo.git --ref branch:main
# 标签（包括带注释标签）或固定提交
ghrah distro install release --from https://example.org/org/runtime.git --ref tag:v1.0.0
# 本地来源继续可用
ghrah distro install local --repo /path/to/ghrah-monorepo --ref commit:COMMIT
```

HTTPS 使用系统 Git 的 credential helper，SSH 使用现有密钥／agent。URL 不接受密码、HTTPS 内嵌用户名／令牌、查询参数或片段，认证应先在 Git 侧配置。远程操作禁用终端密码询问；默认 SSH 使用 BatchMode，已有 GIT_SSH_COMMAND 则沿用。网络／认证失败或超时会报错，不切换 current。

远程来源写入实例 `distro.toml` 的 `[distro.repo].url`，本地来源继续使用 `.path`，二者不能同时设置。实际裸仓缓存放在 `<GHRAH_HOME>/.cache/repositories/<URL摘要>/repo.git`；克隆在临时目录完成后落位，同一来源的准备与构建使用同一缓存锁。运行代不依赖该缓存，清理缓存不会删除已安装产物或业务数据。

远程 `latest` 选择缓存 HEAD 所指的默认分支；同步时重新核对远端默认分支。普通本地工作树继续保留现有按 ref 提交日期选择的 latest，裸仓使用 HEAD；需要稳定版本时明确使用 branch/tag/commit。工具链不读取 Git 子模块或 Git LFS 的外部内容，安装所需源码应包含在该提交中。

构建产物的 `manifest.json` 记录源提交 `.python-version` 中的运行时数字版本，安装和产物验证据此选择解释器，不继承工具链的 Python 3.15。没有版本声明的旧式 ghrah 来源／产物暂沿用 3.11 基线；此兼容规则只服务现有 ghrah 构建链，后续通用组件需要显式运行时契约。所需解释器由 uv 查找或安装。

已有的短命令 `install/create`、`start/boot`、`stop`、`restart`、`status`、`list/ps`、`logs`、`config` 与规范 `distro` 分组使用同一处理函数。运行实例与当前源码工作区分开，cwd 不改变被管理的实例。

**start 默认立即返回。** 返回 starting 表示已提交后台启动，不代表健康检查已通过。后台监督进程脱离终端，接管 Subject 和静态前端；关闭启动 shell 不会终止监督关系。通过 list 查看 running／failed，通过 logs 查看启动及运行输出。重复 start 不会再起一份监督者，重复 stop 返回成功。

```sh
ghrah start generic --json
ghrah ps --json
ghrah start generic --foreground
ghrah distro restart generic
ghrah distro default generic
```

--foreground 显式占用当前终端，适合诊断。默认不自动打开浏览器。boot 保留为 start 的迁移别名，也采用后台语义；救援或前端开发模式须显式加 --foreground，避免无意执行交互操作。

`status` 分开显示安装代、监督进程状态和配置地址，不会因读取状态而 fetch、构建或启动；`running` 表示启动健康检查曾通过，当前健康不会被自动重新探测。`default` 显式选择默认已安装实例，省略名称时使用该设置，不读取源码清单的 default_distro。

每次后台启动创建独立日志，位于 <安装域>/<实例>/logs/；Subject 输出和监督者错误均写入其中。当前尚未自动轮转日志。正常 stop 会收束子进程后再报告停止；POSIX 支持启动中取消和 SIGHUP 收束。监督者被 SIGKILL 等不可捕获方式杀死时的内核级子进程兜底尚未实现，Windows 分支尚未真机验收。

```sh
ghrah distro logs generic --follow
```

日志跟随会切换到重新启动后的新会话；Ctrl+C 只退出读取，实例继续运行。跟随器也能识别文件替换及已观察到的截断，但工具链尚未实现写入端的自动轮转。

## 更新已安装实例

```sh
ghrah distro update generic --check --json
ghrah distro stop generic
ghrah distro update generic
ghrah distro start generic
```

`--check` 只比较源仓本地可见的提交，不 fetch、不构建、不拍快照、不切换；不能同时使用 `--fetch`。执行更新时可显式 `--fetch`，失败立即终止，不把旧的本地结果当成远端最新结果。`--source commit:COMMIT`（或 `--ref`）只覆盖本次候选；长期跟踪来源通过下节配置修改。

新建远程实例的 `fetch_on_update` 默认为 true，本地实例默认为 false。远程 update 默认同步缓存；`update --no-fetch` 仅使用已有缓存，适合离线复用。首次远程 install 必须克隆；缓存丢失后 `update --check` 不会下载，`update --no-fetch` 会报错，使用 `update --fetch` 可重建。再次 install 根据该实例的 fetch_on_update 选择是否刷新已有缓存。`--repo` 覆盖只影响本次操作，不悄悄重写已保存的来源。

更新要求实例完全停止，包括尚未监听端口的 starting 阶段。普通更新保存已有数据，沿用门禁→快照→切换 current 的流程，不自动重启，不恢复旧数据。相同组合代不重复切换或拍快照。切换失败尝试回退；回退本身失败会明确报告并保留恢复产物。运行中的其他实例不因本实例构建而被停止。

full／data 门禁仍未实现，选择它们会在构建前失败。数据快照恢复会丢弃快照之后的写入，仅通过显式救援参数及确认执行；普通 start/update 不进行数据恢复。独立的 `distro rollback/uninstall` 命令尚未提供。

## 配置实例与 CLI

实例配置保存在 `<GHRAH_HOME>/<实例>/distro.toml`；CLI 偏好保存在 `<GHRAH_HOME>/config.toml`，缺省根为 `~/.ghrah`。业务模型、Project 和 Agent 设置仍由运行时自己的 API／UI 管理。

```sh
ghrah distro config show generic
ghrah distro config get generic runtime.subject_port --json
ghrah distro stop generic
ghrah distro config set generic runtime.subject_port 4222
ghrah distro config set generic distro.ref.source branch:main
ghrah distro config set generic distro.ref.fetch_on_update true
ghrah self status
ghrah self config set self.log_tail 200
ghrah self config set self.output json
ghrah self config show
```

| 实例可写键 | 类型与含义 |
| --- | --- |
| runtime.subject_port／runtime.webui_port | 1–65535 整数，两端口不能相同，下次启动生效 |
| distro.ref.source | latest／branch:NAME／tag:NAME／commit:SHA；后续安装更新的候选来源 |
| distro.ref.gate | 当前只可写 basic |
| distro.ref.fetch_on_update | true／false，执行更新时的默认 fetch 行为 |
| data.root | 绝对路径或 ~ 路径，空字符串使用默认数据盘；只允许无已有数据时设置到空目标 |
| data.snapshot_keep | 0–10000 整数，更新时保留的快照数量；0 表示不保留 |

`get/show` 显示有效值、来源和生效时机，只展示支持的配置，不回显未知人工字段。`set` 要求实例停止，检查类型和范围、保留注释和无关内容，并保存上一份 `<文件>.bak` 后原子替换。检测到外部并发编辑则拒绝覆盖；已有数据根迁移和数据认领必须单独处理，不能通过改路径静默完成。

来源字段 `distro.repo.path/url` 可通过 get/show 只读查看，不通过 config set 改写；ref.source 也支持 tag:NAME。

CLI 首轮支持 `self.output = text/json`（支持 JSON 的命令默认输出格式）与 `self.log_tail = 0..10000`（日志默认尾行数）。显式 `--json/--tail` 优先；配置命令本身使用显式输出参数，以便修复已有偏好。self status 显示工具版本、解释器及安装路径，self doctor／doctor 检查安装域。CLI 自我更新尚未实施。

## 本地开发快环（dev）

dev 从**工作树**（含未提交改动）增量构建并本地起服，用于改完代码立刻看到效果。它只读写 `<安装域>/<实例>/.dev/`，不重指 `current`，也不改变 `install`／`runtime`／`start` 的既有行为。

前置：该实例已安装并重指过 `current`（`install`／`create` 的产物），且 `distro.toml` 的 `[distro.repo].path` 指向有效的源码仓工作树。远程 `url` 来源的实例没有本地工作树，dev 无法使用；请改用本地路径安装，或另建一个以本地检出为来源的实例。

```sh
ghrah dev status            # 对比工作树与上次 dev 构建，报「全量 / 增量（哪些包）」+ 会话状态
ghrah dev run               # 构建后前台起服；已在跑则先停、回收端口，再重启
ghrah dev run --no-restart  # 只构建／升级，不起服
ghrah dev run --full        # 忽略指纹强制全量重建
ghrah dev run --no-browser  # 起服但不自动打开浏览器
ghrah dev stop              # 停止 dev 会话（收束整棵进程树）
ghrah dev reset             # 清空 .dev（含产物），下次必然全量
```

动词沿用本仓既有词汇（`start`／`stop`）：`run` 表示"前台跑起来并监督"，`stop` 表示停止。
`up`／`down` 保留为别名（等价于 `run`／`stop`），旧脚本无需改动。

`dev run` 是**幂等**的「确保按当前工作树跑起来」：已有 dev 会话就先停止并**等端口真正释放**再起，不会出现「已在运行就跳过」的空操作，也不会因端口未回收而绑定失败。发布态会话占用同一对端口时同样会让位。

`dev run` 是**前台**命令：起服后在当前终端持续监督，直到 Ctrl-C（或在另一终端执行 `ghrah dev stop`）。就绪后控制台会打印访问地址：

```text
[dev] ===== dev 环境已就绪 =====
[dev]   前端页面  http://localhost:5173
[dev]   后端接口  http://127.0.0.1:4112（/health、ws ws://127.0.0.1:4112/ws）
[dev]   运行日志  <安装域>/<实例>/.dev/dev.log
```

浏览器默认自动打开同 `start`；要关掉用 `--no-browser`。Subject 与前端子进程的输出直接继承当前终端（含 uvicorn 的就绪行与请求日志），监督侧的 INFO 记录另写入 `.dev/dev.log`。

增量维度是「包」：五个 Python wheel 各算一个指纹单元，依赖闭包（`pyproject.toml`／`uv.lock`／`pnpm-lock.yaml`／`pnpm-workspace.yaml`）与 webui 各算一个。只改某个 Python 包时仅重编该 wheel 并强制重装进 dev venv；依赖或 webui 变动走全量重建。首次必然全量（含 pnpm 打包，约几分钟）。

dev 与发布态共用端口：起服前若存在 boot 会话会先停止它，同一时间只运行一个形态。dev 专用 venv 位于 `.dev/gen/env/.venv`，指纹基线在 `.dev/state.json`，会话 pid 在 `.dev/dev.pid`（与旧 `ghrah-bootloader` 的 dev 同路径、同格式，可直接互相接管）。

代内 Python 钉 monorepo `requires-python` 的下限版本（当前 3.11），而不是工具链自身解释器：运行时闭包里的 C 扩展（httptools 等）在最新 CPython 上常无预编译 wheel，会退化成源码编译（Windows 需 MSVC）。工具链自身的 Python 版本与代内环境仍然各管各的。

调试时把 `-v`／`--verbose` 放在子命令之前（`ghrah -v dev run`）。Windows 上运行中的 Python 持有 `.pyd`／`.pyc` 文件锁，因此「停旧 → 装 → 起新」的顺序不可颠倒；构建阶段可以让旧服继续跑。

## 源码构建与诊断

```sh
ghrah build --repo /path/to/ghrah-monorepo --ref commit:COMMIT --out /path/to/output
ghrah self doctor
```

原 `runtime update/status` 仍保留兼容；新用法优先采用 distro 分组。dev 热重载、源码脚手架与通用 package 工作流属于后续迭代。

## 验证

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

默认不运行 integration 标记的真实 monorepo 构建／全栈用例；需要时执行 uv run pytest -q -m integration，并准备相应依赖和环境。测试使用临时安装域，不以真实用户发行版作测试对象。
