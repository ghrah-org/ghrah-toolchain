# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""进程生命周期管理：spawn OS 子进程 + 前端子进程 + 逆序收束。

- ``_port_in_use``（loopback 双栈）、``_terminate_process``（POSIX 进程组
  SIGTERM→SIGKILL / Windows taskkill /T）、``_start_webui``（``pnpm dev
  --strictPort``、``.env.local`` 注入 ``VITE_GHRAH_SUBJECT_WS_URL``）、
  ``_wait_webui_ready``、``_wait_webui_port_released``、``_open_browser``。
- 删除：内嵌 Subject 装配（``_run_subject_server`` / ``_start_subject``）——
  Subject（OS）改为 ``spawn_os_process`` 拉起的独立子进程（out-of-process 硬要求）。
- ``stop()`` 逆序语义与幂等；``asyncio.shield(stop())`` 收口模式由调用方使用。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ghrah_bootloader.health import wait_for_server
from ghrah_bootloader.materialize import pnpm_command

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT_PORT = 4112
DEFAULT_WEBUI_PORT = 5173
DEFAULT_HOST = "127.0.0.1"
_WEBUI_ENV_FILE = ".env.local"
_WEBUI_ENV_KEY = "VITE_GHRAH_SUBJECT_WS_URL"


def port_in_use(host: str, port: int) -> bool:
    """探测 host:port 是否有服务在监听（loopback 双栈：IPv4 + IPv6）。

    vite dev server 以 ``localhost`` 监听时在部分系统仅绑 ``::1``，
    故对 loopback 地址额外探测 IPv6；任一栈可连通即视为占用。
    """
    probes: list[tuple[socket.AddressFamily, tuple[str, int]]] = [
        (socket.AF_INET, (host, port)),
    ]
    if host in ("localhost", "127.0.0.1"):
        probes.append((socket.AF_INET6, ("::1", port)))
    for family, addr in probes:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue
    return False


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    """终止子进程及其整个进程组（POSIX）/ 进程树（Windows taskkill /T）。

    安全护栏（防误伤无关进程组）：

    - 进程已退出（``poll()`` 非 None）→ 直接返回，不再发信号（pid 可能已被回收）；
    - 仅当 ``proc.pid`` 确为自建进程组组长（``getpgid(pid) == pid``，由 spawn
      时的 ``start_new_session=True`` 保证）才对整组发信号；
    - 组校验失败（进程已消失 / 非组长）→ 回退 ``proc.terminate()`` 单进程终止。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # terminate() 只杀直接子进程（cmd.exe），杀不到 pnpm 的 node 子进程树；
        # taskkill /T 按进程树终止。
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            proc.wait(timeout=5)
        except Exception:
            logger.debug("process taskkill error", exc_info=True)
        return
    # POSIX：pnpm 不转发 SIGTERM，仅杀 pnpm.mjs 会让 sh/vite/esbuild 孤儿化
    # 并继续占住端口，必须对整个进程组发信号。组长身份由 spawn 时的
    # start_new_session=True 保证；killpg 前显式复核，防 pid 回收误伤。
    pgid = proc.pid
    group_owned = False
    try:
        group_owned = os.getpgid(pgid) == pgid
    except (ProcessLookupError, PermissionError):
        logger.debug("process group check failed; falling back to terminate()", exc_info=True)
    if not group_owned:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        logger.debug("process group already gone", exc_info=True)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("process group did not exit after SIGTERM, escalating to SIGKILL")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            logger.debug("process group SIGKILL failed", exc_info=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("process pid=%d did not exit after SIGKILL", proc.pid)
    except Exception:
        logger.debug("process wait error", exc_info=True)


@dataclass
class StackConfig:
    """引导栈启动配置（不含内嵌装配——OS 是独立子进程）。"""

    host: str = DEFAULT_HOST
    subject_port: int = DEFAULT_SUBJECT_PORT
    webui_port: int = DEFAULT_WEBUI_PORT
    open_browser: bool = True

    def subject_http_url(self) -> str:
        return f"http://{self.host}:{self.subject_port}/health"

    def subject_ws_url(self) -> str:
        return f"ws://{self.host}:{self.subject_port}/ws"

    def webui_url(self) -> str:
        return f"http://localhost:{self.webui_port}"


class BootStack:
    """OS 子进程 + webui 子进程的生命周期管理。

    - OS（Subject + 内嵌 Core）：经 ``spawn_os_process`` 以发行版 env 的
      ``ghrah-subject-server`` 独立进程拉起（引导器不 import ghrah）。
    - webui dev server：``subprocess.Popen``（pnpm dev --strictPort）。

    所有资源在 ``stop()`` 中逆序释放（webui → OS）；重复 ``stop()`` 幂等；
    未 ``start()`` 即 ``stop()`` 不抛。
    """

    def __init__(
        self,
        config: StackConfig,
        *,
        os_command: list[str] | None = None,
        os_cwd: Path | None = None,
        os_env: dict[str, str] | None = None,
        webui_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._os_command = os_command
        self._os_cwd = os_cwd
        self._os_env = os_env
        self._webui_dir = webui_dir
        self._os_process: subprocess.Popen[bytes] | None = None
        self._webui_process: subprocess.Popen[bytes] | None = None
        self._webui_env_file: Path | None = None
        self._started = False

    @property
    def config(self) -> StackConfig:
        return self._config

    @property
    def os_process(self) -> subprocess.Popen[bytes] | None:
        return self._os_process

    def subject_ws_url(self) -> str:
        return self._config.subject_ws_url()

    def webui_url(self) -> str:
        return self._config.webui_url()

    async def start(self) -> None:
        """起 OS 子进程 → webui 子进程。任一层失败抛异常（已起的资源由 stop 释放）。"""
        await self._start_os()
        if self._webui_dir is not None:
            self._start_webui()
        self._started = True
        if self._config.open_browser:
            self._open_browser()

    async def stop(self) -> None:
        """逆序优雅退出（webui → OS）。幂等。"""
        if self._webui_process is not None:
            terminate_process(self._webui_process)
            self._webui_process = None
            await self._wait_webui_port_released()
        self._cleanup_webui_env_file()
        if self._os_process is not None:
            terminate_process(self._os_process)
            self._os_process = None
        self._started = False

    # ── OS 子进程（out-of-process） ──

    async def _start_os(self) -> None:
        """spawn 独立 OS 子进程（发行版 env 的 ghrah-subject-server）。"""
        if self._os_command is None:
            raise RuntimeError("os_command 未配置：BootStack 需要 OS 启动命令")
        self._check_port_free(
            self._config.host,
            self._config.subject_port,
            label="OS (subject) server",
        )
        env = os.environ.copy()
        if self._os_env:
            env.update(self._os_env)
        self._os_process = subprocess.Popen(
            self._os_command,
            cwd=str(self._os_cwd) if self._os_cwd is not None else None,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(
            "OS subprocess starting (pid=%d, port=%d)",
            self._os_process.pid,
            self._config.subject_port,
        )
        await wait_for_server(
            self._config.subject_http_url(), timeout=10.0, name="OS (subject) server"
        )

    # ── webui ──

    def _ensure_pnpm(self) -> None:
        if shutil.which("pnpm") is None:
            raise RuntimeError(
                "pnpm not found on PATH; install pnpm or ensure Node.js toolchain "
                "available before booting the webui."
            )
        if shutil.which("node") is None:
            raise RuntimeError("node not found on PATH; install Node.js before booting the webui.")

    def _write_webui_env_file(self, webui_dir: Path) -> None:
        env_path = webui_dir / _WEBUI_ENV_FILE
        env_path.write_text(
            f"{_WEBUI_ENV_KEY}={self._config.subject_ws_url()}\n",
            encoding="utf-8",
        )
        self._webui_env_file = env_path
        logger.info("webui env injected: %s=%s", _WEBUI_ENV_KEY, self._config.subject_ws_url())

    def _cleanup_webui_env_file(self) -> None:
        if self._webui_env_file is not None:
            try:
                self._webui_env_file.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                logger.debug("webui env file cleanup error", exc_info=True)
            self._webui_env_file = None

    def _check_port_free(self, host: str, port: int, *, label: str) -> None:
        if port_in_use(host, port):
            raise RuntimeError(
                f"{label} port {port} already in use — an old instance is likely "
                "still running. Stop it first, then rerun."
            )

    async def _wait_webui_port_released(self, timeout: float = 3.0) -> None:
        """webui 进程组终止后复核端口已释放；超时未释放仅告警（不阻塞退出）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not port_in_use(self._config.host, self._config.webui_port):
                return
            await asyncio.sleep(0.2)
        if os.name == "nt":
            hint = "netstat -ano | findstr :%d / 任务管理器"
        else:
            hint = "ss -ltnp | grep %d 或 pkill -f vite"
        logger.warning(
            "webui port %d still in use after process group termination — "
            "an external process may be listening (check with `%s`)",
            self._config.webui_port,
            hint % self._config.webui_port,
        )

    def _wait_webui_ready(self, proc: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
        """轮询 vite 就绪；进程早退立即报错而非静默失败。

        ``--strictPort`` 绑定失败时 vite 立即退出且输出被 DEVNULL 吞掉——
        本方法以退出码暴露失败；超时未就绪仅告警（dev server 可能仍在启动）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"webui dev server exited early (code={proc.returncode}); "
                    "common cause: port already in use (vite --strictPort) or "
                    "pnpm install missing in observer-web package."
                )
            if port_in_use(self._config.host, self._config.webui_port):
                logger.info("WebUI dev server ready on port %d", self._config.webui_port)
                return
            time.sleep(0.5)
        logger.warning(
            "WebUI dev server not ready within %.0fs (may still be starting)",
            timeout,
        )

    def _start_webui(self) -> None:
        self._ensure_pnpm()
        self._check_port_free(self._config.host, self._config.webui_port, label="webui")
        webui_dir = self._webui_dir
        assert webui_dir is not None  # start() 已保证
        if not webui_dir.is_dir():
            raise RuntimeError(
                f"observer-web package not found at {webui_dir}; "
                "ensure ghrah-observer-webui is checked out."
            )
        self._write_webui_env_file(webui_dir)
        env = os.environ.copy()
        env["PORT"] = str(self._config.webui_port)
        # pnpm_command()：Windows 的 pnpm 是 .cmd 批处理，无法被
        # create_process 直接 exec（与 materialize/build 同一权威形态）
        command: list[str] = pnpm_command() + [
            "dev",
            "--port",
            str(self._config.webui_port),
            "--strictPort",
        ]
        if os.name == "nt":
            popen_kwargs: dict = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        else:
            popen_kwargs = {"start_new_session": True}
        self._webui_process = subprocess.Popen(
            command,
            cwd=str(webui_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
        logger.info(
            "WebUI dev server starting (pid=%d, port=%d)",
            self._webui_process.pid,
            self._config.webui_port,
        )
        self._wait_webui_ready(self._webui_process)

    def _open_browser(self) -> None:
        url = self._config.webui_url()
        try:
            import webbrowser

            webbrowser.open(url)
            logger.info("Browser opened at %s", url)
        except Exception:
            logger.debug("Browser open failed (non-fatal)", exc_info=True)
