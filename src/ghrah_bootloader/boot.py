# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot 编排：解析引导目标 → 组装 OS/前端子进程 spawn 计划 → 监督与收束。

- OS 进程命令取发行版 env（``env/.venv/bin/ghrah-subject-server``），
  ``cwd`` 为 ``current/`` 稳定路径；数据面五项 + 端口全量 env 注入，
  指向发行版数据盘（版本无关，不落任何版本的默认位）。
- 引导器不 import ghrah：OS 的 ``ghrah.*`` 全部来自发行版 env（其 ``.pth``
  指向 ``current/``，重指即切换导入源）。
- 监督：OS 子进程意外退出 → 收束全部子进程并以非零码退出；收到
  SIGINT/SIGTERM → 逆序优雅收束（webui → OS）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from ghrah_bootloader.errors import BootloaderError
from ghrah_bootloader.layout import BootToml, DistroLayout, DistroToml, ghrah_root
from ghrah_bootloader.lifecycle import DEFAULT_HOST, BootStack, StackConfig

logger = logging.getLogger(__name__)

_ENV_DB_PATH = "GHRAH_SUBJECT_DB_PATH"
_ENV_TASKSTORE_DB = "GHRAH_SUBJECT_TASKSTORE_DB"
_ENV_MANIFEST_ROOT = "GHRAH_SUBJECT_MANIFEST_ROOT"
_ENV_PROJECT_ROOT_TEMPLATE = "GHRAH_SUBJECT_PROJECT_DEFAULT_ROOT_LOCATOR_TEMPLATE"
_ENV_WORKSPACE_ROOT = "GHRAH_SUBJECT_WORKSPACE_ROOT"
_ENV_SERVER_HOST = "GHRAH_SUBJECT_SERVER_HOST"
_ENV_SERVER_PORT = "GHRAH_SUBJECT_SERVER_PORT"

_WEBUI_PACKAGE_RELPATH = ("ghrah-observer-webui", "packages", "observer-web")
_SUPERVISOR_POLL_SECONDS = 0.5


@dataclass
class BootTarget:
    """已解析的引导目标（发行版 + 版本树 + 配置视图）。"""

    distro: str
    version: str
    tree: Path
    layout: DistroLayout
    distro_toml: DistroToml


def resolve_data_root(layout: DistroLayout, distro_toml: DistroToml) -> Path:
    """数据盘根：``[data] root`` 覆盖（可含 ``~``），默认 ``<distro>/data``。"""
    if distro_toml.data_root:
        return Path(os.path.expanduser(distro_toml.data_root))
    return layout.data


def build_data_plane_env(data_root: Path) -> dict[str, str]:
    """数据面五项 env：全部指向数据盘，不落任何版本的默认位。"""
    return {
        _ENV_DB_PATH: str(data_root / "subject.db"),
        _ENV_TASKSTORE_DB: str(data_root / "taskstore.sqlite3"),
        _ENV_MANIFEST_ROOT: str(data_root / "manifests"),
        _ENV_PROJECT_ROOT_TEMPLATE: str(data_root / "projects" / "{project_id}"),
        _ENV_WORKSPACE_ROOT: str(data_root / "workspace"),
    }


def ensure_data_dirs(data_root: Path) -> None:
    """物化数据盘目录结构（库文件由 OS 自建；目录由引导器保证）。"""
    for sub in ("manifests", "projects", "workspace"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)


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
) -> BootTarget:
    """解析引导目标：``--distro`` > boot.toml default；版本取 ``current`` 指向。

    版本切换（门禁 → 快照 → 重指 ``current``）由 install / runtime update
    承载；boot 只从 ``current`` 引导，显式 ``--version`` 不一致时拒绝。
    """
    base = root or ghrah_root()
    boot = BootToml.load(base)
    distro = distro_arg or (boot.default.split("/", 1)[0] if boot.default else "")
    if not distro:
        raise BootloaderError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
    distro_root = base / distro
    if not distro_root.is_dir():
        raise BootloaderError(f"发行版未安装: {distro}（{distro_root} 不存在）")
    layout = DistroLayout(root=distro_root)
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
            "boot.toml default=%s 与 current=%s 不一致，按 current 引导",
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


async def run_boot(
    target: BootTarget,
    *,
    no_webui: bool = False,
    no_browser: bool = False,
    subject_port: int | None = None,
    webui_port: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    """引导并监督：物化数据盘 → spawn OS（+ 前端）→ 健康检查 → 运行至停止。

    Args:
        target: ``resolve_boot_target`` 的解析结果。
        no_webui: 不启动前端子进程（也无浏览器）。
        no_browser: 不打开浏览器。
        subject_port / webui_port: 端口覆盖（默认取 distro.toml）。
        stop_event: 外部停止信号（默认自建，并接管 SIGINT/SIGTERM）。

    Returns:
        0：停止信号收到并完成逆序收束；1：OS 子进程意外退出（其余已收束）。
    """
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
