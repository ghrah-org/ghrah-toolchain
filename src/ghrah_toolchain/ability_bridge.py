# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""Ability 命令薄桥：解释器解析、模块探测与子进程转调。

工具链不在本地实现任何 Ability 领域逻辑：本模块只负责找到"目标运行时"的 Python
解释器、确认该解释器能 import 到 core 侧入口、再把 argv 原样交给子进程。因此这里
**零 ``ghrah.*`` import**，只用标准库；目标运行时由子进程自行加载。

两个 core 入口：

- ``ghrah.ability.tools.build``——P0 冻结的构建入口（``build`` 命令优先使用）；
- ``ghrah.ability.tools.cli``——统一入口（store 命令必需，``build`` 可回退使用）。

解析规则：``--python`` 是显式选择，失败立即报错、不回退；其余候选按优先级逐个
探测（存在、POSIX 下可执行、目标模块可 import），全部失败时一次性列出每个候选
路径与失败原因。解释器路径只做 expanduser + 绝对化，**绝不** ``resolve()``：venv
的 ``bin/python`` 通常是指向基础解释器的符号链接，解开链接会让子进程丢掉
``sys.prefix``，从而不在目标 venv 中运行。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ghrah_toolchain.errors import AbilityError
from ghrah_toolchain.layout import (
    BootToml,
    DistroLayout,
    ghrah_root,
    list_distros,
    resolve_distro_root,
)

TOOLS_BUILD = "ghrah.ability.tools.build"
TOOLS_CLI = "ghrah.ability.tools.cli"

# 探测 import 的子进程超时（秒）：探测是只读的短生命周期检查，卡住即视为不可用。
_PROBE_TIMEOUT = 30.0

# 子进程里执行的探测脚本：父包缺失时 ``find_spec`` 抛 ``ModuleNotFoundError``
# 而不是返回 None，故捕获后按"不可用"处理；结果只经退出码表达，避免解析输出。
_PROBE_CODE = (
    "import importlib.util, sys\n"
    "try:\n"
    "    spec = importlib.util.find_spec(sys.argv[1])\n"
    "except (ImportError, ValueError):\n"
    "    spec = None\n"
    "raise SystemExit(0 if spec is not None else 1)\n"
)


@dataclass(frozen=True)
class AbilityRuntime:
    """一次 Ability 调用选定的目标运行时。

    - ``python``：解释器路径（expanduser + 绝对化，不 resolve，保住 venv 的
      ``sys.prefix``）；
    - ``ability_root``：发行版级安装存储根 ``<distro>/ability``；``build`` 不落
      安装，恒为 None；
    - ``distro``：目标发行版名；``build`` 未回退到发行版时为 None。
    """

    python: Path
    ability_root: Path | None
    distro: str | None


# ── 路径与候选辅助 ───────────────────────────────────────────


def _absolute(path: Path) -> Path:
    """expanduser 后绝对化，但**不** ``resolve()``（不解符号链接）。"""
    return path.expanduser().absolute()


def _venv_python(venv: Path) -> Path:
    """venv 内的解释器：POSIX 为 ``bin/python``，Windows 为 ``Scripts/python.exe``。"""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _interpreter_reason(python: Path) -> str | None:
    """解释器文件本身是否可用；不可用返回原因，可用返回 None。"""
    if not python.is_file():
        return "解释器不存在"
    if os.name != "nt" and not os.access(python, os.X_OK):
        return "解释器不可执行"
    return None


def _attempt(label: str, path: Path | None, reason: str) -> str:
    """格式化一条候选失败记录（错误信息逐候选列出，便于用户直接修正）。"""
    if path is None:
        return f"  - {label}：{reason}"
    return f"  - {label}：{path} —— {reason}"


def _explicit_failure(python: Path, reason: str) -> AbilityError:
    """``--python`` 显式指定的解释器不可用时的统一错误（不回退）。"""
    return AbilityError(
        f"--python 指定的解释器不可用：\n  {python} —— {reason}\n"
        "--python 是显式选择，不自动回退到其他运行时；请修正路径或换一个解释器。"
    )


def _cwd_venv_candidates() -> list[tuple[str, Path]]:
    """当前目录及祖先的 ``.venv``（向上到含 ``.git`` 文件或目录的那一级为止）。"""
    start = Path.cwd()
    found: list[tuple[str, Path]] = []
    for directory in (start, *start.parents):
        venv = directory / ".venv"
        if venv.is_dir():
            found.append((f"工作目录 {directory} 的 .venv", _venv_python(venv)))
        # .git 可能是目录（普通仓库）也可能是文件（worktree / submodule）：
        # 两者都视为"项目根"，根之上的 .venv 属于别的项目，不再纳入候选。
        if (directory / ".git").exists():
            break
    return found


def _probe_build(python: Path) -> str | None:
    """``build`` 候选可用性：可执行且能 import 到 build 或 cli 入口之一。"""
    reason = _interpreter_reason(python)
    if reason is not None:
        return reason
    if module_available(python, TOOLS_BUILD) or module_available(python, TOOLS_CLI):
        return None
    return f"缺少 {TOOLS_BUILD} 与 {TOOLS_CLI}（目标运行时未安装 ghrah-core）"


def _probe_store(python: Path) -> str | None:
    """store 候选可用性：可执行且能 import 到统一入口 ``tools.cli``。"""
    reason = _interpreter_reason(python)
    if reason is not None:
        return reason
    if module_available(python, TOOLS_CLI):
        return None
    return f"缺少 {TOOLS_CLI}（store 命令需要 ghrah-core P2/P5）"


# ── 默认发行版 ───────────────────────────────────────────────


def default_distro() -> str:
    """``boot.toml`` 的默认发行版名（``default = "<distro>/<version>"`` 第一段）。

    没有 default 时抛 ``AbilityError`` 并列出 ``list_distros()`` 的可用发行版：
    **不**自动选择"唯一一个"发行版，避免同一脚本在不同机器上静默跑向不同目标。
    """
    boot = BootToml.load()
    if not boot.default:
        available = "、".join(list_distros()) or "（无）"
        raise AbilityError(
            "未配置默认发行版（boot.toml 的 default 为空）。"
            f"当前可用发行版：{available}；请先 ghrah distro install，或用 --distro 指定。"
        )
    return boot.default.split("/", 1)[0]


# ── 运行时解析 ───────────────────────────────────────────────


def resolve_build_runtime(python: str | None = None) -> AbilityRuntime:
    """为作者侧 ``build`` 选定目标运行时。

    候选顺序：``--python`` → ``$VIRTUAL_ENV`` → 当前目录及祖先的 ``.venv``（向上到
    含 ``.git`` 文件或目录的那一级为止）→ 默认发行版的 ``current/env/.venv``。
    每个候选须存在、POSIX 下可执行，且能 import 到 ``TOOLS_BUILD`` 或 ``TOOLS_CLI``
    之一。``--python`` 显式给出时不回退（失败直接抛 ``AbilityError``）；其余候选
    按序跳过不可用项，全部失败时抛 ``AbilityError`` 并列出每个候选路径与原因。

    ``build`` 不落安装，返回的 ``ability_root`` 恒为 None；``distro`` 仅在回退到
    默认发行版时给出。
    """
    if python is not None:
        path = _absolute(Path(python))
        reason = _probe_build(path)
        if reason is not None:
            raise _explicit_failure(path, reason)
        return AbilityRuntime(python=path, ability_root=None, distro=None)

    attempts: list[str] = ["  - --python 未提供"]

    env = os.environ.get("VIRTUAL_ENV")
    if env:
        path = _venv_python(_absolute(Path(env)))
        reason = _probe_build(path)
        if reason is None:
            return AbilityRuntime(python=path, ability_root=None, distro=None)
        attempts.append(_attempt("$VIRTUAL_ENV", path, reason))
    else:
        attempts.append("  - $VIRTUAL_ENV 未设置")

    cwd_candidates = _cwd_venv_candidates()
    if not cwd_candidates:
        attempts.append("  - 当前目录及祖先：未找到 .venv 目录")
    for label, path in cwd_candidates:
        reason = _probe_build(path)
        if reason is None:
            return AbilityRuntime(python=path, ability_root=None, distro=None)
        attempts.append(_attempt(label, path, reason))

    try:
        distro = default_distro()
    except AbilityError as e:
        attempts.append(f"  - 默认发行版：{e}")
    else:
        venv = resolve_distro_root(distro) / "current" / "env" / ".venv"
        path = _venv_python(venv)
        reason = _probe_build(path)
        if reason is None:
            return AbilityRuntime(python=path, ability_root=None, distro=distro)
        attempts.append(_attempt(f"默认发行版 {distro}", path, reason))

    raise AbilityError(
        "无法定位可执行 ability 命令的运行时：\n"
        + "\n".join(attempts)
        + "\n请先 ghrah distro install/start，或用 --python 指定解释器。"
    )


def resolve_store_runtime(python: str | None = None, distro: str | None = None) -> AbilityRuntime:
    """为 store 命令（install/list/show/verify/refresh/uninstall）选定目标运行时。

    候选顺序：``--python`` → 目标发行版（``--distro`` 或 ``default_distro()``）的
    ``current/env/.venv``；目标解释器必须能 import 到 ``TOOLS_CLI``。与 ``build``
    不同，这里**不**考虑 ``$VIRTUAL_ENV`` 与 cwd 的 ``.venv``：store 命令操作的是
    某个发行版的安装物，必须用该发行版的 core 来解释包与写 lock，用作者环境去写
    另一个发行版的存储会把"构建工具版本"和"运行/安装版本"混在一起。

    ``--python`` 显式给出时失败不回退（与 ``build`` 同一规则）；否则报错时列出
    目标发行版候选及原因。返回的 ``ability_root`` 为 ``<distro>/ability`` 的绝对
    路径（不 ``resolve()``；存储根若本身是符号链接，拒绝由 core 负责）。
    发行版名非法或为保留名 ``ability`` 时，由 ``layout.resolve_distro_root`` 抛
    ``LayoutError``。
    """
    name = distro or default_distro()
    distro_root = resolve_distro_root(name)
    ability_root = _absolute(DistroLayout(root=distro_root).ability)

    if python is not None:
        path = _absolute(Path(python))
        reason = _probe_store(path)
        if reason is not None:
            raise _explicit_failure(path, reason)
        return AbilityRuntime(python=path, ability_root=ability_root, distro=name)

    path = _venv_python(distro_root / "current" / "env" / ".venv")
    if not distro_root.is_dir():
        reason = "发行版目录不存在"
    elif not (distro_root / "current").exists():
        reason = "尚未安装可用版本（current 不存在）"
    else:
        reason = _probe_store(path)
    if reason is not None:
        raise AbilityError(
            "无法定位发行版级 ability 运行时：\n"
            + _attempt(f"发行版 {name}", path, reason)
            + "\n请先 ghrah distro install/start，或用 --python 指定解释器。"
        )
    return AbilityRuntime(python=path, ability_root=ability_root, distro=name)


# ── 模块探测与子进程转调 ─────────────────────────────────────


def module_available(python: Path, module: str) -> bool:
    """目标解释器能否 import 到 ``module``（不可用一律返回 False，不抛异常）。

    在子进程里执行 ``importlib.util.find_spec``：父包缺失时 ``find_spec`` 会抛
    ``ModuleNotFoundError`` 而不是返回 None。非零退出、探测超时与无法启动
    （``OSError``）同样视为不可用——探测只影响候选筛选，不应把异常抛给调用方。
    """
    try:
        cp = subprocess.run(
            [str(python), "-c", _PROBE_CODE, module],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return cp.returncode == 0


def resolve_build_module(python: Path) -> str:
    """``build`` 选择 core 入口：优先 ``TOOLS_BUILD``，其次 ``TOOLS_CLI``。

    两者都不可用时抛 ``AbilityError``（目标运行时缺少 Ability 工具）。回退到
    ``TOOLS_CLI`` 时，命令层需要把 ``build`` 作为第一个参数（verb）传入。
    """
    if module_available(python, TOOLS_BUILD):
        return TOOLS_BUILD
    if module_available(python, TOOLS_CLI):
        return TOOLS_CLI
    raise AbilityError(
        f"目标运行时缺少 Ability 工具：{python}\n"
        f"需要 {TOOLS_BUILD} 或 {TOOLS_CLI}（ghrah-core P0/P1）。"
    )


def run_module(python: Path, module: str, argv: Sequence[str]) -> int:
    """把一次 Ability 调用原样转交给目标解释器，返回子进程退出码。

    直接继承宿主 stdio/stdin/环境：不 capture 输出（人类模式保持流式）、不注入
    ``PYTHONPATH``、不设超时（build/install 可能很久）、不改写退出码。
    ``KeyboardInterrupt`` 不吞（终端把 Ctrl+C 同时发给父子进程）；解释器无法
    启动等 ``OSError`` 原样抛出。
    """
    completed = subprocess.run([str(python), "-m", module, *argv], check=False)
    return completed.returncode


# ── 旧用户级存储提示 ─────────────────────────────────────────


def legacy_user_store_path() -> Path | None:
    """旧的用户级能力存储根 ``<ghrah_root>/ability``；存在目录时返回路径，否则 None。

    用户级存储已作废（改为发行版级 ``<distro>/ability``）。根取自 ``ghrah_root()``，
    因此 ``GHRAH_HOME`` 重定向同样生效，测试不会碰真实 ``~/.ghrah``。
    """
    path = ghrah_root() / "ability"
    return path if path.is_dir() else None


def warn_legacy_store() -> bool:
    """旧用户级存储存在时向 **stderr** 提示"已不再使用"；返回是否曾提示。

    只写 stderr，不污染 ``--json`` 的 stdout；供 ``ghrah self doctor`` 与
    ``ghrah ability ...`` 复用。每次调用都会重新探测（不做缓存），便于同一进程内
    的多个命令各自提示。
    """
    legacy = legacy_user_store_path()
    if legacy is None:
        return False
    print(
        f"[提示] 检测到旧的用户级能力存储 {legacy}；该目录已不再使用，"
        "能力包按发行版存放在 <发行版>/ability 下。",
        file=sys.stderr,
    )
    return True
