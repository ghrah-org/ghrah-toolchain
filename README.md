# ghrah-bootloader

ghrah AgentOS 安装/配置工具与引导器（独立包，CLI `ghrah-bootloader`）。

## 定位

承载 AgentOS 的安装 / 配置 / 引导（bootloader）/ 监督 / 版本管理。
引导器不包含任何Agent的具体功能，Subject（OS）以**独立子进程**拉起

三层职责：

| 层 | 载体 | 职责 |
| --- | --- | --- |
| 引导器 | `ghrah-bootloader`（独立进程） | 安装 / 门禁 / spawn OS 与前端 / 监督 / 版本管理 |
| OS | `ghrah-subject-server`（发行版 env 内，独立进程） | 运行 Subject + Core，承载 project/agent/session |
| agent | Subject 内的 CoreUnit | 承载具体业务的智能体 |

## 安装域布局（`~/.ghrah`）

```
~/.ghrah/                              # 「磁盘」：用户级安装域
  boot.toml                            # GPT + 引导菜单：default / fallback / last_good
  <distro>/                            # 一个发行版 = 分区 + 配套数据盘
    distro.toml                        # 声明：repo / ref / gate / ports / [data]
    data/                              # ★ 数据盘（版本无关，切换版本不动这里）
      subject.db · taskstore.sqlite3 · manifests/ · projects/ · workspace/
    snapshots/<ts>-v<old>→v<new>/      # 切换前快照（META.toml + data/ 完整拷贝，N 代滚动）
    legacy/adopt-<ts>/                 # 旧数据认领归档与备份（确认后可删）
    env/.venv                          # 发行版运行依赖（自管 .pth → current 段）
    current -> versions/<ver>          # 稳定路径：切换 = 原子重指
    last-good -> versions/<ver>        # 已知良好版本（救援目标）
    versions/<ver>/                    # git worktree @ 固定 commit（纯代码）
```

安装域与数据域分离：版本树纯代码；数据落发行版级数据盘；boot 时数据有：
（subject.db / taskstore / manifests / projects 模板 / workspace）+ 端口全部
经 env 注入指向 `data/`，切换版本对数据面零影响。

## 功能流程

### 1. 首次安装

```bash
ghrah-bootloader install --distro generic --version 2026.09 \
  --ref commit:861ca39 --repo /path/to/ghrah-monorepo
```

链路：解析候选 commit（`latest | branch:<n> | commit:<sha>`）→ 建版本树
（git worktree --detach）→ 门禁 basic（候选临时 env 冒烟，不触碰发行版 env）
→（存在旧 current 时）拍数据快照 → 原子重指 `current` → 按需 sync
（依赖指纹变化才 uv sync / pnpm install）→ 写 boot.toml → 首装设 last-good。
任一步失败等于什么都没发生（current/env/boot.toml 原值，新建树移除）。

版本名缺省取候选提交日期 `%Y.%m`；同月同名自动加短 sha（如 `2026.09-8c932b0`）。
已装同 commit 复用既有树，不建重复目录。

### 2. 旧数据认领（一次性过渡）

检测到 `~/.ghrah` 顶层遗留数据（旧版默认位）时提示；显式认领：

```bash
ghrah-bootloader install --distro generic --adopt-legacy-data
```

守卫（端口占用 / 数据盘同名 / 文件句柄在用，任一命中即拒绝零改动）→ 整体备份
→ 五项数据面**移动**进 `data/`，其余（`ghrah.db`、bak、顶层 `data/`）归档
`legacy/`。认领后顶层只剩 `boot.toml` 与 distro 目录；备份保留，确认后手删。

### 3. 引导与监督

```bash
ghrah-bootloader boot [--no-webui] [--no-browser]   # 别名 start / webui
```

spawn 发行版 env 的 `ghrah-subject-server`（cwd=`current/`，数据面 env 全量
注入）+ 前端（`pnpm dev`，经 `current` 段解析到版本树）；健康检查通过后更新
last-good；前台监督循环——OS 意外退出收束全部子进程以非零码退出，SIGINT/SIGTERM
逆序优雅收束（webui → OS）。启动时写 `.boot.pid`，二次 boot 拒绝双启。

```bash
ghrah-bootloader stop [--distro <n>]     # 另一终端优雅停止
```

pidfile 定位 → cmdline 护栏（防误杀）→ SIGTERM → 轮询进程退出与端口释放
（超时不强杀，给手工处置指引）。

### 4. 版本前移

```bash
ghrah-bootloader runtime status            # current / 候选 / 落后 / last_good
ghrah-bootloader runtime update [--source <src>] [--gate basic] [--fetch]
```

与 install 同一状态机（门禁 → 快照 → 重指 → 按需 sync）；依赖指纹一致时
切换是**纯重指**（自管 `.pth` 指向 `current` 段，重指即换导入源）。

### 5. 救援

```bash
ghrah-bootloader boot --ref last-good                       # 回已知良好版本
ghrah-bootloader boot --ref last-good --restore-snapshot [--yes]
                                                            # + 数据回滚到最近快照
```

`--restore-snapshot` 恢复最近快照并重指 current 到 last-good；交互下二次确认，
**明示损失快照之后的写入窗口**。注意：救援重指 current 后 `boot.toml default`
不自动回写（每次 boot 告警提示差异），菜单对齐自动化属后续批次。

### 6. 诊断

```bash
ghrah-bootloader distro list    # 发行版与版本清单（读 GPT）
ghrah-bootloader doctor         # 布局 / default / current commit / 落后情况
```

## 平台支持

| 平台 | 状态 |
| --- | --- |
| Linux | 一级支持（主开发与验收平台） |
| macOS | 支持（stop 的 `/proc` 护栏已守卫，无 `/proc` 时跳过并告警） |
| Windows | **未支持**（代码含 nt 分支，但 pid 存活探测、stop 语义、symlink 原子重指三处核心机制存在实质缺口，修复已排期） |

## 注意事项

- **签出源默认 `latest` 是全局最近 ref tip**（跨全部分支，可能取到 WIP）；
  生产/验收建议 `commit:` 钉死。
- **`full` / `data` 门禁未启用**（调用即明确报错，不静默降级）；启动计数
  自动回退、`config get/set`、`uninstall` 属后续批次。
- 前端 dev 服务（vite）监听 IPv6 loopback `[::1]:<port>`，用 `127.0.0.1`
  探测会得到连接失败，属正常。
- `distro.toml` 首装生成后手工调整（端口 / 数据盘 root / snapshot_keep）在
  v1 可接受。

## 纯性硬约束

`ghrah_bootloader.*` 的 import 图内**零 `ghrah.*`、零第三方运行时依赖**
（仅 stdlib；健康检查用 `urllib.request` 轮询），由 `tests/test_purity.py`
门禁测试锁定。引导器与 agent 无关：agent 期望状态由 Subject 侧 durable
Project API 承载，引导器不含任何 agent 注入路径（provisioning 已退役）。

## 开发

```bash
uv sync                # dev 组（pytest / ruff）
uv run pytest          # 单测（默认跳过 integration 标记的真实 sync 用例）
uv run ruff check . && uv run ruff format --check .
```

测试可用 `GHRAH_HOME` 重定向安装域。真实域操作见
`tasks/2026-09-24-ghrah-bootloader.md` §七/§九 验收记录。
