# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""旧数据认领测试：全量/部分认领、守卫拒绝、失败回搬、备份保留、外置数据盘。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ghrah_toolchain import adopt as adopt_mod
from ghrah_toolchain.adopt import adopt, detect_legacy
from ghrah_toolchain.errors import AdoptError
from ghrah_toolchain.layout import DistroLayout, DistroToml


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    h.mkdir(parents=True)
    return h


def _layout(home: Path) -> DistroLayout:
    return DistroLayout(root=home / "generic")


def _toml(free_ports: tuple[int, int], **kw) -> DistroToml:
    """构造 DistroToml，端口取动态空闲端口。

    端口是进程外全局资源，无法用 ``GHRAH_HOME`` 隔离；测试若复用产品默认
    端口（4112/5173），就会与同机上运行中的实例互斥。
    """
    return DistroToml(subject_port=free_ports[0], webui_port=free_ports[1], **kw)


def _seed_full(home: Path) -> None:
    """顶层全量遗留：五项 + ghrah.db 边车 + bak + data/run_*。"""
    for name in ("subject.db", "subject.db-wal", "subject.db-shm", "taskstore.sqlite3"):
        (home / name).write_text(name, encoding="utf-8")
    (home / "manifests").mkdir()
    (home / "manifests" / "agent.yaml").write_text("agent", encoding="utf-8")
    (home / "projects").mkdir()
    (home / "projects" / "proj-1").mkdir()
    for name in ("ghrah.db", "ghrah.db-wal", "subject.db.pre-project-migration.bak"):
        (home / name).write_text(name, encoding="utf-8")
    (home / "data" / "run_x").mkdir(parents=True)
    (home / "data" / "run_x" / "out.txt").write_text("run", encoding="utf-8")


def test_detect_legacy(home: Path) -> None:
    assert detect_legacy(home) == []
    _seed_full(home)
    found = detect_legacy(home)
    assert "subject.db" in found and "manifests" in found and "ghrah.db" in found
    assert "data" in found


def test_adopt_full(home: Path, free_ports: tuple[int, int]) -> None:
    _seed_full(home)
    layout, toml = _layout(home), _toml(free_ports)
    report = adopt(layout, toml)
    data = layout.data
    assert report.noop is False
    # 五项 → 数据盘
    assert (data / "subject.db").read_text(encoding="utf-8") == "subject.db"
    assert (data / "subject.db-wal").exists() and (data / "subject.db-shm").exists()
    assert (data / "taskstore.sqlite3").exists()
    assert (data / "manifests" / "agent.yaml").exists()
    assert (data / "projects" / "proj-1").is_dir()
    # 非五项 → legacy/adopt-<ts>/
    assert report.legacy_dir is not None
    lg = report.legacy_dir
    assert (lg / "ghrah.db").exists() and (lg / "ghrah.db-wal").exists()
    assert (lg / "subject.db.pre-project-migration.bak").exists()
    assert (lg / "data" / "run_x" / "out.txt").exists()
    # 备份保留
    assert (lg / "backup" / "subject.db").exists()
    assert (lg / "backup" / "data" / "run_x" / "out.txt").exists()
    # 顶层不变式：无任何标记物残留
    assert detect_legacy(home) == []


def test_adopt_partial_markers(home: Path, free_ports: tuple[int, int]) -> None:
    (home / "subject.db").write_text("db", encoding="utf-8")
    layout, toml = _layout(home), _toml(free_ports)
    report = adopt(layout, toml)
    assert report.adopted == ["subject.db"]
    assert report.archived == []
    assert (layout.data / "subject.db").exists()
    assert detect_legacy(home) == []


def test_adopt_noop_when_clean(home: Path) -> None:
    report = adopt(_layout(home), DistroToml())
    assert report.noop is True
    assert report.legacy_dir is None


def test_adopt_rejects_when_port_in_use(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_full(home)
    monkeypatch.setattr(adopt_mod, "port_in_use", lambda host, port: True)
    with pytest.raises(AdoptError, match="端口"):
        adopt(_layout(home), DistroToml())
    assert (home / "subject.db").exists()  # 未改动


def test_adopt_rejects_data_plane_conflict(home: Path, free_ports: tuple[int, int]) -> None:
    _seed_full(home)
    layout = _layout(home)
    layout.data.mkdir(parents=True)
    (layout.data / "subject.db").write_text("existing", encoding="utf-8")
    with pytest.raises(AdoptError, match="同名"):
        adopt(layout, _toml(free_ports))
    assert (home / "subject.db").read_text(encoding="utf-8") == "subject.db"


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd 扫描")
def test_adopt_rejects_open_fd(home: Path, free_ports: tuple[int, int]) -> None:
    (home / "subject.db").write_text("db", encoding="utf-8")
    with open(home / "subject.db") as fh:  # noqa: SIM115
        with pytest.raises(AdoptError, match="句柄"):
            adopt(_layout(home), _toml(free_ports))
        assert fh.read(1) == "d"


def test_adopt_rolls_back_on_failure(
    home: Path, free_ports: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_full(home)
    layout, toml = _layout(home), _toml(free_ports)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 3:  # 第三个标记物移动失败
            raise OSError("simulated mid-adopt failure")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(adopt_mod.os, "replace", flaky_replace)
    with pytest.raises(AdoptError, match="回搬"):
        adopt(layout, toml)
    monkeypatch.setattr(adopt_mod.os, "replace", real_replace)
    # 回搬后：顶层原样、数据盘无残留
    assert detect_legacy(home) == detect_legacy(home)
    assert sorted(detect_legacy(home)) == sorted(
        [
            "subject.db",
            "subject.db-wal",
            "subject.db-shm",
            "taskstore.sqlite3",
            "manifests",
            "projects",
            "ghrah.db",
            "ghrah.db-wal",
            "subject.db.pre-project-migration.bak",
            "data",
        ]
    )
    assert not (layout.data / "manifests").exists() or (home / "manifests").exists()
    data_items = [p.name for p in layout.data.iterdir()] if layout.data.is_dir() else []
    assert data_items == [], f"数据盘应无残留，实得 {data_items}"


def test_adopt_second_time_is_noop(home: Path, free_ports: tuple[int, int]) -> None:
    _seed_full(home)
    layout, toml = _layout(home), _toml(free_ports)
    first = adopt(layout, toml)
    assert first.noop is False
    second = adopt(layout, toml)
    assert second.noop is True


def test_adopt_external_data_root(home: Path, tmp_path: Path, free_ports: tuple[int, int]) -> None:
    _seed_full(home)
    layout = _layout(home)
    toml = _toml(free_ports, data_root=str(tmp_path / "ext" / "data"))
    report = adopt(layout, toml)
    assert report.noop is False
    assert (tmp_path / "ext" / "data" / "subject.db").exists()
    assert (tmp_path / "ext" / "data" / "manifests" / "agent.yaml").exists()
    assert detect_legacy(home) == []
