# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""winproc 平台模拟单测：pid 探测 / 映像名 / 重指三级回退。

全部经 mock（fake kernel32 / monkeypatch os.name / mock _winapi），锁定
「我方代码路径」（判定表、调用序、复核与回退）；Windows API 签名的真机
正确性属真机验收面（诚实边界）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ghrah_bootloader import winproc
from ghrah_bootloader.errors import WinProcError

# ── fake kernel32 夹具 ────────────────────────────────────────


class FakeKernel32:
    """可配置的 kernel32 替身：OpenProcess/GetExitCodeProcess/CloseHandle。

    行为由类属性（而非实例状态）驱动——winproc 经 ``ctypes`` 设置的
    restype/argtypes 会写回类对象，用 MagicMock 属性承接不进实例字典。
    """

    open_result: object = 1  # 非零句柄
    open_last_error: int = 0
    exit_code: int = 259  # STILL_ACTIVE
    get_exit_ok: bool = True
    image_result: str | None = r"C:\venv\Scripts\python.exe"
    query_image_ok: bool = True

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        FakeKernel32._open_calls.append((access, inherit, pid))
        FakeKernel32.last_error_value = FakeKernel32.open_last_error
        return FakeKernel32.open_result

    def GetExitCodeProcess(self, handle, out):  # noqa: N802
        if not FakeKernel32.get_exit_ok:
            return 0
        out.value = FakeKernel32.exit_code
        return 1

    def QueryFullProcessImageNameW(self, handle, flags, buf, size):  # noqa: N802
        if not FakeKernel32.query_image_ok or FakeKernel32.image_result is None:
            return 0
        buf.value = FakeKernel32.image_result
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        FakeKernel32._close_calls.append(handle)
        return 1

    _open_calls: list = []
    _close_calls: list = []
    last_error_value: int = 0


@pytest.fixture
def fake_kernel32(monkeypatch: pytest.MonkeyPatch) -> type[FakeKernel32]:
    FakeKernel32._open_calls = []
    FakeKernel32._close_calls = []
    FakeKernel32.open_result = 1
    FakeKernel32.open_last_error = 0
    FakeKernel32.exit_code = 259
    FakeKernel32.get_exit_ok = True
    FakeKernel32.image_result = r"C:\venv\Scripts\python.exe"
    FakeKernel32.query_image_ok = True
    FakeKernel32.last_error_value = 0
    monkeypatch.setattr(winproc, "_get_kernel32", lambda: FakeKernel32())
    monkeypatch.setattr(
        winproc.ctypes, "get_last_error", lambda: FakeKernel32.last_error_value, raising=False
    )
    return FakeKernel32


# ── pid_alive 六分支 ─────────────────────────────────────────


def test_pid_alive_alive(fake_kernel32) -> None:
    fake_kernel32.open_result, fake_kernel32.exit_code = 1, 259
    assert winproc.pid_alive(4242) is True


def test_pid_alive_exited(fake_kernel32) -> None:
    fake_kernel32.open_result, fake_kernel32.exit_code = 1, 3
    assert winproc.pid_alive(4242) is False


def test_pid_alive_pid_missing(fake_kernel32) -> None:
    fake_kernel32.open_result, fake_kernel32.open_last_error = 0, 87
    assert winproc.pid_alive(4242) is False


def test_pid_alive_access_denied_is_alive(fake_kernel32) -> None:
    fake_kernel32.open_result, fake_kernel32.open_last_error = 0, 5
    assert winproc.pid_alive(4242) is True


def test_pid_alive_other_open_error_is_none(fake_kernel32) -> None:
    fake_kernel32.open_result, fake_kernel32.open_last_error = 0, 6
    assert winproc.pid_alive(4242) is None


def test_pid_alive_no_kernel32_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "_get_kernel32", lambda: None)
    assert winproc.pid_alive(4242) is None


def test_pid_alive_nonpositive_is_false(fake_kernel32) -> None:
    assert winproc.pid_alive(0) is False
    assert winproc.pid_alive(-1) is False
    assert fake_kernel32._open_calls == []  # 提前返回，不触 API


def test_pid_alive_opens_with_limited_access(fake_kernel32) -> None:
    winproc.pid_alive(4242)
    access, inherit, pid = fake_kernel32._open_calls[0]
    assert access == 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION 最小权限
    assert inherit is False
    assert pid == 4242
    assert fake_kernel32._close_calls  # 句柄已关


# ── process_image ────────────────────────────────────────────


def test_process_image_ok(fake_kernel32) -> None:
    fake_kernel32.image_result = r"C:\Users\me\Scripts\ghrah-bootloader.exe"
    assert winproc.process_image(4242) == r"C:\Users\me\Scripts\ghrah-bootloader.exe"


def test_process_image_open_fail_is_none(fake_kernel32) -> None:
    fake_kernel32.open_result = 0
    assert winproc.process_image(4242) is None


def test_process_image_query_fail_is_none(fake_kernel32) -> None:
    fake_kernel32.query_image_ok = False
    assert winproc.process_image(4242) is None


def test_process_image_no_kernel32_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "_get_kernel32", lambda: None)
    assert winproc.process_image(4242) is None


# ── repoint_dir_link 三级回退 ─────────────────────────────────
# 注：repoint_dir_link 内部无 os.name 分支（平台分派在 runtime_ref 层），
# 此处直接在 Linux 文件系统上测真实链接语义；nt 分派面见 test_runtime_ref。


def test_repoint_symlink_replace_first_level(tmp_path: Path) -> None:
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    strategy = winproc.repoint_dir_link(tmp_path / "current", target)
    assert strategy == "symlink-replace"
    assert (tmp_path / "current").resolve() == target


def test_repoint_symlink_recreate_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """② os.replace 覆盖既有链接失败（目录类目标）→ 删旧建新。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    link = tmp_path / "current"
    link.symlink_to(tmp_path / "generations" / "nonexistent")  # 旧指向
    real_replace = os.replace
    calls: list[tuple[Path, Path]] = []

    def flaky_replace(src, dst, *a, **kw):
        calls.append((Path(src), Path(dst)))
        if len(calls) == 1:
            raise OSError("Access is denied")  # 首次 replace（覆盖目录类目标）失败
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(winproc.os, "replace", flaky_replace)
    strategy = winproc.repoint_dir_link(link, target)
    assert strategy == "symlink-recreate"
    assert link.resolve() == target


def test_repoint_junction_when_symlink_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """③ symlink 创建被拒（未开开发者模式）→ junction 回退。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    link = tmp_path / "current"

    real_symlink_to = Path.symlink_to

    def no_symlink(*a, **kw):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(winproc.Path, "symlink_to", no_symlink)
    junction_calls: list[tuple[Path, Path]] = []

    def fake_junction(lnk: Path, tgt: Path) -> None:
        junction_calls.append((Path(lnk), Path(tgt)))
        real_symlink_to(Path(lnk), tgt, target_is_directory=True)  # 可解析的替身

    monkeypatch.setattr(winproc, "create_junction", fake_junction)
    strategy = winproc.repoint_dir_link(link, target)
    assert strategy == "junction"
    assert junction_calls == [(link, target)]


def test_repoint_junction_unavailable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """③ 的 _winapi 不可用分支：Linux 上不可达，以显式 mock 锁定语义。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    link = tmp_path / "current"

    def no_symlink(*a, **kw):
        raise OSError("privilege not held")

    monkeypatch.setattr(winproc.Path, "symlink_to", no_symlink)
    monkeypatch.setattr(winproc, "_winapi", None)
    with pytest.raises(WinProcError, match="junction"):
        winproc.repoint_dir_link(link, target)


def test_repoint_verify_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复核不一致 → WinProcError（不留半切换态）。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    other = tmp_path / "generations" / "other"
    other.mkdir(parents=True)
    link = tmp_path / "current"

    real_resolve = Path.resolve

    def lying_resolve(self):
        return other if self == link else real_resolve(self)

    monkeypatch.setattr(winproc.Path, "resolve", lying_resolve)
    with pytest.raises(WinProcError, match="复核不一致"):
        winproc.repoint_dir_link(link, target)


def test_repoint_rejects_real_directory_as_old_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧占位是真实（非空）目录 → 拒绝误删。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    link = tmp_path / "current"
    link.mkdir()
    (link / "user-data.txt").write_text("important", encoding="utf-8")

    def no_symlink(*a, **kw):  # 迫使走到「清旧」失败
        raise OSError("privilege not held")

    monkeypatch.setattr(winproc.Path, "symlink_to", no_symlink)
    with pytest.raises(WinProcError):
        winproc.repoint_dir_link(link, target)
    assert (link / "user-data.txt").is_file()  # 数据未被删


def test_repoint_rejects_missing_target(tmp_path: Path) -> None:
    with pytest.raises(WinProcError, match="不是目录"):
        winproc.repoint_dir_link(tmp_path / "current", tmp_path / "generations" / "nope")


def test_repoint_clears_stale_tmp(tmp_path: Path) -> None:
    """上次失败遗留的 tmp 占位被清掉，不影响本次重指。"""
    target = tmp_path / "generations" / "2026.09"
    target.mkdir(parents=True)
    link = tmp_path / "current"
    stale = tmp_path / f"current.tmp-{os.getpid()}"
    stale.symlink_to(tmp_path / "generations" / "dead")  # 悬空旧 tmp
    strategy = winproc.repoint_dir_link(link, target)
    assert strategy == "symlink-replace"
    assert link.resolve() == target
