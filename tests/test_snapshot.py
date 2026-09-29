# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""数据盘快照测试：拍取含 WAL/SHM、META 字段、N 代滚动、恢复往返。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ghrah_bootloader.errors import SnapshotError
from ghrah_bootloader.layout import DistroLayout, DistroToml, snapshots_dir
from ghrah_bootloader.runtime_ref import BootEntry
from ghrah_bootloader.snapshot import list_snapshots, restore, roll, take


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    return h


def _layout(home: Path) -> DistroLayout:
    return DistroLayout(root=home / "generic")


def _entry(version: str, commit: str = "cafe0000") -> BootEntry:
    return BootEntry(distro="generic", version=version, path=Path("/x"), commit=commit)


def _seed_data(home: Path, content: str = "db") -> Path:
    data = _layout(home).data
    data.mkdir(parents=True)
    (data / "subject.db").write_text(content, encoding="utf-8")
    (data / "subject.db-wal").write_text("wal", encoding="utf-8")
    (data / "subject.db-shm").write_text("shm", encoding="utf-8")
    (data / "taskstore.sqlite3").write_text("ts", encoding="utf-8")
    (data / "taskstore.sqlite3-wal").write_text("tswal", encoding="utf-8")
    (data / "manifests").mkdir()
    (data / "manifests" / "agent.yaml").write_text("agent", encoding="utf-8")
    return data


def test_take_includes_wal_shm_and_writes_meta(home: Path) -> None:
    _seed_data(home)
    layout, toml = _layout(home), DistroToml()
    snap = take(
        layout,
        toml,
        old_entry=_entry("2026.09", "aaa1111"),
        new_entry=_entry("2026.10", "bbb2222"),
        source="latest",
        gate="basic",
    )
    assert snap is not None and snap.is_dir()
    assert (snap / "data" / "subject.db-wal").exists()
    assert (snap / "data" / "subject.db-shm").exists()
    assert (snap / "data" / "taskstore.sqlite3-wal").exists()
    assert (snap / "data" / "manifests" / "agent.yaml").exists()
    assert "-v2026.09→v2026.10" in snap.name
    meta = tomllib.loads((snap / "META.toml").read_text(encoding="utf-8"))
    assert meta["old_version"] == "2026.09"
    assert meta["old_commit"] == "aaa1111"
    assert meta["new_version"] == "2026.10"
    assert meta["new_commit"] == "bbb2222"
    assert meta["source"] == "latest"
    assert meta["gate_level"] == "basic"
    assert meta["gate_result"] == "passed"
    assert "time" in meta


def test_take_skips_when_data_missing(home: Path) -> None:
    snap = take(
        _layout(home),
        DistroToml(),
        old_entry=_entry("2026.09"),
        new_entry=_entry("2026.10"),
        source="latest",
        gate="basic",
    )
    assert snap is None


def test_take_same_second_collision_gets_suffix(home: Path) -> None:
    _seed_data(home)
    layout, toml = _layout(home), DistroToml()
    args = dict(old_entry=_entry("1"), new_entry=_entry("2"), source="latest", gate="basic")
    first = take(layout, toml, **args)
    second = take(layout, toml, **args)
    assert first is not None and second is not None
    assert first != second
    assert len(list_snapshots(layout, toml)) == 2
    assert list_snapshots(layout, toml)[-1] == second  # 后拍者字典序在后（更新）


def test_roll_keeps_n_generations(home: Path) -> None:
    _seed_data(home)
    layout, toml = _layout(home), DistroToml()
    for old, new in (("1", "2"), ("2", "3"), ("3", "4")):
        take(
            layout,
            toml,
            old_entry=_entry(old),
            new_entry=_entry(new),
            source="latest",
            gate="basic",
        )
    assert len(list_snapshots(layout, toml)) == 3
    roll(layout, toml, keep=2)
    remaining = list_snapshots(layout, toml)
    assert len(remaining) == 2
    assert remaining[0].name.endswith("-v2→v3")
    assert remaining[1].name.endswith("-v3→v4")


def test_restore_roundtrip(home: Path) -> None:
    data = _seed_data(home, content="A")
    layout, toml = _layout(home), DistroToml()
    snap = take(
        layout,
        toml,
        old_entry=_entry("2026.09"),
        new_entry=_entry("2026.10"),
        source="latest",
        gate="basic",
    )
    assert snap is not None
    # 拍后继续写入（模拟快照后的写入窗口）
    (data / "subject.db").write_text("B", encoding="utf-8")
    (data / "extra.txt").write_text("later", encoding="utf-8")
    restored_version = restore(layout, toml, snap)
    assert restored_version == "2026.09"  # 返回旧版本方向
    assert (data / "subject.db").read_text(encoding="utf-8") == "A"  # 数据回到拍时点
    assert not (data / "extra.txt").exists()  # 快照后的写入丢失（明示语义）
    assert (data / "subject.db-wal").exists()


def test_restore_requires_meta(home: Path) -> None:
    _seed_data(home)
    layout, toml = _layout(home), DistroToml()
    snap = snapshots_dir(layout, toml) / "broken"
    (snap / "data").mkdir(parents=True)
    with pytest.raises(SnapshotError, match="META"):
        restore(layout, toml, snap)


def test_list_snapshots_ignores_stray_dirs(home: Path) -> None:
    _seed_data(home)
    layout, toml = _layout(home), DistroToml()
    take(
        layout,
        toml,
        old_entry=_entry("1"),
        new_entry=_entry("2"),
        source="latest",
        gate="basic",
    )
    stray = snapshots_dir(layout, toml) / "zz-stray-no-meta"
    stray.mkdir(parents=True)
    (stray / "junk").write_text("junk", encoding="utf-8")
    snaps = list_snapshots(layout, toml)
    assert snaps == [s for s in snaps if (s / "META.toml").is_file()]
    assert stray not in snaps


def test_snapshots_follow_external_data_root(home: Path, tmp_path: Path) -> None:
    layout = _layout(home)
    toml = DistroToml(data_root=str(tmp_path / "ext" / "data"))
    assert snapshots_dir(layout, toml) == tmp_path / "ext" / "snapshots"
    (tmp_path / "ext" / "data").mkdir(parents=True)
    (tmp_path / "ext" / "data" / "subject.db").write_text("x", encoding="utf-8")
    snap = take(
        layout,
        toml,
        old_entry=_entry("2026.09"),
        new_entry=_entry("2026.10"),
        source="latest",
        gate="basic",
    )
    assert snap is not None
    assert snap.parent == tmp_path / "ext" / "snapshots"
