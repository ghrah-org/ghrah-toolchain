# ghrah-bootloader

ghrah AgentOS 安装/配置工具与引导器（独立包）。

## 定位

承载 AgentOS 的安装 / 配置 / 启动门禁 / 引导（bootloader）/ 监督 / 版本管理。
引导器与 agent 无关（不硬编码任何人格）；Subject（OS）以独立子进程拉起
（out-of-process 硬要求）。

## 纯性硬约束

`ghrah_bootloader.*` 的 import 图内**零 `ghrah.*`、零第三方运行时依赖**
（仅 stdlib；健康检查用 `urllib.request` 轮询）。引导器与 agent 无关：
agent 期望状态由 Subject 侧 durable Project API 承载，引导器不含任何
agent 注入路径。

## 开发

```bash
uv sync                # dev 组（pytest / ruff）
uv run pytest          # 单测
uv run ruff check . && uv run ruff format --check .
```
