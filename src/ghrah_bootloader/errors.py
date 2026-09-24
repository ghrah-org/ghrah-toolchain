# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""引导器错误类型。"""

from __future__ import annotations


class BootloaderError(Exception):
    """引导器基础错误。"""


class RuntimeRefError(BootloaderError):
    """版本引用解析/切换失败（候选无效、门禁拒绝或同步失败）。"""


class GateError(BootloaderError):
    """启动门禁失败（basic / full / data 任一级别未通过）。"""


class LayoutError(BootloaderError):
    """~/.ghrah 安装域布局读写错误。"""
