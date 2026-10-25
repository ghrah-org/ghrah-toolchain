# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""~/.ghrah 布局读写、toml 解析、缺省读容忍、配置写入底座测试"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghrah_toolchain.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    render_distro_toml,
    resolve_distro_root,
    snapshots_dir,
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
    from ghrah_toolchain.errors import LayoutError

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


@pytest.mark.parametrize(
    ("document", "scenario", "repo_path"),
    [
        (
            'distro = {\n scenario = "multiline",\n repo = {path = "/work/repo"},\n}\n',
            "multiline",
            "/work/repo",
        ),
        (r'distro = { scenario = "\x41" }', "A", ""),
        (r'distro = { scenario = "\e[demo]" }', "\x1b[demo]", ""),
    ],
    ids=["multiline-inline-table", "hex-escape", "escape-character"],
)
def test_distro_toml_reads_toml_11(
    home: Path, document: str, scenario: str, repo_path: str
) -> None:
    """实际配置读取入口支持 TOML 1.1 新增语法。"""
    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(document, encoding="utf-8")

    distro = DistroToml.load(distro_dir)

    assert distro.scenario == scenario
    assert distro.repo_path == repo_path


def test_distro_toml_install_mode_worktree_retired(home: Path) -> None:
    """install_mode = "worktree" 已退役 → load 显式报错（不静默容忍）。"""
    from ghrah_toolchain.errors import LayoutError

    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(
        """
[distro]
install_mode = "worktree"
""",
        encoding="utf-8",
    )
    with pytest.raises(LayoutError, match="已退役"):
        DistroToml.load(distro_dir)


def test_distro_toml_install_mode_key_retired(home: Path) -> None:
    """install_mode 键整体退役（任何值都报错，提示删除）。"""
    from ghrah_toolchain.errors import LayoutError

    distro_dir = home / "generic"
    distro_dir.mkdir(parents=True)
    (distro_dir / "distro.toml").write_text(
        """
[distro]
install_mode = "packages"
""",
        encoding="utf-8",
    )
    with pytest.raises(LayoutError, match="已退役"):
        DistroToml.load(distro_dir)


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
    from ghrah_toolchain.errors import LayoutError

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


# ── 配置写入底座 ───────────────────────────────


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


def test_generations_listing(home: Path) -> None:
    """generations 属性与 list_generations（空目录/缺目录 → 空）。"""
    layout = DistroLayout(root=home / "generic")
    assert layout.generations == home / "generic" / "generations"
    assert layout.list_generations() == []  # 缺目录 → 空
    (layout.generations / "2026.09").mkdir(parents=True)
    (layout.generations / "2026.09+p12345678").mkdir()
    (layout.generations / "stray.txt").write_text("x", encoding="utf-8")
    assert layout.list_generations() == ["2026.09", "2026.09+p12345678"]
