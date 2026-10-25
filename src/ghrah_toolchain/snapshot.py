# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""数据盘快照：拍取 / 枚举 / N 代滚动 / 恢复（文件级拷贝 + META.toml）。

- 快照为 ``data/`` 整体文件级拷贝（含 sqlite WAL/SHM）；要求 OS 已停止
  （由引导器切换前「运行中拒绝」护栏保证）。
- ``snapshots/`` 永不进版本树；目录名 ``<ts>-v<old>→v<new>``（同秒冲突
  追加 ``~<n>`` 后缀，保持字典序 = 时间序）。
- META.toml 手写序列化（仅标量键）。
"""

from __future__ import annotations

import shutil
import time
import tomllib
from pathlib import Path

from ghrah_toolchain.errors import SnapshotError
from ghrah_toolchain.layout import (
    DistroLayout,
    DistroToml,
    resolve_data_root,
    snapshots_dir,
)

_META_NAME = "META.toml"


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def take(
    layout: DistroLayout,
    distro_toml: DistroToml,
    *,
    old_entry,
    new_entry,
    source: str,
    gate: str,
) -> Path | None:
    """对 ``data/`` 整体拍快照；数据盘不存在（首装无数据）返回 ``None``。

    仅在门禁通过后调用（调用序 gate → snapshot → repoint），故 META 中
    gate 结果记为 ``passed``。
    """
    data_root = resolve_data_root(layout, distro_toml)
    if not data_root.is_dir():
        return None
    base = snapshots_dir(layout, distro_toml)
    old_ver = old_entry.version if old_entry is not None else "none"
    old_commit = (old_entry.commit if old_entry is not None else "") or ""
    name = f"{_timestamp()}-v{old_ver}→v{new_entry.version}"
    snap_dir = base / name
    suffix = 0
    while snap_dir.exists():
        suffix += 1
        snap_dir = base / f"{name}~{suffix}"
    snap_dir.mkdir(parents=True)
    shutil.copytree(data_root, snap_dir / "data", symlinks=True)
    meta = [
        "# ghrah-toolchain 数据盘快照元数据",
        f'old_version = "{_escape(old_ver)}"',
        f'old_commit = "{_escape(old_commit)}"',
        f'new_version = "{_escape(new_entry.version)}"',
        f'new_commit = "{_escape(new_entry.commit or "")}"',
        f'time = "{time.strftime("%Y-%m-%dT%H:%M:%S")}"',
        f'source = "{_escape(source)}"',
        f'gate_level = "{_escape(gate)}"',
        'gate_result = "passed"',
    ]
    (snap_dir / _META_NAME).write_text("\n".join(meta) + "\n", encoding="utf-8")
    return snap_dir


def list_snapshots(layout: DistroLayout, distro_toml: DistroToml) -> list[Path]:
    """按目录名（= 时间）排序枚举快照；只认含 ``META.toml`` 的目录（stray 不算）。"""
    base = snapshots_dir(layout, distro_toml)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / _META_NAME).is_file())


def roll(layout: DistroLayout, distro_toml: DistroToml, keep: int) -> list[Path]:
    """保留最近 ``keep`` 代快照，删除更旧；返回被删除的目录列表。"""
    snaps = list_snapshots(layout, distro_toml)
    doomed = snaps[: max(len(snaps) - keep, 0)] if keep >= 0 else snaps
    for snap in doomed:
        shutil.rmtree(snap)
    return doomed


def restore(layout: DistroLayout, distro_toml: DistroToml, snapshot_dir: Path) -> str:
    """清空并覆盖回 ``data/``；返回快照记载的旧版本方向（供调用方重指 current）。"""
    if not (snapshot_dir / "data").is_dir():
        raise SnapshotError(f"快照不含 data/ 拷贝: {snapshot_dir}")
    meta_path = snapshot_dir / _META_NAME
    if not meta_path.is_file():
        raise SnapshotError(f"快照缺少 {_META_NAME}: {snapshot_dir}")
    try:
        meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SnapshotError(f"快照 {_META_NAME} 解析失败: {snapshot_dir}: {e}") from e
    old_version = str(meta.get("old_version", ""))
    data_root = resolve_data_root(layout, distro_toml)
    if data_root.exists():
        shutil.rmtree(data_root)
    shutil.copytree(snapshot_dir / "data", data_root, symlinks=True)
    return old_version
