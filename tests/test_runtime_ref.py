# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ref 状态机测试（批次 2/3：源解析 / 版本命名 / 切换顺序 / 原子性 / 锁）。

真实 git 子进程 + 临时 ``GHRAH_HOME``；``_sync`` / ``_gate`` / ``_run``
可 monkeypatch（避免真实 uv/pnpm 物化）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ghrah_bootloader.errors import GateError, RuntimeRefError
from ghrah_bootloader.layout import BootToml, DistroLayout, DistroToml
from ghrah_bootloader.runtime_ref import (
    BootEntry,
    RefSource,
    RuntimeRefManager,
    acquire_distro_lock,
    distro_lock_path,
    repoint_current,
)


@pytest.fixture(autouse=True)
def _no_running_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认视端口为空闲（避免真实环境里 4112 恰被占用导致用例漂移）。"""
    monkeypatch.setattr("ghrah_bootloader.runtime_ref.port_in_use", lambda host, port: False)


# ── 仓与安装域夹具 ───────────────────────────────────────────


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, date: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    _git(repo, "add", name, env=env)
    _git(repo, "commit", "-q", "-m", name, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "monorepo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "user.email", "t@t")
    _commit(r, "a", "2026-09-01T10:00:00 +0000")
    return r


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    # 既有用例锁定 worktree 链路（回归面）：预写显式模式的 distro.toml，
    # 绕过缺省 packages（B2 翻转缺省后旧用例的 ensure 会走构建分支）。
    distro_root = h / "generic"
    distro_root.mkdir(parents=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "worktree"

[distro.repo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    return h


def _mgr(home: Path, repo: Path) -> RuntimeRefManager:
    return RuntimeRefManager(root=home, repo=repo)


def _mock_sync_gate(mgr: RuntimeRefManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mgr, "_gate", lambda tree, level, mode="worktree": "passed")
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False, mode="worktree": None)


def _entry(version: str, commit: str = "cafe0000") -> BootEntry:
    return BootEntry(distro="generic", version=version, path=Path("/x"), commit=commit)


# ── RefSource ───────────────────────────────────────────────


def test_refsource_parse_and_render() -> None:
    assert RefSource.parse(None) == RefSource(kind="latest", value=None)
    assert RefSource.parse("latest").render() == "latest"
    assert RefSource.parse("branch:main") == RefSource(kind="branch", value="main")
    assert RefSource.parse("branch:main").render() == "branch:main"
    assert RefSource.parse("commit:abc123").render() == "commit:abc123"


def test_refsource_parse_rejects_invalid() -> None:
    for bad in ("", "tag:v1", "branch:", "commit:", "x:y:z", "latest:x", "branch"):
        with pytest.raises(RuntimeRefError, match="非法 ref 源"):
            RefSource.parse(bad)


# ── 源解析（D9/D10） ─────────────────────────────────────────


def test_resolve_candidate_latest(repo: Path) -> None:
    mgr = _mgr(Path("/nonexistent-home"), repo)
    assert mgr._resolve_candidate(RefSource.parse("latest"), fetch=False, repo=repo) == _git(
        repo, "rev-parse", "HEAD"
    )


def test_resolve_candidate_latest_picks_newest_ref_tip(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    _git(repo, "branch", "old-branch", old)
    mgr = _mgr(Path("/nonexistent-home"), repo)
    assert mgr._resolve_candidate(RefSource.parse("latest"), fetch=False, repo=repo) == new


def test_resolve_candidate_peels_annotated_tag(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", "-m", "v1", "v1")
    mgr = _mgr(Path("/nonexistent-home"), repo)
    assert mgr._resolve_candidate(RefSource.parse("latest"), fetch=False, repo=repo) == head


def test_resolve_candidate_branch_and_commit(repo: Path) -> None:
    mgr = _mgr(Path("/nonexistent-home"), repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert mgr._resolve_candidate(RefSource.parse("commit:" + head), fetch=False, repo=repo) == head
    _git(repo, "branch", "dev")
    assert mgr._resolve_candidate(RefSource.parse("branch:dev"), fetch=False, repo=repo) == head


def test_resolve_candidate_missing_ref_raises(repo: Path) -> None:
    mgr = _mgr(Path("/nonexistent-home"), repo)
    with pytest.raises(RuntimeRefError, match="候选 ref 解析失败"):
        mgr._resolve_candidate(RefSource.parse("branch:nope"), fetch=False, repo=repo)
    with pytest.raises(RuntimeRefError, match="候选 ref 解析失败"):
        mgr._resolve_candidate(RefSource.parse("commit:" + "0" * 40), fetch=False, repo=repo)


def test_resolve_candidate_fetch_triggers_git_fetch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(Path("/nonexistent-home"), repo)
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, env=None, check=True):
        calls.append([str(c) for c in cmd])
        if "for-each-ref" in cmd:
            stdout = _git(repo, "rev-parse", "HEAD")
        elif "rev-parse" in cmd:
            stdout = _git(repo, "rev-parse", "HEAD")
        else:
            stdout = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mgr, "_run", fake_run)
    mgr._resolve_candidate(RefSource.parse("latest"), fetch=True, repo=repo)
    assert "fetch" in calls[0]
    assert "--all" in calls[0]


def test_repo_priority_cli_env_toml(
    home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-q")
    distro_root = home / "generic"
    distro_root.mkdir(parents=True, exist_ok=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "worktree"

[distro.repo]
path = "{other}"
""",
        encoding="utf-8",
    )
    toml = DistroToml.load(distro_root)
    # 无 CLI 覆盖的管理器：toml / env 优先级
    mgr = RuntimeRefManager(root=home)
    monkeypatch.delenv("GHRAH_MONOREPO_ROOT", raising=False)
    assert mgr._resolve_repo(distro_root, toml) == other
    # env 覆盖 toml
    monkeypatch.setenv("GHRAH_MONOREPO_ROOT", str(tmp_path / "env-repo"))
    (tmp_path / "env-repo").mkdir()
    assert mgr._resolve_repo(distro_root, toml) == tmp_path / "env-repo"
    # --repo（构造器）覆盖 env
    assert _mgr(home, repo)._resolve_repo(distro_root, toml) == repo


def test_ensure_source_falls_back_to_toml(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    distro_root = home / "generic"
    distro_root.mkdir(parents=True, exist_ok=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "worktree"

[distro.repo]
path = "{repo}"

[distro.ref]
source = "commit:{head}"
""",
        encoding="utf-8",
    )
    mgr = RuntimeRefManager(root=home)
    _mock_sync_gate(mgr, monkeypatch)
    entry = mgr.ensure(distro="generic")
    assert entry.commit == head


# ── 版本命名与复用（D1） ─────────────────────────────────────


def test_version_name_derived_from_commit_date(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    entry = mgr.ensure(distro="generic")
    assert entry.version == "2026.09"
    assert (home / "generic" / "versions" / "2026.09").is_dir()
    assert (home / "generic" / "current").resolve().name == "2026.09"


def test_version_name_collision_appends_short_sha(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    first = mgr.ensure(distro="generic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    second = mgr.ensure(distro="generic", source=f"commit:{second_commit}")
    assert second.version != first.version
    assert second.version.startswith("2026.09-")
    assert second_commit.startswith(second.version.rsplit("-", 1)[1])
    assert len(DistroLayout(root=home / "generic").list_versions()) == 2


def test_installed_commit_reuses_tree(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    first = mgr.ensure(distro="generic")
    again = mgr.ensure(distro="generic", source=f"commit:{head}")
    assert again.version == first.version
    assert DistroLayout(root=home / "generic").list_versions() == [first.version]


def test_explicit_version_name_collision_suffixed_then_rejected(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式名同名不同 commit：追加短 sha 落位；短 sha 名也被占用（不同 commit）→ 报错。"""
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    mgr.ensure(distro="generic", version="myver")
    other = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    second = mgr.ensure(distro="generic", version="myver", source=f"commit:{other}")
    assert second.version.startswith("myver-")

    third = _commit(repo, "c", "2026-09-21T10:00:00 +0000")
    short = _git(repo, "rev-parse", "--short=7", third)
    # 预占短 sha 名（目录存在且 commit 与候选不同）
    layout = DistroLayout(root=home / "generic")
    (layout.versions / "myver").mkdir(parents=True, exist_ok=True)
    (layout.versions / f"myver-{short}").mkdir()
    with pytest.raises(RuntimeRefError, match="版本名冲突"):
        mgr.ensure(distro="generic", version="myver", source=f"commit:{third}")


# ── list_entries / resolve ───────────────────────────────────


def test_list_entries_and_resolve(home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    entry = mgr.ensure(distro="generic")
    entries = mgr.list_entries()
    assert [e.label for e in entries] == [entry.label]
    assert entries[0].commit == entry.commit
    assert mgr.resolve("generic").label == entry.label


def test_resolve_missing_current_raises(home: Path, repo: Path) -> None:
    mgr = _mgr(home, repo)
    with pytest.raises(RuntimeRefError, match="current"):
        mgr.resolve("generic")


def test_resolve_current_escape_rejected(home: Path, repo: Path) -> None:
    (home / "generic" / "versions").mkdir(parents=True)
    outside = home / "outside"
    outside.mkdir()
    (home / "generic" / "current").symlink_to(outside)
    mgr = _mgr(home, repo)
    with pytest.raises(RuntimeRefError, match="versions/ 之外"):
        mgr.resolve("generic")


# ── 切换顺序（D5） ──────────────────────────────────────────


def test_gate_failure_leaves_state_untouched(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    snapped: list[str] = []
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False, mode="worktree": None)
    monkeypatch.setattr(mgr, "_gate", _raising_gate)
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: snapped.append("snap"))
    with pytest.raises(GateError):
        mgr.ensure(distro="generic")
    layout = DistroLayout(root=home / "generic")
    assert not layout.current.exists()
    assert layout.list_versions() == []  # 本次新建树被移除
    assert snapped == []  # 未拍快照
    assert BootToml.load(home).default is None  # boot.toml 未动


def _raising_gate(tree, level, mode="worktree"):
    raise GateError("门禁注入失败")


def test_switch_order_gate_snapshot_repoint(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    order: list[str] = []
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False, mode="worktree": None)
    monkeypatch.setattr(
        mgr, "_gate", lambda tree, level, mode="worktree": order.append("gate") or "passed"
    )
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: order.append("snapshot") or None)
    real_repoint = repoint_current
    monkeypatch.setattr(
        "ghrah_bootloader.runtime_ref.repoint_current",
        lambda distro_root, ver: order.append("repoint") or real_repoint(distro_root, ver),
    )
    # 首次安装：无旧 current → 不拍快照
    mgr.ensure(distro="generic")
    assert order == ["gate", "repoint"]
    # 切换到新 commit：gate → snapshot → repoint
    order.clear()
    second = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    mgr.ensure(distro="generic", source=f"commit:{second}")
    assert order == ["gate", "snapshot", "repoint"]


def test_sync_failure_rolls_back_atomically(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    monkeypatch.setattr(mgr, "_gate", lambda tree, level, mode="worktree": "passed")
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False, mode="worktree": None)
    first = mgr.ensure(distro="generic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")

    calls: list[tuple[str, bool]] = []

    def failing_sync(distro, ver, *, force=False, mode="worktree"):
        calls.append((ver, force))
        if not force:
            raise RuntimeError("uv sync 模拟失败")

    monkeypatch.setattr(mgr, "_sync", failing_sync)
    with pytest.raises(RuntimeRefError, match="已回退"):
        mgr.ensure(distro="generic", source=f"commit:{second_commit}")
    layout = DistroLayout(root=home / "generic")
    assert layout.current.resolve().name == first.version  # 重指回旧版本
    assert (first.version, True) in calls  # 旧版本被强制重 sync
    assert layout.list_versions() == [first.version]  # 新建树已移除
    assert BootToml.load(home).default == first.label  # boot.toml 回写


def test_first_install_no_snapshot_and_sets_last_good(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    snapped: list[str] = []
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: snapped.append("snap"))
    entry = mgr.ensure(distro="generic")
    assert snapped == []  # 首装无旧 current，不拍快照
    layout = DistroLayout(root=home / "generic")
    assert layout.last_good.is_symlink()
    assert layout.last_good.resolve().name == entry.version
    boot = BootToml.load(home)
    assert boot.default == entry.label
    assert boot.last_good == entry.label


# ── 锁与运行中拒绝（D8/D14） ─────────────────────────────────


def test_lock_blocks_concurrent_ensure(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    distro_root = home / "generic"  # home 夹具已预建（含 distro.toml）
    with acquire_distro_lock(distro_root):
        with pytest.raises(RuntimeRefError, match="锁"):
            mgr.ensure(distro="generic")


def test_stale_lock_with_dead_pid_recycled(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    distro_root = home / "generic"
    distro_lock_path(distro_root).write_text("999999999", encoding="utf-8")
    entry = mgr.ensure(distro="generic")  # 陈旧锁（死 pid）被回收
    assert entry.version == "2026.09"


def test_rejects_when_running(home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ghrah_bootloader.runtime_ref.port_in_use", lambda host, port: True)
    mgr = _mgr(home, repo)
    with pytest.raises(RuntimeRefError, match="占用"):
        mgr.ensure(distro="generic")


def test_update_requires_existing_install(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    _mock_sync_gate(mgr, monkeypatch)
    with pytest.raises(RuntimeRefError, match="尚未安装"):
        mgr.ensure(distro="generic", update=True)


# ── gate 级别（§六） ─────────────────────────────────────────


def test_gate_full_and_data_rejected(home: Path, repo: Path) -> None:
    mgr = _mgr(home, repo)
    for level in ("full", "data"):
        with pytest.raises(GateError, match="未启用"):
            mgr._gate(Path("/tmp"), level)
    with pytest.raises(GateError, match="未知门禁级别"):
        mgr._gate(Path("/tmp"), "weird")


# ── 身份双跳（B2：显示面 BOOTSTRAP → git → None） ────────────


def test_tree_commit_dual_protocol(home: Path, repo: Path) -> None:
    from ghrah_bootloader.build import read_source_commit

    mgr = _mgr(home, repo)
    versions = home / "generic" / "versions"

    # 产物形态树（有 BOOTSTRAP 无 .git）→ BOOTSTRAP 值优先
    pkg_tree = versions / "2026.09"
    (pkg_tree / "wheels").mkdir(parents=True)
    (pkg_tree / "BOOTSTRAP.toml").write_text(
        f'[bootstrap]\nsource_commit = "{"a" * 40}"\n', encoding="utf-8"
    )
    assert read_source_commit(pkg_tree) == "a" * 40
    assert mgr._tree_commit(pkg_tree) == "a" * 40

    # worktree 形态树（无 BOOTSTRAP 有 .git）→ git 值
    wt_tree = versions / "2026.09-wt"
    _git(repo, "worktree", "add", "--detach", str(wt_tree), "HEAD")
    try:
        assert read_source_commit(wt_tree) is None
        assert mgr._tree_commit(wt_tree) == _git(repo, "rev-parse", "HEAD")
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt_tree))

    # 双缺目录 → None；损坏 BOOTSTRAP → 回退 git/None
    bare = versions / "2026.09-bare"
    bare.mkdir()
    assert mgr._tree_commit(bare) is None
    broken = versions / "2026.09-broken"
    broken.mkdir()
    (broken / "BOOTSTRAP.toml").write_text("not [ valid toml ==", encoding="utf-8")
    assert read_source_commit(broken) is None
    assert mgr._tree_commit(broken) is None


# ── 自管 .pth 接线（D3） ─────────────────────────────────────


def _fake_installed_env(home: Path) -> tuple[Path, Path]:
    """物化假发行版：树 + venv site-packages（含 ghrah editable 工件）+ current。"""
    distro_root = home / "generic"
    tree = distro_root / "versions" / "2026.09"
    site = distro_root / "env" / ".venv" / "lib" / "python3.99" / "site-packages"
    site.mkdir(parents=True)
    # setuptools 形态（spike 时代）与 uv 形态（0.10.x 实测：_editable_impl_<pkg>.pth，
    # 内容为版本树绝对路径，会遮蔽 current 段——真实集成用例暴露）
    (site / "__editable__.ghrah_core-0.1.0.pth").write_text(str(tree), encoding="utf-8")
    (site / "_editable_impl_ghrah_core.pth").write_text(str(tree), encoding="utf-8")
    (site / "__editable__.other_pkg-1.0.pth").write_text("/elsewhere", encoding="utf-8")
    tree.mkdir(parents=True, exist_ok=True)
    for rel in ("uv.lock", "pyproject.toml", "pnpm-lock.yaml"):
        (tree / rel).write_text(rel, encoding="utf-8")
    repoint_current(distro_root, "2026.09")
    return distro_root / "env" / ".venv", site


def test_sync_writes_managed_pth_and_cleans_editable(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv, site = _fake_installed_env(home)
    mgr = _mgr(home, repo)
    cmds: list[list[str]] = []

    def fake_run(cmd, cwd=None, env=None, check=True):
        cmds.append([str(c) for c in cmd])
        stdout = ""
        if any("ghrah.core" in part and "sys.path" in part for part in cmd):
            stdout = "OK"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mgr, "_run", fake_run)
    mgr._sync("generic", "2026.09")
    assert any(c[0] == "uv" and "sync" in c for c in cmds)  # 指纹变化 → 全量物化
    assert any(c[0] == "pnpm" and "install" in c for c in cmds)
    assert not (site / "__editable__.ghrah_core-0.1.0.pth").exists()  # ghrah 工件清理
    assert not (site / "_editable_impl_ghrah_core.pth").exists()  # uv 形态同样清理
    assert (site / "__editable__.other_pkg-1.0.pth").exists()  # 非 ghrah 工件保留
    from ghrah_bootloader.layout import managed_pth_path, verify_managed_pth

    current = home / "generic" / "current"
    assert managed_pth_path(venv).is_file()
    assert verify_managed_pth(venv, current) is True
    assert (home / "generic" / "env" / ".deps-fingerprint").is_file()

    # 指纹一致 → 跳过 uv/pnpm，仅复核 .pth
    cmds.clear()
    (managed_pth_path(venv)).write_text("/drifted\n", encoding="utf-8")
    mgr._sync("generic", "2026.09")
    assert not any(c[0] == "uv" for c in cmds)
    assert verify_managed_pth(venv, current) is True  # 漂移被复核重写
