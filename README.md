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
