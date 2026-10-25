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

from ghrah_toolchain.boot import boot_pid_path, resolve_boot_target, run_boot, stop_boot_session
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


def _process(record: dict[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(record["pid"])
        if process.create_time() != record["created"]:
            return None
        if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return None
        return process
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
        raise ToolchainError(f"[{root.name}] 监督进程仍在运行，请先 ghrah distro stop {root.name}")
    pidfile = boot_pid_path(root)
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError) as exc:
            raise ToolchainError("旧 pidfile 损坏，无法确认实例已停止") from exc
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
                raise ToolchainError("旧 pidfile 损坏，先运行诊断") from exc
            if _pid_alive(old_pid) is not False:
                raise ToolchainError("已有旧会话，先显式停止后再启动")
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
                options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
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
            raise ToolchainError(f"启动监督进程失败，查看 {log_path}") from exc
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


def serve_instance(name: str, token: str, *, no_webui: bool = False) -> int:
    """后台监督入口：将就绪、退出或失败写回该会话，并收束全部子进程。"""
    root = resolve_distro_root(name)
    with _locked(root):
        record = _read(root)
        if record is None or record["token"] != token or record["pid"] != os.getpid():
            raise ToolchainError("监督进程身份与实例记录不匹配")
    logging.getLogger().setLevel(logging.INFO)

    def ready() -> None:
        _transition(root, token, "running")
        logging.info("[%s] 已就绪，日志与生命周期由后台监督进程管理", name)

    try:
        target = resolve_boot_target(name, record["generation"])
        code = asyncio.run(run_boot(target, no_webui=no_webui, no_browser=True, on_ready=ready))
    except Exception as exc:
        logging.exception("[%s] 启动或运行失败", name)
        _transition(root, token, "failed", str(exc))
        return 1
    _transition(
        root, token, "stopped" if code == 0 else "failed", None if code == 0 else "子进程异常退出"
    )
    return code


def stop_instance(name: str, *, timeout: float = 15.0) -> int:
    """幂等停止具名实例，核对创建时间后向监督者发信号，等待整组收束。"""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ToolchainError("停止超时必须是有限正数")
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
        if process is None or record["state"] not in _ACTIVE:
            return 0
        record["state"] = "stopping"
        _write(root, record)
        try:
            if os.name == "nt":
                from ghrah_toolchain.boot import _taskkill

                _taskkill(process.pid, force=True)
            else:
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
