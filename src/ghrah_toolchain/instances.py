# SPDX-License-Identifier: Apache-2.0
"""具名实例的异步启动、状态、日志与受控停止。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from ghrah_toolchain.boot import (
    _busy_ports,
    boot_pid_path,
    resolve_boot_target,
    run_boot,
    stop_boot_session,
)
from ghrah_toolchain.errors import RuntimeRefError, ToolchainError
from ghrah_toolchain.layout import list_distros, resolve_distro_root
from ghrah_toolchain.runtime_ref import _pid_alive, acquire_distro_lock

_RECORD = ".session.json"
_ACTIVE = {"starting", "running", "stopping"}


@contextlib.contextmanager
def _locked(root: Path):
    """短暂竞争时等待同一实例锁，不把后台进程启动竞窗当成失败。"""
    deadline = time.monotonic() + 5
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(acquire_distro_lock(root))
                break
            except RuntimeRefError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        yield


def _read(root: Path) -> dict[str, Any] | None:
    path = root / _RECORD
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            data["schema"] != 1
            or not isinstance(data["token"], str)
            or re.fullmatch(r"[0-9a-f]{32}", data["token"]) is None
            or type(data["pid"]) is not int
            or data["pid"] <= 0
            or type(data["created"]) not in (int, float)
            or data["created"] <= 0
            or not math.isfinite(data["created"])
            or not isinstance(data["generation"], str)
            or not data["generation"]
            or data["state"] not in {*_ACTIVE, "stopped", "failed"}
        ):
            raise ValueError("字段不合法")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolchainError(f"实例记录损坏: {path}: {exc}") from exc


def _write(root: Path, record: dict[str, Any]) -> None:
    temp = root / f".{_RECORD}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, root / _RECORD)
    finally:
        temp.unlink(missing_ok=True)


def _argv(process: psutil.Process) -> list[str]:
    """读取进程 argv 供身份复核；不可读时返回空列表（按“无法确认”处理）。"""
    try:
        return process.cmdline()
    except psutil.Error:
        return []


def _process(record: dict[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(record["pid"])
        if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return None
        if process.create_time() == record["created"]:
            return process
        # psutil 的 create_time 以系统时钟（boot_time + 启动 tick）为基准，
        # NTP／手动校时会让它整体漂移，即使进程从未重启。此时以监督者
        # argv 中的会话 token 复核身份：token 每次启动随机生成且写入记录，
        # 被复用的无关 pid 不可能同时命中模块名与 token。
        argv = _argv(process)
        if "ghrah_toolchain.supervisor" in argv and record["token"] in argv:
            return process
        return None
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise ToolchainError("无法核对实例进程身份，拒绝继续操作") from exc


def instance_status(name: str) -> dict[str, Any]:
    """读取具名实例状态，不更改源配置或发起运行。"""
    root = resolve_distro_root(name)
    if not root.is_dir():
        raise ToolchainError(f"实例未安装: {name}")
    record = _read(root)
    if record is None:
        if boot_pid_path(root).exists():
            return {"name": name, "state": "legacy", "log": None}
        return {"name": name, "state": "stopped", "log": None}
    state = record["state"]
    if state in _ACTIVE and _process(record) is None:
        state = "failed" if state != "stopping" else "stopped"
    return {
        "name": name,
        "state": state,
        "pid": record["pid"],
        "generation": record["generation"],
        "log": str(root / "logs" / f"{record['token']}.log"),
        "error": record.get("error"),
    }


def list_instances() -> list[dict[str, Any]]:
    """枚举用户安装域的具名实例。"""
    return [instance_status(name) for name in list_distros()]


def assert_instance_stopped(root: Path) -> None:
    """调用者持有实例锁；覆盖尚未监听端口的启动／停止阶段。"""
    record = _read(root)
    if record and _process(record) is not None:
        raise ToolchainError(
            f"[{root.name}] 实例的后台管理进程仍在运行，请先 ghrah distro stop {root.name}"
        )
    pidfile = boot_pid_path(root)
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError) as exc:
            raise ToolchainError("旧版进程记录文件损坏，无法确认实例已停止") from exc
        if _pid_alive(pid) is not False:
            raise ToolchainError(f"[{root.name}] 旧会话仍在运行，请先停止")


def _transition(root: Path, token: str, state: str, error: str | None = None) -> None:
    with _locked(root):
        record = _read(root)
        if record is None or record["token"] != token:
            raise ToolchainError("实例会话已被替换，拒绝覆盖新状态")
        if state == "running" and record["state"] == "stopping":
            return
        record.update(state=state, error=error)
        _write(root, record)


def start_instance(
    name: str, *, no_webui: bool = False, foreground: bool = False
) -> dict[str, Any] | int:
    """启动已安装具名实例；默认立即返回，监督者及输出脱离终端。"""
    root = resolve_distro_root(name)
    token = uuid.uuid4().hex
    with _locked(root):
        target = resolve_boot_target(name, None)
        previous = _read(root)
        if previous and previous["state"] in _ACTIVE and _process(previous) is not None:
            return instance_status(name)
        if boot_pid_path(root).is_file():
            try:
                old_pid = int(boot_pid_path(root).read_text().strip())
            except (ValueError, OSError) as exc:
                raise ToolchainError(
                    "旧版进程记录文件损坏，请先运行 ghrah self doctor 检查"
                ) from exc
            if _pid_alive(old_pid) is not False:
                raise ToolchainError("实例仍通过旧版方式运行，请先停止后再启动")
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        log_path = logs / f"{token}.log"
        if foreground:
            pid = os.getpid()
        else:
            command = [sys.executable, "-m", "ghrah_toolchain.supervisor", name, token]
            if no_webui:
                command.append("--no-webui")
            options: dict[str, Any] = {}
            if os.name == "nt":
                # 用不可见的独立控制台替代 DETACHED_PROCESS：后者在 Win10+ 会为
                # 脱离的 console 进程拉起 ConPTY（conhost 的 PseudoConsoleWindow），
                # 桌面上表现为一个空的命令框。CREATE_NO_WINDOW 同样让监督者脱离
                # 当前终端，但不显示窗口。
                options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                options["start_new_session"] = True
            with log_path.open("ab") as output:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    **options,
                )
            pid = process.pid
        try:
            created = psutil.Process(pid).create_time()
        except psutil.Error as exc:
            if not foreground:
                process.terminate()
                process.wait(timeout=10)
            raise ToolchainError(f"启动后台管理进程失败，请查看日志: {log_path}") from exc
        _write(
            root,
            {
                "schema": 1,
                "token": token,
                "pid": pid,
                "created": created,
                "state": "starting",
                "generation": target.version,
            },
        )
    if foreground:
        return serve_instance(name, token, no_webui=no_webui)
    return instance_status(name)


def _claim_record(root: Path, token: str, *, pid: int | None = None) -> dict[str, Any]:
    """把实例记录归属到真正运行监督逻辑的进程（调用者须持实例锁）。

    POSIX 上 ``Popen`` 的 pid 就是监督进程本身，记录无需改写；Windows 上
    ``venv\\Scripts\\python.exe`` 是 ``venvlauncher``、console-script 入口是
    trampoline，``Popen`` 拿到的是启动器 pid，真正执行本模块的是它的子进程。
    父进程无法预知那个 pid，因此改由持 token 的监督进程自注册：此后
    status／stop／日志归属跟踪的才是拥有实例生命周期的进程。

    ``pid`` 仅供调用方指明归属进程（测试注入）；缺省为当前进程。
    """
    record = _read(root)
    if record is None or record["token"] != token:
        raise ToolchainError("后台管理进程与实例记录不匹配")
    owner = psutil.Process(pid)
    if record["pid"] != owner.pid:
        record["pid"] = owner.pid
        record["created"] = owner.create_time()
        _write(root, record)
    return record


def serve_instance(name: str, token: str, *, no_webui: bool = False) -> int:
    """后台监督入口：将就绪、退出或失败写回该会话，并收束全部子进程。"""
    root = resolve_distro_root(name)
    with _locked(root):
        record = _claim_record(root, token)
    logging.getLogger().setLevel(logging.INFO)

    def ready() -> None:
        _transition(root, token, "running")
        logging.info("[%s] 启动检查已通过，服务在后台运行；可用 ghrah logs 查看日志", name)

    try:
        target = resolve_boot_target(name, record["generation"])

        async def supervise() -> int:
            stop = asyncio.Event()

            async def requested() -> None:
                while True:
                    current = _read(root)
                    if (
                        current is None
                        or current["token"] != token
                        or current["state"] == "stopping"
                    ):
                        stop.set()
                        return
                    await asyncio.sleep(0.1)

            watcher = asyncio.create_task(requested())
            try:
                return await run_boot(
                    target,
                    no_webui=no_webui,
                    no_browser=True,
                    on_ready=ready,
                    stop_event=stop,
                )
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        code = asyncio.run(supervise())
    except Exception as exc:
        logging.exception("[%s] 启动或运行失败", name)
        _transition(root, token, "failed", str(exc))
        return 1
    _transition(
        root, token, "stopped" if code == 0 else "failed", None if code == 0 else "子进程异常退出"
    )
    return code


def stop_instance(name: str, *, timeout: float = 15.0) -> int:
    """幂等停止具名实例，核对身份后向监督者发信号，等待整组收束。

    返回 0 前复核端口已释放；无法核对监督者但端口仍被占用时拒绝报告成功。
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ToolchainError("等待停止的秒数必须大于 0，且不能为无穷大")
    root = resolve_distro_root(name)
    if not root.is_dir():
        raise ToolchainError(f"实例未安装: {name}")
    with _locked(root):
        record = _read(root)
        if record is None:
            if not boot_pid_path(root).is_file():
                return 0
            # 旧 pidfile 使用原有专用停止路径，不凭一个 PID 接管为新会话。
            return stop_boot_session(name, timeout=timeout)
        process = _process(record)
        if process is None:
            # “看不见进程”不等于“已停止”：不同 PID 命名空间或监督者
            # 异常退出留下的孤儿子进程仍可能占着端口。此时宁可报错，
            # 也不要把仍在服务的实例报告成已停止。
            busy = _busy_ports(root, None, None)
            if busy:
                raise ToolchainError(
                    f"[{name}] 无法确认后台管理进程的身份（进程编号: {record['pid']}），"
                    f"但端口仍在监听: {busy}；"
                    "可能是当前环境无法查看该进程，或后台管理进程退出后仍有相关进程运行。"
                    "请人工核查后处理"
                )
            return 0
        record["state"] = "stopping"
        _write(root, record)
        try:
            if os.name != "nt":
                process.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            return 0
    deadline = time.monotonic() + timeout
    while _process(record) is not None:
        if time.monotonic() >= deadline:
            raise ToolchainError(f"[{name}] 停止超时；未强杀，查看实例日志")
        time.sleep(0.1)
    # 只在原监督者已经完成退出后报告停止；不触碰新会话状态。
    with _locked(root):
        latest = _read(root)
        if latest and latest["token"] == record["token"]:
            latest["state"] = "stopped"
            _write(root, latest)
    busy = _busy_ports(root, None, None)
    if busy:
        raise ToolchainError(
            f"[{name}] 后台管理进程已退出，但端口仍被占用: {busy}（可能有残留子进程）"
        )
    return 0


def read_logs(name: str, *, tail: int = 100) -> str:
    """读取最近会话日志末尾；不将任意路径解释为日志目标。"""
    from collections import deque

    status = instance_status(name)
    if not status.get("log"):
        return ""
    path = Path(status["log"])
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return "".join(deque(stream, maxlen=tail))
    except FileNotFoundError:
        return ""


def follow_logs(name: str, *, tail: int = 100):
    """从同一个文件句柄读末尾后跟随，轮转／新会话时切换；不控制实例生命周期。"""
    from collections import deque

    stream = None
    identity = None
    first = True
    try:
        while True:
            status = instance_status(name)
            path = Path(status["log"]) if status.get("log") else None
            try:
                stat = path.stat() if path else None
            except FileNotFoundError:
                stat = None
            current = (path, stat.st_dev, stat.st_ino) if stat else None
            if current is not None and (current != identity or stream is None):
                if stream:
                    remaining = stream.read()
                    if remaining:
                        yield remaining
                    stream.close()
                stream = path.open(encoding="utf-8", errors="replace")
                identity = current
                if first:
                    recent = "".join(deque(stream, maxlen=tail))
                    if recent:
                        yield recent
                    first = False
            if stream:
                if current == identity and stat.st_size < stream.tell():
                    stream.seek(0)
                chunk = stream.read()
                if chunk:
                    yield chunk
            time.sleep(0.1)
    finally:
        if stream:
            stream.close()
