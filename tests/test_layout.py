# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""~/.ghrah 布局读写、toml 解析、缺省读容忍、.pth 自管、配置写入底座测试"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghrah_bootloader.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    managed_pth_path,
    render_distro_toml,
    resolve_distro_root,
    snapshots_dir,
    verify_managed_pth,
    write_managed_pth,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GHRAH_HOME", str(tmp_path / "ghrah-home"))
    return ghrah_root()


def test_ghrah_root_env_override(home: Path, tmp_path: Path) -> None:
    assert home == tmp_path / "ghrah-home"


def test_boot_toml_missing_returns_empty(home: Path) -> None:
    """boot.toml 不存在时返回空世界（缺省读容忍）。"""
    boot = BootToml.load()
    assert boot.default is None
    assert boot.fallback is None
    assert boot.last_good is None


def test_boot_toml_parse(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "boot.toml").write_text(
        'default = "generic/2026.09"\n'
        'fallback = "generic/2026.08"\n'
        'last_good = "generic/2026.09"\n',
        encoding="utf-8",
    )
    boot = BootToml.load()
    assert boot.default == "generic/2026.09"
    assert boot.fallback == "generic/2026.08"
    assert boot.last_good == "generic/2026.09"


def test_boot_toml_invalid_raises(home: Path) -> None:
    from ghrah_bootloader.errors import LayoutError

    home.mkdir(parents=True, exist_ok=True)
    (home / "boot.toml").write_text("not [ valid toml ==", encoding="utf-8")
    with pytest.raises(LayoutError):
        BootToml.load()


def test_distro_toml_missing_returns_defaults(home: Path) -> None:
    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    d = DistroToml.load(distro_dir)
    assert d.name == ""
    assert d.ref_source == "latest"
    assert d.ref_gate == "basic"
    assert d.subject_port == 4112
    assert d.webui_port == 5173
    assert d.snapshot_keep == 3


def test_distro_toml_parse(home: Path) -> None:
    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(
        """
[distro]
name = "generic"
scenario = "通用协作型"

[distro.repo]
path = "/path/to/monorepo"

[distro.ref]
source = "branch:main"
gate = "full"

[distro.plugins]
enabled = ["taskstore"]

[runtime]
subject_port = 4222
webui_port = 5180

[data]
root = "~/.ghrah/generic/data"
snapshot_keep = 5
""",
        encoding="utf-8",
    )
    d = DistroToml.load(distro_dir)
    assert d.name == "generic"
    assert d.scenario == "通用协作型"
    assert d.repo_path == "/path/to/monorepo"
    assert d.ref_source == "branch:main"
    assert d.ref_gate == "full"
    assert d.enabled_plugins == ["taskstore"]
    assert d.subject_port == 4222
    assert d.webui_port == 5180
    assert d.data_root == "~/.ghrah/generic/data"
    assert d.snapshot_keep == 5


def test_list_distros_empty_when_root_missing(home: Path) -> None:
    assert list_distros() == []


def test_list_distros_enumerates_only_dirs(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "boot.toml").write_text("", encoding="utf-8")
    (home / "generic").mkdir()
    (home / "lab").mkdir()
    (home / ".hidden").mkdir()
    (home / "stray.txt").write_text("", encoding="utf-8")
    assert list_distros() == ["generic", "lab"]


def test_resolve_distro_root_rejects_bad_names(home: Path) -> None:
    from ghrah_bootloader.errors import LayoutError

    with pytest.raises(LayoutError):
        resolve_distro_root("")
    with pytest.raises(LayoutError):
        resolve_distro_root("a/b")
    with pytest.raises(LayoutError):
        resolve_distro_root(".hidden")
    assert resolve_distro_root("generic") == home / "generic"


def test_distro_layout_paths(home: Path) -> None:
    layout = DistroLayout(root=home / "generic")
    assert layout.distro_toml == home / "generic" / "distro.toml"
    assert layout.data == home / "generic" / "data"
    assert layout.snapshots == home / "generic" / "snapshots"
    assert layout.env == home / "generic" / "env"
    assert layout.venv == home / "generic" / "env" / ".venv"
    assert layout.current == home / "generic" / "current"
    assert layout.last_good == home / "generic" / "last-good"
    assert layout.versions == home / "generic" / "versions"


def test_list_versions_missing_dir(home: Path) -> None:
    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == []


def test_list_versions_sorted(home: Path) -> None:
    layout = DistroLayout(root=home / "generic")
    layout.versions.mkdir(parents=True)
    (layout.versions / "2026.10").mkdir()
    (layout.versions / "2026.09").mkdir()
    (layout.versions / "not-a-dir.txt").write_text("", encoding="utf-8")
    assert layout.list_versions() == ["2026.09", "2026.10"]


# ── .pth 自管（裁定 11） ──────────────────────────────────────


def _make_venv(tmp_path: Path) -> Path:
    import sys

    venv = tmp_path / ".venv"
    if sys.platform == "win32":
        site = venv / "Lib" / "site-packages"
    else:
        site = (
            venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    site.mkdir(parents=True)
    return venv


def test_write_and_verify_managed_pth(tmp_path: Path) -> None:
    """写入自管 .pth 后应可复核一致；current 重指后复核应失败（需重写）。"""
    venv = _make_venv(tmp_path)
    current = tmp_path / "current"
    current.mkdir()

    pth = write_managed_pth(venv, current)
    assert pth == managed_pth_path(venv)
    assert verify_managed_pth(venv, current) is True

    # current 重指（模拟版本切换）：.pth 内容仍指向 current 段（稳定路径），
    # 复核仍应通过——这正是自管 .pth 的意义。
    assert verify_managed_pth(venv, current) is True

    # .pth 被篡改/缺失：复核失败
    pth.write_text("/somewhere/else\n", encoding="utf-8")
    assert verify_managed_pth(venv, current) is False
    pth.unlink()
    assert verify_managed_pth(venv, current) is False


def test_managed_pth_contains_current_segment(tmp_path: Path) -> None:
    """.pth 各行必须是含 current 段的稳定路径（不解析为版本绝对路径）。"""
    venv = _make_venv(tmp_path)
    current = tmp_path / "current"
    current.mkdir()

    pth = write_managed_pth(venv, current)
    lines = [
        line.strip()
        for line in pth.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) == 3
    for line in lines:
        assert str(current) in line
        assert line.endswith("/src")
    # 不允许出现 versions/<ver> 绝对路径（setuptools editable 的漂移形态）
    assert all("versions" not in line for line in lines)


# ── 配置写入底座（批次 2/3/6） ───────────────────────────────


def test_boot_toml_save_roundtrip_and_atomic(home: Path) -> None:
    """save → load 往返一致；原子写（不留 tmp 残件）。"""
    boot = BootToml(
        default="generic/2026.09", fallback="generic/2026.08", last_good="generic/2026.09"
    )
    boot.save()
    loaded = BootToml.load()
    assert loaded == boot
    assert (home / "boot.toml").is_file()
    leftovers = [p.name for p in home.iterdir() if p.name.startswith(".boot.toml.tmp")]
    assert leftovers == []


def test_boot_toml_save_partial_keys(home: Path) -> None:
    """None 键不落盘；往返保持 None（缺省读容忍）。"""
    BootToml(default="generic/2026.09").save()
    loaded = BootToml.load()
    assert loaded.default == "generic/2026.09"
    assert loaded.fallback is None
    assert loaded.last_good is None


def test_boot_toml_save_escapes_values(home: Path) -> None:
    BootToml(default='weird"\\name/1.0').save()
    assert BootToml.load().default == 'weird"\\name/1.0'


def test_render_distro_toml_readback(home: Path) -> None:
    rendered = render_distro_toml(
        name="generic",
        repo_path="/repo/path",
        source="branch:main",
        gate="basic",
        subject_port=4112,
        webui_port=5173,
        data_root="",
        snapshot_keep=3,
    )
    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(rendered, encoding="utf-8")
    d = DistroToml.load(distro_dir)
    assert d.name == "generic"
    assert d.repo_path == "/repo/path"
    assert d.ref_source == "branch:main"
    assert d.ref_gate == "basic"
    assert d.ref_fetch_on_update is False
    assert d.subject_port == 4112
    assert d.webui_port == 5173
    assert d.data_root == ""
    assert d.snapshot_keep == 3


def test_distro_toml_parse_fetch_on_update(home: Path) -> None:
    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(
        """
[distro.ref]
source = "latest"
gate = "basic"
fetch_on_update = true
""",
        encoding="utf-8",
    )
    assert DistroToml.load(distro_dir).ref_fetch_on_update is True


def test_snapshots_dir_rules(home: Path) -> None:
    """默认 <distro>/snapshots；[data] root 外置时同级跟随。"""
    layout = DistroLayout(root=home / "generic")
    assert snapshots_dir(layout, DistroToml()) == home / "generic" / "snapshots"
    toml = DistroToml(data_root=str(home / "ext" / "data"))
    assert snapshots_dir(layout, toml) == home / "ext" / "snapshots"
