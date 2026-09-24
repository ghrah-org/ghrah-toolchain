# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot 编排：解析引导目标 → 组装 OS/前端子进程 spawn 计划 → 监督与收束。

- OS 进程命令取发行版 env（``env/.venv/bin/ghrah-subject-server``），
  ``cwd`` 为 ``current/`` 稳定路径；数据面五项 + 端口全量 env 注入，
  指向发行版数据盘（版本无关，不落任何版本的默认位）。
- 引导器不 import ghrah：OS 的 ``ghrah.*`` 全部来自发行版 env（其 ``.pth``
  指向 ``current/``，重指即切换导入源）。
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
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from ghrah_bootloader import snapshot
from ghrah_bootloader.errors import BootloaderError
from ghrah_bootloader.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    build_data_plane_env,
    ensure_data_dirs,
    ghrah_root,
    resolve_data_root,
)
from ghrah_bootloader.lifecycle import DEFAULT_HOST, BootStack, StackConfig
from ghrah_bootloader.runtime_ref import acquire_distro_lock, repoint_current

# 数据面路径助手自 layout 迁入；此处保留 re-export 视图（历史导入面不破坏）
__all__ = [
    "BootTarget",
    "build_data_plane_env",
    "ensure_data_dirs",
    "os_server_command",
    "resolve_boot_target",
    "resolve_data_root",
    "run_boot",
]

logger = logging.getLogger(__name__)

_ENV_SERVER_HOST = "GHRAH_SUBJECT_SERVER_HOST"
_ENV_SERVER_PORT = "GHRAH_SUBJECT_SERVER_PORT"

_WEBUI_PACKAGE_RELPATH = ("ghrah-observer-webui", "packages", "observer-web")
_SUPERVISOR_POLL_SECONDS = 0.5
_LAST_GOOD_REF = "last-good"


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


def os_server_command(venv: Path) -> list[str]:
    """OS 进程命令：发行版 venv 的 ``ghrah-subject-server`` 入口。"""
    if os.name == "nt":
        server = venv / "Scripts" / "ghrah-subject-server.exe"
    else:
        server = venv / "bin" / "ghrah-subject-server"
    if not server.is_file():
        raise BootloaderError(f"OS server 入口缺失: {server}（发行版 env 未物化，请先完成安装）")
    return [str(server)]


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
        raise BootloaderError(f"--ref 仅接受 last-good（收到 {ref!r}）")
    base = root or ghrah_root()
    boot = BootToml.load(base)
    distro = distro_arg or (boot.default.split("/", 1)[0] if boot.default else "")
    if not distro and ref == _LAST_GOOD_REF and boot.last_good and "/" in boot.last_good:
        distro = boot.last_good.split("/", 1)[0]
    if not distro:
        raise BootloaderError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
    distro_root = base / distro
    if not distro_root.is_dir():
        raise BootloaderError(f"发行版未安装: {distro}（{distro_root} 不存在）")
    layout = DistroLayout(root=distro_root)
    if ref == _LAST_GOOD_REF:
        tree = _resolve_last_good_tree(layout, boot, distro)
        version = tree.name
    else:
        if not layout.current.exists():
            raise BootloaderError(
                f"current 未指向任何版本: {layout.current}（先安装发行版并重指 current）"
            )
        tree = layout.current.resolve()
        try:
            tree.relative_to(layout.versions.resolve())
        except ValueError as e:
            raise BootloaderError(f"current 指向 versions/ 之外的路径: {tree}") from e
        version = tree.name
        if version_arg and version_arg != version:
            raise BootloaderError(
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
    """last-good 目标树：符号链接优先，缺失回退 ``boot.toml.last_good``。"""
    if layout.last_good.is_symlink() or layout.last_good.exists():
        tree = layout.last_good.resolve()
        try:
            tree.relative_to(layout.versions.resolve())
        except ValueError as e:
            raise BootloaderError(f"last-good 指向 versions/ 之外的路径: {tree}") from e
        return tree
    if boot.last_good and "/" in boot.last_good:
        lg_distro, lg_ver = boot.last_good.split("/", 1)
        if lg_distro == distro:
            tree = (layout.versions / lg_ver).resolve()
            if tree.is_dir():
                return tree
    raise BootloaderError(
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
) -> int:
    """引导并监督：物化数据盘 → spawn OS（+ 前端）→ 健康检查 → 运行至停止。

    Args:
        target: ``resolve_boot_target`` 的解析结果。
        no_webui: 不启动前端子进程（也无浏览器）。
        no_browser: 不打开浏览器。
        subject_port / webui_port: 端口覆盖（默认取 distro.toml）。
        stop_event: 外部停止信号（默认自建，并接管 SIGINT/SIGTERM）。
        restore_snapshot: 救援数据回滚——恢复最近快照并重指 ``current``
            到引导目标版本（要求目标经 ``--ref last-good`` 解析）。
        yes: 跳过恢复前的交互二次确认（非交互 stdin 时必须提供）。

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
        logger.info("救援重指 current → versions/%s", target.version)
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
    webui_dir = None
    if not no_webui:
        webui_dir = target.layout.current.joinpath(*_WEBUI_PACKAGE_RELPATH)
    stack = BootStack(
        config,
        os_command=os_server_command(target.layout.venv),
        os_cwd=target.layout.current,
        os_env=os_env,
        webui_dir=webui_dir,
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("signal handler unavailable: %s", sig)
    try:
        await stack.start()
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
            await asyncio.sleep(_SUPERVISOR_POLL_SECONDS)
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                logger.debug("signal handler removal failed: %s", sig)
        await asyncio.shield(stack.stop())


# ── 救援与 known-good（D7） ──────────────────────────────────


def _perform_snapshot_restore(target: BootTarget, *, yes: bool) -> None:
    """恢复最近快照 → 重指 ``current`` 到引导目标版本（全程持锁，二次确认）。"""
    distro_root = target.layout.root
    with acquire_distro_lock(distro_root):
        snaps = snapshot.list_snapshots(target.layout, target.distro_toml)
        if not snaps:
            raise BootloaderError("无快照可恢复（snapshots/ 为空）")
        latest = snaps[-1]
        if not yes:
            if not sys.stdin.isatty():
                raise BootloaderError("非交互环境：--restore-snapshot 需配合 --yes")
            answer = input(
                f"将恢复快照 {latest.name} 并重指 current 到 {target.version}；"
                "快照之后的写入窗口将丢失。确认恢复？[y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                raise BootloaderError("已取消快照恢复")
        restored = snapshot.restore(target.layout, target.distro_toml, latest)
        logger.info("快照已恢复: %s（数据回到旧版本 %s 方向）", latest.name, restored or "未知")
    repoint_current(distro_root, target.version)
    logger.info("救援重指 current → versions/%s", target.version)


def _update_last_good(target: BootTarget) -> None:
    """启动健康检查通过后：更新 ``last-good`` 符号链接 + ``boot.toml.last_good``。"""
    from ghrah_bootloader.runtime_ref import repoint_last_good

    repoint_last_good(target.layout.root, target.version)
    boot = BootToml.load(target.layout.root.parent)
    boot.last_good = target.label
    boot.save(target.layout.root.parent)
