# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot 编排：解析引导目标 → 组装 OS/前端（代内入口 + 静态 serve）→ 监督与收束。

- OS 入口经 ``current`` 进代（``current/env/.venv/bin/ghrah-subject-server``）；
  OS ``cwd`` 为 ``current/`` 稳定路径；数据面五项 + 端口全量 env 注入，
  指向发行版数据盘（版本无关，不落任何版本的默认位）。
- webui 默认静态 serve 基座产物 dist（代目标：``versions/<base>/observer-web/dist``，
  index.html 响应注入 WS URL）；``--webui-dev`` 显式走开发域源码树。
- 引导器不 import ghrah：OS 的 ``ghrah.*`` 全部来自代内 env。
- 救援：``--ref last-good`` 取 last-good 符号链接（缺失回退
  ``boot.toml.last_good``）并重指 ``current``；``--restore-snapshot``
  先恢复最近快照（二次确认）再重指启动。
- 健康检查通过后更新 ``last-good`` 符号链接 + ``boot.toml.last_good``
  （失败仅告警，不阻断启动）。
- 监督：OS 子进程意外退出 → 收束全部子进程并以非零码退出；收到
  SIGINT/SIGTERM → 逆序优雅收束（webui → OS）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ghrah_toolchain import snapshot
from ghrah_toolchain.errors import ToolchainError
from ghrah_toolchain.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    build_data_plane_env,
    ensure_data_dirs,
    ghrah_root,
    resolve_data_root,
)
from ghrah_toolchain.lifecycle import DEFAULT_HOST, BootStack, StackConfig, port_in_use
from ghrah_toolchain.materialize import read_generation
from ghrah_toolchain.runtime_ref import (
    _pid_alive,
    acquire_distro_lock,
    repoint_current,
)

# 数据面路径助手自 layout 迁入；此处保留 re-export 视图（历史导入面不破坏）
__all__ = [
    "BootTarget",
    "boot_pid_path",
    "build_data_plane_env",
    "ensure_data_dirs",
    "os_server_command",
    "resolve_boot_target",
    "resolve_data_root",
    "resolve_os_server",
    "run_boot",
    "stop_boot_session",
    "webui_dev_dir",
    "webui_static_root",
]

logger = logging.getLogger(__name__)

_ENV_SERVER_HOST = "GHRAH_SUBJECT_SERVER_HOST"
_ENV_SERVER_PORT = "GHRAH_SUBJECT_SERVER_PORT"

_WEBUI_PACKAGE_RELPATH = ("ghrah-observer-webui", "packages", "observer-web")
_SUPERVISOR_POLL_SECONDS = 0.5
_LAST_GOOD_REF = "last-good"
_BOOT_PID_NAME = ".boot.pid"


def boot_pid_path(distro_root: Path) -> Path:
    """boot 会话 pidfile 路径。"""
    return distro_root / _BOOT_PID_NAME


def _write_boot_pid(distro_root: Path) -> None:
    """写 boot pidfile；已存在且 pid 活着 → 拒绝双启（陈旧则回收）。

    探测不可用（Windows 无 ctypes 等非预期态）→ fail-closed 拒绝并给出
    人工处置指引（mtime 回退对长跑会话不安全——会误回收活会话致双启）。
    """
    path = boot_pid_path(distro_root)
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip() or "0")
        except OSError, ValueError:
            old = 0
        alive = _pid_alive(old)
        if alive is None:
            raise ToolchainError(
                f"无法探测旧 boot 会话 pid={old} 是否存活 ({path})；"
                "请人工确认无会话运行后删除该文件再试"
            )
        if alive:
            raise ToolchainError(f"已有 boot 会话在运行 (pid={old}, {path})；请先 ghrah stop")
        logger.warning("回收陈旧 boot pidfile: %s (pid=%s)", path, old)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _clear_boot_pid(distro_root: Path) -> None:
    """移除 boot pidfile（仅当仍属于本进程时）。"""
    path = boot_pid_path(distro_root)
    try:
        current = int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError, OSError, ValueError:
        return
    if current == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink()


@dataclass
class BootTarget:
    """已解析的引导目标（发行版 + 版本树 + 配置视图）。"""

    distro: str
    version: str
    tree: Path
    layout: DistroLayout
    distro_toml: DistroToml

    @property
    def label(self) -> str:
        return f"{self.distro}/{self.version}"


def os_server_entry(venv: Path) -> Path:
    """OS 入口路径（``ghrah-subject-server``；不做存在性校验）。"""
    if os.name == "nt":
        return venv / "Scripts" / "ghrah-subject-server.exe"
    return venv / "bin" / "ghrah-subject-server"


def os_server_command(venv: Path) -> list[str]:
    """OS 进程命令：给定 venv 的 ``ghrah-subject-server`` 入口（纯 venv→命令助手）。"""
    server = os_server_entry(venv)
    if not server.is_file():
        raise ToolchainError(f"OS server 入口缺失: {server}（发行版 env 未物化，请先完成安装）")
    return [str(server)]


def resolve_os_server(layout: DistroLayout) -> list[str]:
    """OS 入口：``current/env/.venv``（代内 per-gen env）。

    发行级 ``env/`` 兜底已退役（worktree 形态不再支持）；
    代内 env 缺失即报错。
    """
    gen_venv = layout.current / "env" / ".venv"
    if os_server_entry(gen_venv).is_file():
        return os_server_command(gen_venv)
    raise ToolchainError(f"OS server 入口缺失: {gen_venv}（当前代 env 未物化，请先完成安装）")


def webui_static_root(target: BootTarget) -> Path:
    """静态 serve 根：代目标 → 基座树 ``versions/<base>/observer-web/dist``。

    引导目标恒为组合代（``read_generation`` 必命中）；dist 缺失 →
    fail-closed（基座产物不完整）。
    """
    info = read_generation(target.tree)
    if info is None:
        raise ToolchainError(f"引导目标不是组合代: {target.tree}（current 只指向 generations/）")
    dist = target.layout.versions / info.base_version / "observer-web" / "dist"
    if not dist.is_dir():
        raise ToolchainError(
            f"基座产物不完整: {dist} 缺失（observer-web dist）；重新 install 修复基座树"
        )
    return dist


def webui_dev_dir(target: BootTarget, dev_dir_arg: str | Path | None) -> Path:
    """``--webui-dev`` 开发域目录：显式参 > ``[distro.repo].path`` 推导；均无效 fail-closed。"""
    if dev_dir_arg is not None:
        dev_dir = Path(dev_dir_arg).expanduser()
        if not dev_dir.is_dir():
            raise ToolchainError(
                f"--webui-dev-dir 无效: {dev_dir} 不是目录；"
                "应指向开发域源码树的 observer-web 包目录"
            )
        return dev_dir
    repo_path = target.distro_toml.repo_path
    if repo_path:
        dev_dir = Path(repo_path).expanduser().joinpath(*_WEBUI_PACKAGE_RELPATH)
        if dev_dir.is_dir():
            return dev_dir
    raise ToolchainError(
        "开发域路径不可用：提供 --webui-dev-dir（开发域源码树的 observer-web 包目录），"
        "或在 distro.toml [distro.repo].path 配置有效源仓路径"
    )


def _within_managed_segment(layout: DistroLayout, tree: Path) -> bool:
    """树是否落在受管段 ``generations/`` 内的单层目标。

    current/last-good 只允许指向组合代；``versions/`` 是基座产物库
    （枚举显示面），不再作为引导目标。
    """
    try:
        rel = tree.relative_to(layout.generations.resolve())
    except ValueError, OSError:
        return False
    return len(rel.parts) == 1


def resolve_boot_target(
    distro_arg: str | None,
    version_arg: str | None,
    *,
    root: Path | None = None,
    ref: str | None = None,
) -> BootTarget:
    """解析引导目标：``--distro`` > boot.toml default；版本取 ``current`` 指向。

    ``ref="last-good"``：目标取 ``last-good`` 符号链接（缺失回退
    ``boot.toml.last_good``），跳过 ``--version`` 一致性校验；其余 ref 值拒绝。
    版本切换（门禁 → 快照 → 重指 ``current``）由 install / runtime update
    承载；boot 只从解析出的目标引导。
    """
    if ref is not None and ref != _LAST_GOOD_REF:
        raise ToolchainError(f"--ref 仅接受 last-good（收到 {ref!r}）")
    base = root or ghrah_root()
    boot = BootToml.load(base)
    distro = distro_arg or (boot.default.split("/", 1)[0] if boot.default else "")
    if not distro and ref == _LAST_GOOD_REF and boot.last_good and "/" in boot.last_good:
        distro = boot.last_good.split("/", 1)[0]
    if not distro:
        raise ToolchainError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
    distro_root = base / distro
    if not distro_root.is_dir():
        raise ToolchainError(f"发行版未安装: {distro}（{distro_root} 不存在）")
    layout = DistroLayout(root=distro_root)
    if ref == _LAST_GOOD_REF:
        tree = _resolve_last_good_tree(layout, boot, distro)
        version = tree.name
    else:
        if not layout.current.exists():
            raise ToolchainError(
                f"current 未指向任何版本: {layout.current}（先安装发行版并重指 current）"
            )
        tree = layout.current.resolve()
        if not _within_managed_segment(layout, tree):
            raise ToolchainError(f"current 指向 generations/ 之外的路径: {tree}")
        version = tree.name
        if version_arg and version_arg != version:
            raise ToolchainError(
                f"--version {version_arg} 与 current 指向的 {version} 不一致；"
                "版本切换由 install / runtime update 承载（boot 不做重指）"
            )
    if boot.default and boot.default != f"{distro}/{version}":
        logger.warning(
            "boot.toml default=%s 与引导目标 %s 不一致，按目标引导",
            boot.default,
            version,
        )
    return BootTarget(
        distro=distro,
        version=version,
        tree=tree,
        layout=layout,
        distro_toml=DistroToml.load(layout.root),
    )


def _resolve_last_good_tree(layout: DistroLayout, boot: BootToml, distro: str) -> Path:
    """last-good 目标树：符号链接优先，缺失回退 ``boot.toml.last_good``。

    查段固定 ``generations/``。
    """
    if layout.last_good.is_symlink() or layout.last_good.exists():
        tree = layout.last_good.resolve()
        if not _within_managed_segment(layout, tree):
            raise ToolchainError(f"last-good 指向 generations/ 之外的路径: {tree}")
        return tree
    if boot.last_good and "/" in boot.last_good:
        lg_distro, lg_ver = boot.last_good.split("/", 1)
        if lg_distro == distro:
            tree = (layout.generations / lg_ver).resolve()
            if tree.is_dir():
                return tree
    raise ToolchainError(
        f"last-good 未设置或不可用: {layout.last_good} / boot.toml last_good={boot.last_good!r}"
    )


async def run_boot(
    target: BootTarget,
    *,
    no_webui: bool = False,
    no_browser: bool = False,
    subject_port: int | None = None,
    webui_port: int | None = None,
    stop_event: asyncio.Event | None = None,
    restore_snapshot: bool = False,
    yes: bool = False,
    webui_dev: bool = False,
    webui_dev_dir_arg: str | Path | None = None,
    on_ready: Callable[[], None] | None = None,
) -> int:
    """引导并监督：物化数据盘 → spawn OS（+ 前端）→ 健康检查 → 运行至停止。

    Args:
        target: ``resolve_boot_target`` 的解析结果。
        no_webui: 不启动前端（也无浏览器）。
        no_browser: 不打开浏览器。
        subject_port / webui_port: 端口覆盖（默认取 distro.toml）。
        stop_event: 外部停止信号（默认自建，并接管 SIGINT/SIGTERM）。
        restore_snapshot: 救援数据回滚——恢复最近快照并重指 ``current``
            到引导目标版本（要求目标经 ``--ref last-good`` 解析）。
        yes: 跳过恢复前的交互二次确认（非交互 stdin 时必须提供）。
        webui_dev: 前端走 pnpm dev 开发域（默认按目标形态：代目标静态
            serve 基座 dist；过渡期 versions 树沿用旧 dev 路径）。
        webui_dev_dir_arg: 开发域目录（显式 > ``[distro.repo].path`` 推导）。

    Returns:
        0：停止信号收到并完成逆序收束；1：OS 子进程意外退出（其余已收束）。
    """
    if restore_snapshot:
        _perform_snapshot_restore(target, yes=yes)
    elif not target.layout.current.exists() or target.layout.current.resolve() != target.tree:
        # --ref last-good 救援且 current 指向他处：重指 current 到引导目标
        # （OS cwd / webui / 自管 .pth 均经 current 段生效）
        with acquire_distro_lock(target.layout.root):
            repoint_current(target.layout.root, target.version)
        logger.info("救援重指 current → generations/%s", target.version)
    data_root = resolve_data_root(target.layout, target.distro_toml)
    ensure_data_dirs(data_root)
    sport = subject_port if subject_port is not None else target.distro_toml.subject_port
    wport = webui_port if webui_port is not None else target.distro_toml.webui_port
    config = StackConfig(
        host=DEFAULT_HOST,
        subject_port=sport,
        webui_port=wport,
        open_browser=not no_browser and not no_webui,
    )
    os_env = {
        **build_data_plane_env(data_root),
        _ENV_SERVER_HOST: config.host,
        _ENV_SERVER_PORT: str(sport),
    }
    # 形态判定装配：--webui-dev 显式走开发域；否则静态 serve 基座 dist。
    webui_kwargs: dict[str, object] = {}
    if not no_webui:
        if webui_dev or webui_dev_dir_arg is not None:
            webui_kwargs["webui_dir"] = webui_dev_dir(target, webui_dev_dir_arg)
        else:
            webui_kwargs["webui_static_root"] = webui_static_root(target)
            webui_kwargs["webui_ws_url"] = config.subject_ws_url()
    stack = BootStack(
        config,
        os_command=resolve_os_server(target.layout),
        os_cwd=target.layout.current,
        os_env=os_env,
        **webui_kwargs,
    )
    logger.info(
        "boot %s @ %s (tree=%s data=%s os_port=%d)",
        target.distro,
        target.version,
        target.tree,
        data_root,
        sport,
    )
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except NotImplementedError, RuntimeError, ValueError:
            logger.debug("signal handler unavailable: %s", sig)
    _write_boot_pid(target.layout.root)
    try:
        starting = asyncio.create_task(stack.start())
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((starting, stopping), return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set() and not starting.done():
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)
                return 0
            await starting
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        if on_ready is not None:
            on_ready()
        try:
            _update_last_good(target)
        except Exception:
            logger.warning("last-good 更新失败（不阻断启动）", exc_info=True)
        while True:
            if stop.is_set():
                logger.info("停止信号收到，逆序收束子进程（webui → OS）")
                return 0
            proc = stack.os_process
            if proc is not None and proc.poll() is not None:
                logger.error("OS 子进程意外退出 (code=%s)，收束其余子进程", proc.returncode)
                return 1
            frontend = getattr(stack, "webui_process", None)
            if frontend is not None and frontend.poll() is not None:
                logger.error("前端子进程异常退出 (code=%s)，收束全栈", frontend.returncode)
                return 1
            await asyncio.sleep(_SUPERVISOR_POLL_SECONDS)
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError, RuntimeError, ValueError:
                logger.debug("signal handler removal failed: %s", sig)
        try:
            await asyncio.shield(stack.stop())
        finally:
            _clear_boot_pid(target.layout.root)


# ── 救援与 known-good ──────────────────────────────────


def _perform_snapshot_restore(target: BootTarget, *, yes: bool) -> None:
    """恢复最近快照 → 重指 ``current`` 到引导目标版本（全程持锁，二次确认）。"""
    distro_root = target.layout.root
    with acquire_distro_lock(distro_root):
        snaps = snapshot.list_snapshots(target.layout, target.distro_toml)
        if not snaps:
            raise ToolchainError("无快照可恢复（snapshots/ 为空）")
        latest = snaps[-1]
        if not yes:
            if not sys.stdin.isatty():
                raise ToolchainError("非交互环境：--restore-snapshot 需配合 --yes")
            answer = input(
                f"将恢复快照 {latest.name} 并重指 current 到 {target.version}；"
                "快照之后的写入窗口将丢失。确认恢复？[y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                raise ToolchainError("已取消快照恢复")
        restored = snapshot.restore(target.layout, target.distro_toml, latest)
        logger.info("快照已恢复: %s（数据回到旧版本 %s 方向）", latest.name, restored or "未知")
    repoint_current(distro_root, target.version)
    logger.info("救援重指 current → generations/%s", target.version)


def _update_last_good(target: BootTarget) -> None:
    """启动健康检查通过后：更新 ``last-good`` 符号链接 + ``boot.toml.last_good``。"""
    from ghrah_toolchain.instances import _locked
    from ghrah_toolchain.runtime_ref import repoint_last_good

    repoint_last_good(target.layout.root, target.version)
    with _locked(target.layout.root.parent):
        boot = BootToml.load(target.layout.root.parent)
        boot.last_good = target.label
        boot.save(target.layout.root.parent)


def stop_boot_session(
    distro: str,
    *,
    timeout: float = 15.0,
    subject_port: int | None = None,
    webui_port: int | None = None,
) -> int:
    """停止运行中的 boot 会话（前台 boot 的另一终端入口）。

    流程：读 pidfile → pid 已死则清文件提示「未在运行」→ 平台护栏
    （POSIX：``/proc/<pid>/cmdline``；Windows：进程映像名；防误杀）→
    终止协议（POSIX：SIGTERM；Windows：taskkill 优雅→强杀升级链）→
    轮询 pid 退出与端口释放。

    Returns:
        0：会话已退出且端口释放；1：未在运行（含陈旧 pidfile 清理 /
        护栏拒绝）；2：超时。
    """
    import signal as _signal
    import time as _time

    root = ghrah_root()
    distro_root = root / distro
    pidfile = boot_pid_path(distro_root)
    if not pidfile.is_file():
        print(f"[{distro}] 无 boot pidfile（未在运行）")
        return 1
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError) as e:
        raise ToolchainError(f"boot pidfile 损坏: {pidfile}: {e}") from e
    if _session_exited(pid):
        with contextlib.suppress(OSError):
            pidfile.unlink()
        print(f"[{distro}] boot 会话已不在运行（清理陈旧 pidfile，pid={pid}）")
        return 1
    _stop_guard_rc = _stop_guard(distro, pid)
    if _stop_guard_rc is not None:
        return _stop_guard_rc
    if os.name == "nt":
        return _stop_windows(distro, distro_root, pid, timeout, subject_port, webui_port)
    print(f"[{distro}] 向 boot 会话 pid={pid} 发送 SIGTERM（逆序收束 webui → OS）")
    os.kill(pid, _signal.SIGTERM)
    deadline = _time.monotonic() + timeout
    while not _session_exited(pid):
        if _time.monotonic() >= deadline:
            print(
                f"[{distro}] 等待超时（{timeout}s）：pid {pid} 仍在；不强杀，"
                "请人工核查（ps -fp <pid>）后处置"
            )
            return 2
        _time.sleep(0.2)
    ports_busy = _busy_ports(distro_root, subject_port, webui_port)
    if ports_busy:
        print(f"[{distro}] 会话已退出，但端口仍被占用: {ports_busy}（可能是残留子进程）")
        return 2
    print(f"[{distro}] boot 会话已停止，端口已释放")
    return 0


def _cmdline_is_ghrah_family(cmdline: str) -> bool:
    """``/proc/<pid>/cmdline`` 是否属于 ghrah 工具链家族（stop 防误杀护栏）。

    接受入口脚本名（``ghrah`` / ``ghrah-toolchain`` / ``ghrah-bootloader``）
    与 ``python -m <包>[.<子模块>]`` 两种形态；后台监督进程的实际 argv 是
    ``python -m ghrah_toolchain.supervisor``，不能只匹配顶层包名。
    """
    words = cmdline.split()
    scripts = {"ghrah", "ghrah-toolchain", "ghrah-bootloader"}
    if any(Path(word).name in scripts for word in words[:2]):
        return True
    for index, word in enumerate(words[:2]):
        if word != "-m" or index + 1 >= len(words):
            continue
        if words[index + 1].split(".", 1)[0] in {"ghrah_toolchain", "ghrah_bootloader"}:
            return True
    return False


def _stop_guard(distro: str, pid: int) -> int | None:
    """平台护栏：pid 是否仍属于 ghrah-toolchain 家族；拒绝时返回 rc=1。

    - POSIX：``/proc/<pid>/cmdline``（无 /proc 平台跳过并告警）；
    - Windows：进程映像名（OpenProcess + QueryFullProcessImageNameW），
      探测不可用同样跳过并告警。pidfile 本身已是一道护栏。
    """
    if os.name == "posix":
        # 护栏依赖 /proc（Linux 独有；macOS 等 posix 平台无 /proc）。
        # 存在性 + 读取双守卫：不可用时跳过 cmdline 校验并告警——pidfile
        # 本身已是一道护栏，读失败不应让 stop 崩溃（跨平台回归位）。
        cmdline = _read_cmdline_guard(pid)
        if cmdline is None:
            print(
                f"[{distro}] 告警：无法读取 /proc/{pid}/cmdline，"
                "跳过 cmdline 护栏，仅凭 pidfile 继续"
            )
            return None
        if not _cmdline_is_ghrah_family(cmdline):
            print(
                f"[{distro}] 拒绝：pid {pid} 的 cmdline 不属于 ghrah-toolchain（{cmdline!r}）；"
                "请人工核查后处理"
            )
            return 1
        return None
    from ghrah_toolchain import winproc

    image = winproc.process_image(pid)
    if image is None:
        print(
            f"[{distro}] 告警：无法查询 pid {pid} 的进程映像名，跳过映像名护栏，仅凭 pidfile 继续"
        )
        return None
    base = image.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
    if (
        base not in {"ghrah.exe", "ghrah-toolchain.exe", "ghrah-bootloader.exe"}
        and "python" not in base
    ):
        print(
            f"[{distro}] 拒绝：pid {pid} 的映像名不属于 ghrah-toolchain 家族"
            f"（{image}）；请人工核查后处理"
        )
        return 1
    return None


def _taskkill(pid: int, *, force: bool) -> None:
    """Windows 终止协议原语：优雅（WM_CLOSE 语义）或强杀（进程树）。"""
    cmd = ["taskkill", "/PID", str(pid)]
    if force:
        cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
    import subprocess as _subprocess

    with contextlib.suppress(Exception):
        _subprocess.run(cmd, capture_output=True, timeout=10, check=False)


def _stop_windows(
    distro: str,
    distro_root: Path,
    pid: int,
    timeout: float,
    subject_port: int | None,
    webui_port: int | None,
) -> int:
    """Windows stop 升级链：taskkill 优雅 → 轮询 → ``/F /T`` 强杀。

    优雅段返回码不检查（控制台进程的 WM_CLOSE 多半无效——诚实面：
    实际语义≈「强杀升级链」；``/T`` 按进程树终止兜底 OS/webui 孤儿）。
    对齐 lifecycle.terminate_process 已有的优雅→强杀模式，不引入第二种
    终止协议。
    """
    import time as _time

    print(f"[{distro}] 向 boot 会话 pid={pid} 发送 taskkill（优雅，超时升级 /F /T）")
    _taskkill(pid, force=False)
    deadline = _time.monotonic() + timeout
    while not _session_exited(pid):
        if _time.monotonic() >= deadline:
            break
        _time.sleep(0.2)
    if _session_exited(pid):
        return _stop_ports_check(distro, distro_root, subject_port, webui_port)
    print(f"[{distro}] 优雅终止超时（{timeout}s），升级强杀: taskkill /F /T /PID {pid}")
    _taskkill(pid, force=True)
    grace = _time.monotonic() + 5.0
    while not _session_exited(pid):
        if _time.monotonic() >= grace:
            print(
                f'[{distro}] 强杀后 pid {pid} 仍在；请人工核查（tasklist /FI "PID eq {pid}"）后处置'
            )
            return 2
        _time.sleep(0.2)
    return _stop_ports_check(distro, distro_root, subject_port, webui_port)


def _stop_ports_check(
    distro: str, distro_root: Path, subject_port: int | None, webui_port: int | None
) -> int:
    ports_busy = _busy_ports(distro_root, subject_port, webui_port)
    if ports_busy:
        hint = "netstat -ano | findstr :<port>" if os.name == "nt" else "ss -ltnp | grep :<port>"
        print(f"[{distro}] 会话已退出，但端口仍被占用: {ports_busy}（可能是残留子进程；{hint}）")
        return 2
    print(f"[{distro}] boot 会话已停止，端口已释放")
    return 0


def _read_cmdline_guard(pid: int) -> str | None:
    """读取 ``/proc/<pid>/cmdline`` 作为防误杀护栏；不可用返回 ``None``。

    双守卫：``/proc`` 存在性（macOS 等 posix 无 /proc）+ 读取容错。
    返回 ``None`` 时调用方应跳过护栏并告警，而非崩溃。
    """
    if not Path("/proc").is_dir():
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _session_exited(pid: int) -> bool:
    """会话是否已退出：pid 不存活，或 POSIX 下处于僵尸态（Z，未被 reap）。

    僵尸态进程已执行完毕，仅待父进程 wait；对调用方语义等同已退出。
    探测不可用（None）视为未退出（fail-closed：stop 走超时路径而非误报）。
    """
    alive = _pid_alive(pid)
    if alive is False:
        return True
    if alive is None:
        return False
    if os.name == "posix":
        with contextlib.suppress(OSError):
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm 可能含空格/括号，取最后一个 ')' 之后的首个字段（state）
            state = stat.rsplit(")", 1)[1].split()[0]
            return state == "Z"
    return False


def _busy_ports(distro_root: Path, subject_port: int | None, webui_port: int | None) -> list[int]:
    """检查发行版端口占用（默认取 distro.toml）。"""
    toml = DistroToml.load(distro_root)
    ports = [
        subject_port if subject_port is not None else toml.subject_port,
        webui_port if webui_port is not None else toml.webui_port,
    ]
    return [p for p in ports if port_in_use("127.0.0.1", p)]
