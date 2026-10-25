# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""Windows 进程探测与目录链接原语（nt 分派目标的唯一落位）。

仅在 ``os.name == "nt"`` 时被 :mod:`ghrah_toolchain.runtime_ref` /
:mod:`ghrah_toolchain.boot` 分派进入；本模块可在任意平台安全 import
（``ctypes`` 标准库恒在；``_winapi`` 仅 Windows 存在，try-import 守卫）。

- :func:`pid_alive` —— OpenProcess + GetExitCodeProcess 三值探测
  （``None`` = 探测不可用；fail-closed 语义由调用方决定）；
- :func:`process_image` —— OpenProcess + QueryFullProcessImageNameW 取进程
  映像全路径（stop 护栏：pid 是否仍属于 ghrah-toolchain 家族）；
- :func:`repoint_dir_link` / :func:`create_junction` —— 目录链接三级回退
  重指（symlink+replace → 删旧建新 symlink → junction）。

纯 stdlib（ctypes / _winapi）。对 Windows API 的调用形态由平台模拟单测
锁定——mock 表面收敛于本模块；真机行为待真机验收。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path

from ghrah_toolchain.errors import WinProcError

__all__ = ["create_junction", "pid_alive", "process_image", "repoint_dir_link"]

# OpenProcess 最小权限：查询受限信息（存活探测与映像名查询够用）
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# GetExitCodeProcess 的「仍在运行」哨兵值。陷阱：进程真实退出码恰为 259
# 时会被误判存活（Windows 工具链通病）——实验级接受，真机验收观察项。
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5  # 进程存在但无权限 → 视为存活
_ERROR_INVALID_PARAMETER = 87  # pid 不存在（OpenProcess 对无效 pid 的典型返回）

try:  # Windows-only 标准库模块；Linux 上 import 即失败
    import _winapi
except ImportError:  # pragma: no cover - 平台相关
    _winapi = None


def _get_kernel32() -> ctypes.WinDLL | None:
    """惰性加载 kernel32；不可用（非预期平台）返回 ``None``。

    Linux 上 ``ctypes.WinDLL`` 属性本身不存在（AttributeError），
    非 Windows 平台调用则抛 OSError——两者都归一为 ``None``。
    """
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        return None
    try:
        return windll("kernel32", use_last_error=True)
    except OSError:
        return None


def _last_error() -> int:
    """取最近一次 ctypes 调用的 Windows 错误码。

    ``ctypes.get_last_error`` 仅 Windows 存在；平台模拟测试经
    monkeypatch 注入本模块依赖的 ``ctypes.get_last_error`` 属性。
    """
    getter = getattr(ctypes, "get_last_error", None)
    return getter() if getter is not None else 0


def _configure(func, restype, argtypes) -> None:
    """配置 ctypes 函数原型；非 ctypes 函数对象（测试 fake）跳过。"""
    try:
        func.restype = restype
        func.argtypes = argtypes
    except AttributeError, TypeError:  # 测试替身：纯 Python 方法
        pass


def _open_process(k32, pid: int):
    """配置并调用 OpenProcess；返回句柄（失败为 0/None）。"""
    _configure(k32.OpenProcess, ctypes.c_void_p, (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32))
    return k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def _close_handle(k32, handle) -> None:
    with contextlib.suppress(Exception):
        _configure(k32.CloseHandle, ctypes.c_int, (ctypes.c_void_p,))
        k32.CloseHandle(handle)


# ── pid 存活探测 ─────────────────────────────────────────────


def pid_alive(pid: int) -> bool | None:
    """Windows pid 存活探测（替换早期恒返回 True 的桩实现）。

    Returns:
        ``True``：进程存活（exit code = STILL_ACTIVE，或
            ERROR_ACCESS_DENIED——进程存在但无权限）；
        ``False``：进程已退出（exit code ≠ STILL_ACTIVE）或 pid 不存在
            （ERROR_INVALID_PARAMETER）；
        ``None``：探测不可用（kernel32 加载失败 / API 调用失败 /
            不可归类的 OpenProcess 错误）——调用方 fail-closed 处理。
    """
    if pid <= 0:
        return False
    k32 = _get_kernel32()
    if k32 is None:
        return None
    handle = _open_process(k32, pid)
    if not handle:
        err = _last_error()
        if err == _ERROR_ACCESS_DENIED:
            return True  # 进程存在但无权限
        if err == _ERROR_INVALID_PARAMETER:
            return False  # pid 不存在
        return None
    try:
        _configure(
            k32.GetExitCodeProcess,
            ctypes.c_int,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)),
        )
        exit_code = ctypes.c_uint32()
        # 直接传实例：真实 ctypes 对 POINTER(T) 形参自动取址（auto-byref）；
        # 测试 fake 则拿到实例本身写 .value
        if not k32.GetExitCodeProcess(handle, exit_code):
            return None
        return exit_code.value == _STILL_ACTIVE
    finally:
        _close_handle(k32, handle)


# ── 进程映像名（stop 护栏） ─────────────────────────────────


def process_image(pid: int) -> str | None:
    """进程映像全路径（``QueryFullProcessImageNameW``）；不可用返回 ``None``。

    ``None`` 时调用方应告警跳过护栏（对齐 posix 无 /proc 的降级语义）。
    """
    k32 = _get_kernel32()
    if k32 is None:
        return None
    handle = _open_process(k32, pid)
    if not handle:
        return None
    try:
        _configure(
            k32.QueryFullProcessImageNameW,
            ctypes.c_int,
            (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)),
        )
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, size):
            return None
        return buf.value
    finally:
        _close_handle(k32, handle)


# ── 目录链接重指（三级回退） ─────────────────────────────────


def create_junction(link: Path, target_abs: Path) -> None:
    """建目录 junction（无需特权；目标必须为绝对路径目录）。"""
    if _winapi is None:
        raise WinProcError("_winapi 不可用（非 Windows 平台）：无法创建 junction")
    _winapi.CreateJunction(str(target_abs), str(link))


def _clear_link(path: Path) -> None:
    """按形态清除链接占位；真实（非空）目录拒绝误删。"""
    if path.is_symlink():
        path.unlink()
        return
    if path.exists():
        # junction / 目录符号链接呈现为目录：rmdir 只摘链接本身；
        # 真实非空目录 rmdir 抛 OSError → fail-closed（不误删数据）。
        try:
            path.rmdir()
        except OSError as e:
            raise WinProcError(f"链接占位清除失败（可能为真实目录，拒绝误删）: {path}: {e}") from e


def _verify_target(link: Path, target_abs: Path) -> None:
    """复核链接解析结果与期望目标一致；不一致 fail-closed。"""
    resolved = link.resolve()
    if resolved != target_abs.resolve():
        raise WinProcError(
            f"重指复核不一致: {link} 解析到 {resolved}，期望 {target_abs}（不留半切换态）"
        )


def _try_symlink_replace(link: Path, tmp: Path, target_abs: Path) -> None:
    """建相对目标 symlink 并 ``os.replace`` 到位（原子路径，两级共用）。"""
    rel = os.path.relpath(target_abs, link.parent)
    if tmp.is_symlink() or tmp.exists():
        _clear_link(tmp)
    tmp.symlink_to(rel, target_is_directory=True)
    os.replace(tmp, link)


def repoint_dir_link(link: Path, target_abs: Path) -> str:
    """重指目录链接（三级回退），返回实际策略名。

    1. ``"symlink-replace"``：相对目标 symlink + ``os.replace``——开发者
       模式/管理员下成立，与 posix 路径完全同构（原子，域迁移不断链）；
    2. ``"symlink-recreate"``：``os.replace`` 覆盖目录类目标失败时
       「删旧建新」——非原子窗口以复核收口；
    3. ``"junction"``：symlink 创建本身被拒（未开开发者模式）时改用目录
       junction（无需特权；目标是绝对路径，域迁移会断链——README 声明）。

    任何一级复核不一致 → :class:`WinProcError`（fail-closed，不留半切换态）。
    """
    link = Path(link)
    target_abs = Path(target_abs)
    if not target_abs.is_dir():
        raise WinProcError(f"重指目标不是目录: {target_abs}")
    tmp = link.parent / f"{link.name}.tmp-{os.getpid()}"

    # ① 原子：symlink + os.replace
    try:
        _try_symlink_replace(link, tmp, target_abs)
        _verify_target(link, target_abs)
        return "symlink-replace"
    except OSError:
        pass

    # ② 删旧建新：腾空目标位后 replace 是纯文件系统操作（必成）
    try:
        _clear_link(link)
        _try_symlink_replace(link, tmp, target_abs)
        _verify_target(link, target_abs)
        return "symlink-recreate"
    except OSError:
        pass

    # ③ junction：symlink 被拒（特权缺失）的最后回退
    try:
        _clear_link(link)
        create_junction(link, target_abs)
        _verify_target(link, target_abs)
        return "junction"
    except OSError as e:
        raise WinProcError(f"重指失败（三级回退均不可用）: {link} → {target_abs}: {e}") from e
