# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot pidfile 与 stop 命令测试：双启守卫、优雅停止、陈旧回收、误杀护栏。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ghrah_bootloader.boot import (
    _clear_boot_pid,
    _write_boot_pid,
    boot_pid_path,
    stop_boot_session,
)
from ghrah_bootloader.errors import BootloaderError

_SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    h.mkdir(parents=True)
    return h


def _distro_root(home: Path) -> Path:
    d = home / "generic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_pidfile_write_and_clear(home: Path) -> None:
    root = _distro_root(home)
    _write_boot_pid(root)
    pidfile = boot_pid_path(root)
    assert pidfile.is_file()
    assert int(pidfile.read_text(encoding="utf-8").strip()) == os.getpid()
    _clear_boot_pid(root)
    assert not pidfile.exists()


def test_pidfile_rejects_double_boot(home: Path) -> None:
    root = _distro_root(home)
    _write_boot_pid(root)  # 本进程即"活 pid"
    with pytest.raises(BootloaderError, match="已有 boot 会话"):
        _write_boot_pid(root)
    _clear_boot_pid(root)


def test_pidfile_recycles_stale(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")  # 几乎不可能存活的 pid
    _write_boot_pid(root)
    assert int(pidfile.read_text(encoding="utf-8").strip()) == os.getpid()
    _clear_boot_pid(root)


def test_clear_pidfile_ignores_foreign_pid(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")
    _clear_boot_pid(root)  # 非 本进程 pid：不动
    assert pidfile.read_text(encoding="utf-8").strip() == "999999999"


# ── stop_boot_session ────────────────────────────────────────

_FAKE_BOOT_CODE = """
import signal, sys, time
stopped = {"v": False}
def on_term(sig, frame):
    stopped["v"] = True
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while not stopped["v"]:
    time.sleep(0.05)
"""


def _spawn_fake_boot(home: Path, name: str = "ghrah-bootloader") -> subprocess.Popen:
    """spawn 一个 argv[0] 伪装为 ghrah-bootloader 的子进程（装 SIGTERM handler）。

    经 home 下的符号链接 <name> → sys.executable 实现（execv 需要绝对路径，
    且 argv[0] 由调用方指定）。
    """
    if os.name == "posix":
        exe = home / name
        exe.symlink_to(sys.executable)
        argv = [str(exe), "-c", _FAKE_BOOT_CODE]
    else:
        argv = [sys.executable, "-c", _FAKE_BOOT_CODE]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "GHRAH_HOME": str(home)},
        cwd=home,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # 等 ready
    return proc


def test_stop_terminates_fake_boot(home: Path) -> None:
    root = _distro_root(home)
    proc = _spawn_fake_boot(home)
    boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
    rc = stop_boot_session("generic", timeout=10)
    proc.wait(timeout=10)
    assert rc == 0
    assert proc.returncode == 0


def test_stop_dead_pid_cleans_pidfile(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")
    rc = stop_boot_session("generic")
    assert rc == 1
    assert not pidfile.exists()


def test_stop_without_pidfile(home: Path) -> None:
    _distro_root(home)
    rc = stop_boot_session("generic")
    assert rc == 1


@pytest.mark.skipif(os.name != "posix", reason="cmdline 校验仅 POSIX")
def test_stop_refuses_foreign_process(home: Path) -> None:
    root = _distro_root(home)
    proc = _spawn_fake_boot(home, name="definitely-not-bootloader")
    try:
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=5)
        assert rc == 1
        assert proc.poll() is None  # 未被杀
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="无 /proc 的 posix 模拟需 posix 平台")
def test_stop_without_proc_skips_guard(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """macOS 回归位：/proc 不存在时护栏跳过、stop 不崩溃、继续 SIGTERM。"""
    from ghrah_bootloader import boot as boot_mod

    monkeypatch.setattr(boot_mod.Path, "is_dir", lambda self: False)
    proc = _spawn_fake_boot(home)
    try:
        root = _distro_root(home)
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=10)
        proc.wait(timeout=10)
        assert rc == 0
        assert proc.returncode == 0
        out = capsys.readouterr().out
        assert "跳过 cmdline 护栏" in out
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_stop_timeout_reports_stuck(home: Path) -> None:
    root = _distro_root(home)
    # spawn 一个忽略 SIGTERM 的伪装进程 → 超时路径
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    if os.name == "posix":
        exe = home / "ghrah-bootloader"
        exe.symlink_to(sys.executable)
        argv = [str(exe), "-c", code]
    else:
        argv = [sys.executable, "-c", code]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "GHRAH_HOME": str(home)},
        cwd=home,
    )
    assert proc.stdout is not None
    proc.stdout.readline()
    try:
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=1.0)
        assert rc == 2
        assert proc.poll() is None  # 不强杀
    finally:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.1)


# ── Windows stop 升级链（nt 模拟，mock winproc/_taskkill） ──


class _OsProxy:
    """os 模块代理：name=nt，其余属性透传真实 os（Path 不受毒化）。"""

    def __init__(self) -> None:
        import os as real_os

        object.__setattr__(self, "_real", real_os)

    def __getattr__(self, item):
        return getattr(object.__getattribute__(self, "_real"), item)

    @property
    def name(self) -> str:
        return "nt"


class _StopNtSim:
    """nt 停止链路模拟：os 代理（name=nt）+ mock winproc/_taskkill/_session_exited。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, image: str | None) -> None:
        from ghrah_bootloader import boot as boot_mod
        from ghrah_bootloader import winproc as winproc_mod

        self.boot_mod = boot_mod
        self.taskkill_calls: list[list[str]] = []
        self.exited_answers: list[bool] = []
        self.force_survives = False
        self.graceful_exit = False
        monkeypatch.setattr(boot_mod, "os", _OsProxy(), raising=False)
        monkeypatch.setattr(winproc_mod, "process_image", lambda pid: image)
        monkeypatch.setattr(
            boot_mod,
            "_taskkill",
            lambda pid, force: self.taskkill_calls.append(
                ["taskkill", "/F", "/T", "/PID", str(pid)]
                if force
                else ["taskkill", "/PID", str(pid)]
            ),
        )
        sim = self

        def exited_or_escalated(pid: int) -> bool:
            # 优雅 taskkill 已发且开了 graceful_exit → 恒「已退出」
            # （模拟优雅段内退出，摆脱对轮询拍数的精确预知）；
            # 强杀（第二次 taskkill）已发 → 默认「已退出」（force_survives
            # 例外：模拟强杀也不死的顽固进程）；否则消费答案序列
            # （空后恒 False=活，绝不触真实探测）
            if sim.taskkill_calls and sim.graceful_exit:
                return True
            if len(sim.taskkill_calls) >= 2:
                return False if sim.force_survives else True
            return sim.exited_answers.pop(0) if sim.exited_answers else False

        monkeypatch.setattr(boot_mod, "_session_exited", exited_or_escalated)


def test_stop_nt_escalation_chain(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """优雅 taskkill（无 /F）→ 轮询存活 → 超时强杀 /F /T → 退出 → 端口检查。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=r"C:\venv\Scripts\python.exe")
    # 预检 + 优雅段轮询恒「活」（exited_or_escalated：强杀后恒「死」）
    monkeypatch.setattr("ghrah_bootloader.boot._busy_ports", lambda *a, **kw: [])
    rc = stop_boot_session("generic", timeout=0.3)
    assert rc == 0
    assert sim.taskkill_calls[0] == ["taskkill", "/PID", str(pid)]  # 优雅在前
    assert sim.taskkill_calls[1] == ["taskkill", "/F", "/T", "/PID", str(pid)]  # 强杀升级
    assert len(sim.taskkill_calls) == 2


def test_stop_nt_graceful_exit_skips_force(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """优雅段内即退出 → 不触发强杀。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=r"C:\venv\Scripts\ghrah-bootloader.exe")
    sim.graceful_exit = True  # 优雅 taskkill 后即退出
    monkeypatch.setattr("ghrah_bootloader.boot._busy_ports", lambda *a, **kw: [])
    rc = stop_boot_session("generic", timeout=5.0)
    assert rc == 0
    assert sim.taskkill_calls == [["taskkill", "/PID", str(pid)]]


def test_stop_nt_force_survivor_reports_stuck(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """强杀后仍活 → rc=2 + nt 处置文案（tasklist）。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=r"C:\venv\Scripts\python.exe")
    sim.force_survives = True  # 强杀也不死
    rc = stop_boot_session("generic", timeout=0.2)
    assert rc == 2
    assert len(sim.taskkill_calls) == 2  # 优雅 + 强杀都试过
    assert "tasklist" in capsys.readouterr().out


def test_stop_nt_guard_rejects_foreign_image(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """护栏拒绝：映像名无关（notepad）→ rc=1、零 taskkill。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=r"C:\Windows\system32\notepad.exe")
    rc = stop_boot_session("generic", timeout=1.0)
    assert rc == 1
    assert sim.taskkill_calls == []


def test_stop_nt_guard_allows_python_and_bootloader(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """护栏放行：python.exe 与 ghrah-bootloader.exe 两种映像名。"""
    for image in (r"C:\py\python.exe", r"C:\venv\Scripts\ghrah-bootloader.exe"):
        root = _distro_root(home)
        pid = 4242
        boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
        sim = _StopNtSim(monkeypatch, image=image)
        sim.graceful_exit = True
        monkeypatch.setattr("ghrah_bootloader.boot._busy_ports", lambda *a, **kw: [])
        rc = stop_boot_session("generic", timeout=1.0)
        assert rc == 0, image
        assert sim.taskkill_calls, image


def test_stop_nt_guard_unavailable_degrades(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """护栏不可用（process_image → None）→ 告警跳过 + 继续升级链。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=None)
    sim.graceful_exit = True  # 护栏降级后优雅退出
    monkeypatch.setattr("ghrah_bootloader.boot._busy_ports", lambda *a, **kw: [])
    rc = stop_boot_session("generic", timeout=1.0)
    assert rc == 0
    out = capsys.readouterr().out
    assert "跳过映像名护栏" in out
    assert sim.taskkill_calls == [["taskkill", "/PID", str(pid)]]


def test_stop_nt_busy_ports_hint_windows(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """会话退出但端口占用 → nt 文案含 netstat 指引。"""
    root = _distro_root(home)
    pid = 4242
    boot_pid_path(root).write_text(f"{pid}\n", encoding="utf-8")
    sim = _StopNtSim(monkeypatch, image=r"C:\venv\Scripts\python.exe")
    sim.graceful_exit = True
    monkeypatch.setattr("ghrah_bootloader.boot._busy_ports", lambda *a, **kw: [4112])
    rc = stop_boot_session("generic", timeout=1.0)
    assert rc == 2
    assert "netstat" in capsys.readouterr().out


def test_write_boot_pid_fails_closed_on_unknown(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测不可用（None）→ fail-closed 拒绝 + 人工处置文案。"""
    from ghrah_bootloader import boot as boot_mod

    root = _distro_root(home)
    boot_pid_path(root).write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(boot_mod, "_pid_alive", lambda pid: None)
    with pytest.raises(BootloaderError, match="人工确认"):
        _write_boot_pid(root)


def test_write_boot_pid_recycles_on_dead_nt(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 下 pid 死 → 陈旧 pidfile 正常回收（恒真桩缺陷消解）。"""
    from ghrah_bootloader import boot as boot_mod

    root = _distro_root(home)
    boot_pid_path(root).write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(boot_mod, "_pid_alive", lambda pid: False)
    _write_boot_pid(root)
    assert int(boot_pid_path(root).read_text(encoding="utf-8").strip()) == os.getpid()
    _clear_boot_pid(root)
