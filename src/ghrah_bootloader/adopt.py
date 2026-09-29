# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""旧数据认领：``~/.ghrah`` 顶层遗留数据一次性移动进发行版数据盘。

一次性过渡命令（``install --adopt-legacy-data``），语义：

- **移动非复制**：五项数据面（``subject.db``/``taskstore.sqlite3`` 各含
  -wal/-shm、``manifests/``、``projects/``）→ ``resolve_data_root()`` 派生
  路径（支持 ``[data] root`` 外置）；``workspace/`` 无遗留，由数据盘保证。
- **非五项遗留物归档不删除**：``ghrah.db``(含 -wal/-shm)、
  ``subject.db.pre-project-migration.bak``、顶层 ``data/`` →
  ``<distro>/legacy/adopt-<ts>/``。
- **先备份后移动，失败回搬**：移动前整体拷贝到
  ``legacy/adopt-<ts>/backup/``；逐项 ``os.replace``，任一步失败 → 逆序
  回搬已移动项 → :class:`AdoptError`。成功后备份保留，用户确认后手删。
- **守卫**：端口占用 / 数据盘同名冲突 / POSIX 下文件句柄在用，任一命中
  即拒绝（WAL 与主库必须同窗移动）。

不变式（认领后）：顶层只有 ``boot.toml`` 与 distro 目录（隐藏锁/pid 除外）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ghrah_bootloader.errors import AdoptError
from ghrah_bootloader.layout import DistroLayout, DistroToml, resolve_data_root
from ghrah_bootloader.lifecycle import port_in_use

logger = logging.getLogger(__name__)

# 五项数据面（数据库主文件 + WAL/SHM 边车 + 目录）
_DATA_PLANE_FILES = (
    "subject.db",
    "subject.db-wal",
    "subject.db-shm",
    "taskstore.sqlite3",
    "taskstore.sqlite3-wal",
    "taskstore.sqlite3-shm",
)
_DATA_PLANE_DIRS = ("manifests", "projects")

# 非五项遗留物（归档不删除）
_LEGACY_FILES = (
    "ghrah.db",
    "ghrah.db-wal",
    "ghrah.db-shm",
    "subject.db.pre-project-migration.bak",
)
_LEGACY_DIRS = ("data",)

_ALL_MARKERS: tuple[str, ...] = (
    *_DATA_PLANE_FILES,
    *_DATA_PLANE_DIRS,
    *_LEGACY_FILES,
    *_LEGACY_DIRS,
)

_DEFAULT_HOST = "127.0.0.1"


@dataclass
class AdoptReport:
    """认领结果清单。"""

    adopted: list[str] = field(default_factory=list)  # 移入数据盘的标记物
    archived: list[str] = field(default_factory=list)  # 归档进 legacy/ 的标记物
    legacy_dir: Path | None = None  # 本次 adopt 的 legacy/adopt-<ts> 目录
    noop: bool = False  # 顶层无遗留标记物

    @property
    def noop_reason(self) -> str:  # 仅作文档提示用
        return "顶层无遗留数据"


def detect_legacy(root: Path) -> list[str]:
    """枚举顶层存在的遗留标记物（五项 + 非五项；供 install 提示与 adopt 复用）。"""
    if not root.is_dir():
        return []
    return [m for m in _ALL_MARKERS if (root / m).exists()]


def adopt(
    layout: DistroLayout,
    distro_toml: DistroToml,
    *,
    subject_port: int | None = None,
) -> AdoptReport:
    """执行认领：守卫 → 备份 → 逐项移动 → 校验；失败回搬。

    Args:
        layout: 发行版布局视图。
        distro_toml: 发行版声明（数据盘根 / 端口取自此处，可被 ``subject_port`` 覆盖）。
        subject_port: 运行中探测端口（默认取 ``distro_toml.subject_port``）。

    Raises:
        AdoptError: 守卫拒绝（未改动任何文件）或移动中断（已回搬）。
    """
    root = layout.root.parent
    markers = detect_legacy(root)
    if not markers:
        print("顶层无遗留数据，无需认领。")
        return AdoptReport(noop=True)

    port = subject_port if subject_port is not None else distro_toml.subject_port
    _guard(layout, distro_toml, markers, port=port)

    stamp = time.strftime("%Y%m%dT%H%M%S")
    legacy_dir = layout.root / "legacy" / f"adopt-{stamp}"
    backup_dir = legacy_dir / "backup"
    legacy_dir.mkdir(parents=True, exist_ok=False)

    try:
        _backup(root, markers, backup_dir)
        report = _move_all(root, layout, distro_toml, markers, legacy_dir)
    except Exception:
        # _move_all 内部已回搬；此处清理本次新建的空 legacy 目录（尽力）
        with contextlib.suppress(OSError):
            backup_dir.rmdir()
            (layout.root / "legacy" / f"adopt-{stamp}").rmdir()
            (layout.root / "legacy").rmdir()
        raise
    report.legacy_dir = legacy_dir
    _verify(root, layout, distro_toml, report)
    return report


# ── 守卫（A3） ───────────────────────────────────────────────


def _guard(layout: DistroLayout, distro_toml: DistroToml, markers: list[str], *, port: int) -> None:
    """认领守卫：端口占用 / 数据盘同名冲突 / 文件句柄在用。"""
    if port_in_use(_DEFAULT_HOST, port):
        raise AdoptError(
            f"端口 {port} 被占用：认领要求 OS 已停止（先 ghrah-bootloader stop / Ctrl-C）"
        )
    data_root = resolve_data_root(layout, distro_toml)
    conflicts = [
        m
        for m in markers
        if m in (*_DATA_PLANE_FILES, *_DATA_PLANE_DIRS) and (data_root / m).exists()
    ]
    if conflicts:
        raise AdoptError(f"数据盘已存在同名项 {conflicts}（{data_root}）：拒绝二次认领/合并")
    holders = _find_fd_holders(layout.root.parent, markers)
    if holders:
        raise AdoptError(
            f"以下进程持有待移动文件的句柄（搬走 inode 会造成静默脑裂）: {holders}；"
            "请先停止相关进程再认领"
        )


def _find_fd_holders(root: Path, markers: list[str]) -> list[int]:
    """POSIX best-effort：扫描 /proc/<pid>/fd，返回持有标记物句柄的 pid。非 POSIX 跳过。"""
    if os.name != "posix" or not Path("/proc").is_dir():
        return []
    targets = {str(root / m) for m in markers}
    holders: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        fd_dir = proc / "fd"
        if not fd_dir.is_dir():
            continue
        try:
            for fd in fd_dir.iterdir():
                with contextlib.suppress(OSError):
                    if str(fd.resolve()) in targets:
                        holders.append(int(proc.name))
                        break
        except PermissionError:
            continue
    return holders


# ── 备份与移动（A4） ─────────────────────────────────────────


def _backup(root: Path, markers: list[str], backup_dir: Path) -> None:
    """整体备份标记物到 ``legacy/adopt-<ts>/backup/``（copytree/copy2 保守拷贝）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    for m in markers:
        src = root / m
        if src.is_dir():
            shutil.copytree(src, backup_dir / m, symlinks=True)
        else:
            shutil.copy2(src, backup_dir / m)


def _move_all(
    root: Path,
    layout: DistroLayout,
    distro_toml: DistroToml,
    markers: list[str],
    legacy_dir: Path,
) -> AdoptReport:
    """逐项移动（数据面 → 数据盘；遗留物 → legacy/）；失败逆序回搬。"""
    data_root = resolve_data_root(layout, distro_toml)
    data_root.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []  # (src, dst) 已成功移动项，回搬用
    report = AdoptReport()
    try:
        for m in markers:
            src = root / m
            if not src.exists():
                continue
            if m in _DATA_PLANE_FILES or m in _DATA_PLANE_DIRS:
                dst = data_root / m
            else:
                dst = legacy_dir / m
            os.replace(src, dst)
            moved.append((src, dst))
            if dst.parent == data_root:
                report.adopted.append(m)
            else:
                report.archived.append(m)
    except Exception as exc:
        logger.error("认领移动失败（%s），逆序回搬 %d 项", exc, len(moved))
        for src, dst in reversed(moved):
            with contextlib.suppress(OSError):
                os.replace(dst, src)
        raise AdoptError(f"认领中断已回搬（{exc}）；备份保留待人工核查") from exc
    return report


def _verify(root: Path, layout: DistroLayout, distro_toml: DistroToml, report: AdoptReport) -> None:
    """校验：五项标记物清零（workspace 除外）、数据面就位。"""
    remaining = detect_legacy(root)
    if remaining:
        raise AdoptError(f"认领后顶层仍有标记物 {remaining}（不应发生，备份在 legacy/）")
    data_root = resolve_data_root(layout, distro_toml)
    missing = [m for m in report.adopted if not (data_root / m).exists()]
    if missing:
        raise AdoptError(f"数据盘缺少已认领项 {missing}（备份在 legacy/）")
