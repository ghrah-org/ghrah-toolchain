# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""进程生命周期管理：spawn OS 子进程 + 前端（dev 子进程 / 静态线程）+ 逆序收束。

- ``_port_in_use``（loopback 双栈）、``_terminate_process``（POSIX 进程组
  SIGTERM→SIGKILL / Windows taskkill /T）、``_start_webui``（``pnpm dev
  --strictPort``、``.env.local`` 注入 ``VITE_GHRAH_SUBJECT_WS_URL``）、
  ``_wait_webui_ready``、``_wait_webui_port_released``、``_open_browser``。
- ``_start_webui_static``（静态 serve）：``ThreadingHTTPServer`` + daemon 线程，
  目录钉 dist 根；``index.html`` 响应注入 ``window.__GHRAH_SUBJECT_WS_URL__``
  + ``Cache-Control: no-store``；``stop()`` 为 ``shutdown()`` + 端口释放复核。
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
import threading
import time
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ghrah_toolchain.health import wait_for_server
from ghrah_toolchain.materialize import pnpm_command

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT_PORT = 4112
DEFAULT_WEBUI_PORT = 5173
DEFAULT_HOST = "127.0.0.1"
_WEBUI_ENV_FILE = ".env.local"
_WEBUI_ENV_KEY = "VITE_GHRAH_SUBJECT_WS_URL"
_WS_URL_GLOBAL = "__GHRAH_SUBJECT_WS_URL__"


def _ws_url_inject_script(ws_url: str) -> str:
    """静态 serve 的 WS URL 注入 script 标签（先于首个 module script 执行）。"""
    escaped = ws_url.replace('"', '\\"')
    return f'<script>window.{_WS_URL_GLOBAL}="{escaped}";</script>'


def _inject_ws_url(html: str, ws_url: str) -> str:
    """把注入 script 放进 HTML：``<head>`` 开标签后优先（保证先于 head 内全部
    module script），其次首个 ``<script`` 前，兜底前置。"""
    script = _ws_url_inject_script(ws_url)
    lowered = html.lower()
    head_at = lowered.find("<head")
    if head_at >= 0:
        close = html.find(">", head_at)
        if close >= 0:
            return html[: close + 1] + script + html[close + 1 :]
    script_at = lowered.find("<script")
    if script_at >= 0:
        return html[:script_at] + script + html[script_at:]
    return script + html


def _make_static_handler(dist_root: Path, ws_url: str) -> type[SimpleHTTPRequestHandler]:
    """静态 webui handler 工厂：目录钉 dist 根；index.html 注入 WS URL + no-store。"""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            kwargs["directory"] = str(dist_root)
            super().__init__(*args, **kwargs)

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            path = self.path.partition("?")[0]
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            super().do_GET()

        def _serve_index(self) -> None:
            try:
                html = (dist_root / "index.html").read_text(encoding="utf-8")
            except OSError:
                self.send_error(404, "index.html missing in dist root")
                return
            body = _inject_ws_url(html, ws_url).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - 父类签名
            del format, args

    return Handler


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
    except ProcessLookupError, PermissionError:
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
    except ProcessLookupError, PermissionError:
        logger.debug("process group already gone", exc_info=True)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("相关进程未响应停止请求，正在强制结束")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            logger.debug("process group SIGKILL failed", exc_info=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("强制结束后进程仍未退出（进程编号: %d）", proc.pid)
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
    """OS 子进程 + webui（dev 子进程 / 静态线程）的生命周期管理。

    - OS（Subject + 内嵌 Core）：经 ``spawn_os_process`` 以代内 env 的
      ``ghrah-subject-server`` 独立进程拉起（引导器不 import ghrah）。
    - webui 静态 serve（默认）：``ThreadingHTTPServer`` + daemon 线程（stdlib，
      目录钉 dist 根；index.html 注入 WS URL）。
    - webui dev server（``--webui-dev``）：``subprocess.Popen``（pnpm dev
      --strictPort），仅显式开发域路径触达。

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
        webui_static_root: Path | None = None,
        webui_ws_url: str | None = None,
    ) -> None:
        if webui_dir is not None and (webui_static_root is not None or webui_ws_url is not None):
            raise RuntimeError(
                "前端源码目录 webui_dir 不能与已构建页面配置 "
                "webui_static_root/webui_ws_url 同时使用"
            )
        self._config = config
        self._os_command = os_command
        self._os_cwd = os_cwd
        self._os_env = os_env
        self._webui_dir = webui_dir
        self._webui_static_root = webui_static_root
        self._webui_ws_url = webui_ws_url
        self._os_process: subprocess.Popen[bytes] | None = None
        self._webui_process: subprocess.Popen[bytes] | None = None
        self._webui_env_file: Path | None = None
        self._webui_static_server: ThreadingHTTPServer | None = None
        self._webui_static_thread: threading.Thread | None = None
        self._started = False

    @property
    def config(self) -> StackConfig:
        return self._config

    @property
    def os_process(self) -> subprocess.Popen[bytes] | None:
        return self._os_process

    @property
    def webui_process(self) -> subprocess.Popen[bytes] | None:
        """返回显式开发前端子进程，静态服务没有独立进程。"""
        return self._webui_process

    def subject_ws_url(self) -> str:
        return self._config.subject_ws_url()

    def webui_url(self) -> str:
        return self._config.webui_url()

    async def start(self) -> None:
        """起 OS → 前端（dev 子进程 / 静态线程）。任一层失败抛异常（已起资源由 stop 释放）。"""
        await self._start_os()
        if self._webui_dir is not None:
            self._start_webui()
        elif self._webui_static_root is not None:
            self._start_webui_static()
        self._started = True
        if self._config.open_browser:
            self._open_browser()

    async def stop(self) -> None:
        """逆序优雅退出（webui → OS）。幂等。"""
        if self._webui_process is not None:
            terminate_process(self._webui_process)
            self._webui_process = None
            await self._wait_webui_port_released()
        if self._webui_static_server is not None:
            self._stop_webui_static()
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
            raise RuntimeError("未配置后端启动命令 os_command")
        self._check_port_free(
            self._config.host,
            self._config.subject_port,
            label="后端服务",
        )
        env = os.environ.copy()
        if self._os_env:
            env.update(self._os_env)
        self._os_process = subprocess.Popen(
            self._os_command,
            cwd=str(self._os_cwd) if self._os_cwd is not None else None,
            env=env,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
        logger.info(
            "正在启动后端（进程编号: %d；端口: %d）",
            self._os_process.pid,
            self._config.subject_port,
        )
        await wait_for_server(self._config.subject_http_url(), timeout=10.0, name="后端服务")

    # ── webui ──

    def _ensure_pnpm(self) -> None:
        if shutil.which("pnpm") is None:
            raise RuntimeError(
                "找不到 pnpm 命令；请先安装 pnpm，并确认命令所在目录已加入 PATH，再启动前端。"
            )
        if shutil.which("node") is None:
            raise RuntimeError(
                "找不到 node 命令；请先安装 Node.js，并确认命令所在目录已加入 PATH，再启动前端。"
            )

    def _write_webui_env_file(self, webui_dir: Path) -> None:
        env_path = webui_dir / _WEBUI_ENV_FILE
        env_path.write_text(
            f"{_WEBUI_ENV_KEY}={self._config.subject_ws_url()}\n",
            encoding="utf-8",
        )
        self._webui_env_file = env_path
        logger.info(
            "已设置前端连接后端的地址: %s=%s", _WEBUI_ENV_KEY, self._config.subject_ws_url()
        )

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
                f"{label}端口 {port} 已被占用，可能是之前的实例仍在运行。"
                "请先停止占用端口的服务后重试。"
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
            "前端相关进程停止后，端口 %d 仍被占用；可能有其他进程在使用，可运行 `%s` 检查",
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
                    f"前端开发服务器在启动完成前退出（退出码: {proc.returncode}）；"
                    "请检查端口是否被占用（vite --strictPort），"
                    "以及是否已在 observer-web 目录执行 pnpm install 安装依赖。"
                )
            if port_in_use(self._config.host, self._config.webui_port):
                logger.info("前端开发服务器已就绪，端口: %d", self._config.webui_port)
                return
            time.sleep(0.5)
        logger.warning(
            "等待 %.0f 秒后前端开发服务器仍未就绪，可能还在启动，请查看日志",
            timeout,
        )

    def _start_webui(self) -> None:
        self._ensure_pnpm()
        self._check_port_free(self._config.host, self._config.webui_port, label="前端服务")
        webui_dir = self._webui_dir
        assert webui_dir is not None  # start() 已保证
        if not webui_dir.is_dir():
            raise RuntimeError(
                f"找不到前端源码目录 observer-web: {webui_dir}；"
                "请确认已下载完整的 ghrah-observer-webui 源码。"
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
            stdout=None,
            stderr=None,
            **popen_kwargs,
        )
        logger.info(
            "正在启动前端开发服务器（进程编号: %d；端口: %d）",
            self._webui_process.pid,
            self._config.webui_port,
        )
        self._wait_webui_ready(self._webui_process)

    # ── webui 静态 serve ──

    def _start_webui_static(self) -> None:
        """静态 serve 基座产物 dist：stdlib http server + daemon 线程。"""
        root = self._webui_static_root
        if root is None or not root.is_dir():
            raise RuntimeError(f"找不到已构建的前端页面目录: {root}；请确认构建文件完整")
        self._check_port_free(self._config.host, self._config.webui_port, label="前端服务")
        handler = _make_static_handler(root, self._webui_ws_url or self._config.subject_ws_url())
        server = ThreadingHTTPServer((self._config.host, self._config.webui_port), handler)
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"ghrah-webui-static-{self._config.webui_port}",
            daemon=True,
        )
        self._webui_static_server = server
        self._webui_static_thread = thread
        thread.start()
        self._wait_static_ready()
        logger.info(
            "前端页面服务已就绪（端口: %d；页面目录: %s）",
            self._config.webui_port,
            root,
        )

    def _wait_static_ready(self, timeout: float = 5.0) -> None:
        """静态面就绪探活：端口连通即就绪（绑定在构造期同步完成，超时即异常态）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_in_use(self._config.host, self._config.webui_port):
                return
            time.sleep(0.1)
        raise RuntimeError(
            f"等待 {timeout} 秒后，前端页面服务仍未监听端口 {self._config.webui_port}"
        )

    def _stop_webui_static(self) -> None:
        """停止静态面：shutdown + join + close；幂等（先摘引用再收束）。"""
        server = self._webui_static_server
        thread = self._webui_static_thread
        self._webui_static_server = None
        self._webui_static_thread = None
        if server is None:
            return
        server.shutdown()
        if thread is not None:
            thread.join(timeout=5.0)
        server.server_close()

    def _open_browser(self) -> None:
        url = self._config.webui_url()
        try:
            import webbrowser

            webbrowser.open(url)
            logger.info("已请求浏览器打开 %s", url)
        except Exception:
            logger.debug("Browser open failed (non-fatal)", exc_info=True)
