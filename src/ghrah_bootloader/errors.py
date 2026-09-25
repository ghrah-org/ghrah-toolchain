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


class SnapshotError(BootloaderError):
    """数据盘快照拍取/滚动/恢复失败。"""


class AdoptError(BootloaderError):
    """旧数据认领失败（守卫拒绝或移动中断已回搬）。"""


class BuildError(BootloaderError):
    """发行物构建/验证失败（子进程失败、产物不完整或校验不过）。"""


class MaterializeError(BootloaderError):
    """组合代物化失败（env 装配、清单读写或完整性校验不过）。"""
