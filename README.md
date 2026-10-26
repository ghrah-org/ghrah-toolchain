# ghrah-toolchain

ghrah 的独立工具链，提供源码组合的离线校验、发行物构建和安装，以及具名实例的后台启停与日志查询。工具链不加载 Core／Subject 业务包；运行时从已安装发行版的环境中启动。

需要 Python **3.15.1 或更新版本**。开发与构建使用 uv；涉及前端构建时还需要 pnpm 和 Node ≥22。工具链自身的 Python 版本与被部署发行版的 Python 环境分别管理。

## 安装与开发

在本仓执行：

```sh
uv python install 3.15.1
uv sync --group dev
uv run ghrah --version
uv run python -m ghrah_toolchain --help
```

distribution 为 `ghrah-toolchain`，Python import 为 `ghrah_toolchain`，正式 CLI 为 `ghrah`。新包不提供旧的 `ghrah-bootloader` 可执行入口或 Python import。

需要写 `~/.ghrah` 的命令（`dev`／`install`／`start`）请用装好的 `ghrah`，而不要用 `uv run ghrah`：

```sh
uv tool install --editable .   # 入口 ~/.local/bin/ghrah，工具 venv 在安装域外
ghrah --version
```

`uv run ghrah` 使用项目内 `.venv` 的解释器；在受限环境（沙箱、受控终端等）下它只能写工作区，写 `~/.ghrah` 会报 `Permission denied`——`dev` 起服失败时会直接给出这条提示。

## 检查源码组合

在包含 ghrah.toml 的工作区运行：

```sh
ghrah check
ghrah metadata --json
ghrah check --manifest-path /path/to/ghrah-monorepo/ghrah.toml --json
```

解析器支持 TOML 1.1，向上发现工作区，校验字段、组件原生身份、受控路径、构建依赖、入口、实例、接口导入导出与局部命名空间引用。schema 资源随 wheel 分发，校验不访问远程 schema，也不执行组件代码。

当前组件来源仅支持本地 path。metadata 是静态视图：不会启动组件、创建 Agent 授权或证明外部服务已经可用。构建／安装链目前继续使用已有产物流程，尚未改为由 ghrah.toml 驱动。

## 创建、启动和停止具名实例

首次安装仍需要明确的运行时源码仓与提交：

```sh
ghrah create generic --repo /path/to/ghrah-monorepo --ref commit:COMMIT
ghrah start generic
ghrah list
ghrah logs generic --tail 100
ghrah stop generic
```

create 与 install 使用同一安装流程，创建实例后不自动启动。已有安装域继续位于 ~/.ghrah，数据、组合代、快照与 last-good 保留兼容。可用 GHRAH_HOME 显式指定隔离安装域。

**start 默认立即返回。** 返回 starting 表示已提交后台启动，不代表健康检查已通过。后台监督进程脱离终端，接管 Subject 和静态前端；关闭启动 shell 不会终止监督关系。通过 list 查看 running／failed，通过 logs 查看启动及运行输出。重复 start 不会再起一份监督者，重复 stop 返回成功。

```sh
ghrah start generic --json
ghrah ps --json
ghrah start generic --foreground
```

--foreground 显式占用当前终端，适合诊断。默认不自动打开浏览器。boot 保留为 start 的迁移别名，也采用后台语义；救援或前端开发模式须显式加 --foreground，避免无意执行交互操作。

每次后台启动创建独立日志，位于 <安装域>/<实例>/logs/；Subject 输出和监督者错误均写入其中。当前尚未自动轮转日志。正常 stop 会收束子进程后再报告停止；POSIX 支持启动中取消和 SIGHUP 收束。监督者被 SIGKILL 等不可捕获方式杀死时的内核级子进程兜底尚未实现，Windows 分支尚未真机验收。

## 本地开发快环（dev）

dev 从**工作树**（含未提交改动）增量构建并本地起服，用于改完代码立刻看到效果。它只读写 `<安装域>/<实例>/.dev/`，不重指 `current`，也不改变 `install`／`runtime`／`start` 的既有行为。

前置：该实例已安装并重指过 `current`（`create`／`install` 的产物），且 `distro.toml` 的 `[distro.repo].path` 指向有效的源码仓工作树。

```sh
ghrah dev status            # 对比工作树与上次 dev 构建，报「全量 / 增量（哪些包）」
ghrah dev up                # 构建 + 物化 dev venv + 接管端口前台起服
ghrah dev up --no-restart   # 只构建／升级，不起服
ghrah dev up --full         # 忽略指纹强制全量重建
ghrah dev up --no-browser   # 起服但不自动打开浏览器
ghrah dev down              # 停止 dev 会话（收束整棵进程树）
ghrah dev reset             # 清空 .dev（含产物），下次必然全量
```

`dev up` 是**前台**命令：起服后在当前终端持续监督，直到 Ctrl-C（或在另一终端执行 `ghrah dev down`）。就绪后控制台会打印访问地址：

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

调试时把 `-v`／`--verbose` 放在子命令之前（`ghrah -v dev up`）。Windows 上运行中的 Python 持有 `.pyd`／`.pyc` 文件锁，因此「停旧 → 装 → 起新」的顺序不可颠倒；构建阶段可以让旧服继续跑。

## 现有产物与安装功能

```sh
ghrah build --repo /path/to/ghrah-monorepo --ref commit:COMMIT --out /path/to/output
ghrah runtime update --distro generic --source commit:COMMIT
ghrah doctor
```

安装切换沿用原有门禁、快照、current 与 last-good 机制。full／data 门禁仍未实现，调用会明确失败。恢复数据快照会丢弃快照之后的写入，需通过显式救援参数确认；普通 start 不进行数据恢复。

## 验证

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

默认不运行 integration 标记的真实 monorepo 构建／全栈用例；需要时执行 uv run pytest -q -m integration，并准备相应依赖和环境。测试使用临时安装域，不以真实用户发行版作测试对象。
