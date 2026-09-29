# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ref 状态机测试（源解析 / 版本命名 / 切换顺序 / 原子性 / 锁 / 平台分派）。

packages：真实 git 子进程 + 临时 ``GHRAH_HOME``；
``build_artifacts`` / ``materialize_generation`` / ``_gate_generation`` 以假实现
替换（避免真实构建与装配），``_sync`` 可 monkeypatch。

形态无关的状态机语义（候选解析 / 命名 / 复用 / 锁 / 回滚骨架）在此锁定；
packages 全链路断言主阵地在 ``test_install_packages.py``。
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
    distro_root = h / "generic"
    distro_root.mkdir(parents=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]

[distro.repo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    return h


def _mgr(home: Path, repo: Path) -> RuntimeRefManager:
    return RuntimeRefManager(root=home, repo=repo)


def _mock_sync_gate(mgr: RuntimeRefManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mgr, "_gate", lambda gen_dir, level: "passed")
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False: None)


def _entry(version: str, commit: str = "cafe0000") -> BootEntry:
    return BootEntry(distro="generic", version=version, path=Path("/x"), commit=commit)


# ── 假构建/物化基础设施（形态无关状态机用例复用） ────────────


def _materialize_fake_artifacts(tree: Path, commit: str) -> None:
    """物化最小产物树骨架（身份面 + 清单面）。"""
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "wheels").mkdir(exist_ok=True)
    (tree / "wheels" / "pins.txt").write_text("third-party-pins\n", encoding="utf-8")
    (tree / "BOOTSTRAP.toml").write_text(
        f'[bootstrap]\nsource_commit = "{commit}"\nbuilt_at = "2026-09-25T00:00:00Z"\n',
        encoding="utf-8",
    )


def _materialize_fake_generation(gen_dir: Path, spec) -> None:
    """物化最小代骨架（GENERATION + 指纹 + env 完整性探针所需文件）。"""
    from ghrah_bootloader.materialize import (
        FINGERPRINT_NAME,
        GENERATION_TOML,
        generation_fingerprint,
        render_generation_toml,
    )

    gen_dir.mkdir(parents=True, exist_ok=True)
    py = gen_dir / "env" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        py.chmod(0o755)
    (gen_dir / "env" / "node" / "node_modules").mkdir(parents=True, exist_ok=True)
    (gen_dir / GENERATION_TOML).write_text(
        render_generation_toml(spec, built_at="2026-09-25T00:00:00Z"), encoding="utf-8"
    )
    (gen_dir / FINGERPRINT_NAME).write_text(generation_fingerprint(spec) + "\n", encoding="utf-8")


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch, repo: Path):
    """假构建/物化/门禁：记录调用、物化骨架；可注入失败。"""
    calls: dict[str, list] = {"build": [], "materialize": []}
    fail: dict[str, Exception | None] = {"build": None, "materialize": None}

    def _build(repo_path, commit, *, out_dir):
        calls["build"].append((commit, Path(out_dir)))
        if fail["build"] is not None:
            raise fail["build"]
        _materialize_fake_artifacts(Path(out_dir), commit)

    def _materialize(distro_root, base_tree, spec, *, rebuild=False):
        calls["materialize"].append((Path(base_tree), spec.gen_id(), rebuild))
        if fail["materialize"] is not None:
            raise fail["materialize"]
        _materialize_fake_generation(Path(distro_root) / "generations" / spec.gen_id(), spec)

    def _gate_generation(self_or_dir, gen_dir=None):
        calls.setdefault("gate", []).append(Path(gen_dir if gen_dir is not None else self_or_dir))

    import ghrah_bootloader.runtime_ref as rr

    monkeypatch.setattr(rr, "materialize_generation", _materialize)
    monkeypatch.setattr(rr, "build_artifacts", _build)
    monkeypatch.setattr(RuntimeRefManager, "_gate_generation", _gate_generation)
    return type("FakeBuild", (), {"calls": calls, "fail": fail})()


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


# ── 源解析 ─────────────────────────────────────────


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
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    distro_root = home / "generic"
    distro_root.mkdir(parents=True, exist_ok=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro.repo]
path = "{repo}"

[distro.ref]
source = "commit:{head}"
""",
        encoding="utf-8",
    )
    mgr = RuntimeRefManager(root=home)
    entry = mgr.ensure(distro="generic")
    assert entry.commit == head


# ── 版本命名与复用 ─────────────────────────────────────


def test_version_name_derived_from_commit_date(home: Path, repo: Path, fake_build) -> None:
    mgr = _mgr(home, repo)
    entry = mgr.ensure(distro="generic")
    assert entry.version == "2026.09"
    assert (home / "generic" / "versions" / "2026.09").is_dir()
    assert (home / "generic" / "current").resolve().name == "2026.09"


def test_version_name_collision_appends_short_sha(home: Path, repo: Path, fake_build) -> None:
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    second = mgr.ensure(distro="generic", source=f"commit:{second_commit}")
    assert second.version != first.version
    assert second.version.startswith("2026.09-")
    assert second_commit.startswith(second.version.rsplit("-", 1)[1])
    assert len(DistroLayout(root=home / "generic").list_versions()) == 2


def test_installed_commit_reuses_tree(home: Path, repo: Path, fake_build) -> None:
    mgr = _mgr(home, repo)
    head = _git(repo, "rev-parse", "HEAD")
    first = mgr.ensure(distro="generic")
    again = mgr.ensure(distro="generic", source=f"commit:{head}")
    assert again.version == first.version
    assert DistroLayout(root=home / "generic").list_versions() == [first.version]


def test_explicit_version_name_collision_suffixed_then_rejected(
    home: Path, repo: Path, fake_build
) -> None:
    """显式名同名不同 commit：追加短 sha 落位；短 sha 名也被占用（不同 commit）→ 报错。"""
    mgr = _mgr(home, repo)
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


def test_list_entries_and_resolve(home: Path, repo: Path, fake_build) -> None:
    mgr = _mgr(home, repo)
    entry = mgr.ensure(distro="generic")
    entries = mgr.list_entries()
    # versions（基座产物库）+ generations（代）双段枚举
    assert entry.label in [e.label for e in entries]
    gen_entry = next(e for e in entries if e.kind == "generation")
    assert gen_entry.commit == entry.commit
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
    with pytest.raises(RuntimeRefError, match="generations/ 之外"):
        mgr.resolve("generic")


# ── 切换顺序 ──────────────────────────────────────────


def _raising_gate(gen_dir, level):
    raise GateError("门禁注入失败")


def test_gate_failure_leaves_state_untouched(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    snapped: list[str] = []
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False: None)
    monkeypatch.setattr(mgr, "_gate", _raising_gate)
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: snapped.append("snap"))
    with pytest.raises(GateError):
        mgr.ensure(distro="generic")
    layout = DistroLayout(root=home / "generic")
    assert not layout.current.exists()
    assert layout.list_versions() == []  # 本次新建树被移除
    assert layout.list_generations() == []  # 本次新建代被移除
    assert snapped == []  # 未拍快照
    assert BootToml.load(home).default is None  # boot.toml 未动


def test_switch_order_gate_snapshot_repoint(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    order: list[str] = []
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False: None)
    monkeypatch.setattr(mgr, "_gate", lambda gen_dir, level: order.append("gate") or "passed")
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
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    monkeypatch.setattr(mgr, "_gate", lambda gen_dir, level: "passed")
    monkeypatch.setattr(mgr, "_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_sync", lambda distro, ver, *, force=False: None)
    first = mgr.ensure(distro="generic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")

    calls: list[tuple[str, bool]] = []

    def failing_sync(distro, ver, *, force=False):
        calls.append((ver, force))
        if not force:
            raise RuntimeError("代复核模拟失败")

    monkeypatch.setattr(mgr, "_sync", failing_sync)
    with pytest.raises(RuntimeRefError, match="已回退"):
        mgr.ensure(distro="generic", source=f"commit:{second_commit}")
    layout = DistroLayout(root=home / "generic")
    assert layout.current.resolve().name == first.version  # 重指回旧版本
    assert (first.version, True) in calls  # 旧版本被强制复核
    assert layout.list_versions() == [first.version]  # 新建树已移除
    assert BootToml.load(home).default == first.label  # boot.toml 回写


def test_first_install_no_snapshot_and_sets_last_good(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
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


# ── 锁与运行中拒绝 ─────────────────────────────────


def test_lock_blocks_concurrent_ensure(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    distro_root = home / "generic"  # home 夹具已预建（含 distro.toml）
    with acquire_distro_lock(distro_root):
        with pytest.raises(RuntimeRefError, match="锁"):
            mgr.ensure(distro="generic")


def test_stale_lock_with_dead_pid_recycled(home: Path, repo: Path, fake_build) -> None:
    mgr = _mgr(home, repo)
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
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _mgr(home, repo)
    with pytest.raises(RuntimeRefError, match="尚未安装"):
        mgr.ensure(distro="generic", update=True)


# ── gate 级别 ─────────────────────────────────────────


def test_gate_full_and_data_rejected(home: Path, repo: Path) -> None:
    mgr = _mgr(home, repo)
    for level in ("full", "data"):
        with pytest.raises(GateError, match="未启用"):
            mgr._gate(Path("/tmp"), level)
    with pytest.raises(GateError, match="未知门禁级别"):
        mgr._gate(Path("/tmp"), "weird")


# ── 身份（BOOTSTRAP-only，无 git 回退） ────────────────


def test_tree_commit_bootstrap_only(home: Path, repo: Path) -> None:
    from ghrah_bootloader.build import read_source_commit

    mgr = _mgr(home, repo)
    versions = home / "generic" / "versions"

    # 产物形态树（有 BOOTSTRAP 无 .git）→ BOOTSTRAP 值
    pkg_tree = versions / "2026.09"
    (pkg_tree / "wheels").mkdir(parents=True)
    (pkg_tree / "BOOTSTRAP.toml").write_text(
        f'[bootstrap]\nsource_commit = "{"a" * 40}"\n', encoding="utf-8"
    )
    assert read_source_commit(pkg_tree) == "a" * 40
    assert mgr._tree_commit(pkg_tree) == "a" * 40

    # git 回退已退役——worktree 形态树（无 BOOTSTRAP）不再回退取 git 值
    wt_tree = versions / "2026.09-wt"
    _git(repo, "worktree", "add", "--detach", str(wt_tree), "HEAD")
    try:
        assert read_source_commit(wt_tree) is None
        assert mgr._tree_commit(wt_tree) is None  # 无 git 回退
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt_tree))

    # 双缺目录 → None；损坏 BOOTSTRAP → None
    bare = versions / "2026.09-bare"
    bare.mkdir()
    assert mgr._tree_commit(bare) is None
    broken = versions / "2026.09-broken"
    broken.mkdir()
    (broken / "BOOTSTRAP.toml").write_text("not [ valid toml ==", encoding="utf-8")
    assert read_source_commit(broken) is None
    assert mgr._tree_commit(broken) is None


# ── Windows 平台分派（nt 模拟，mock winproc 表面） ──


def test_pid_alive_nt_dispatches_to_winproc(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nt 下 _pid_alive 分派 winproc.pid_alive（三值透传，恒真桩退役）。"""
    from ghrah_bootloader import runtime_ref
    from ghrah_bootloader import winproc as winproc_mod

    calls: list[int] = []

    def fake_pid_alive(pid: int):
        calls.append(pid)
        return None  # 探测不可用

    monkeypatch.setattr(runtime_ref.os, "name", "nt", raising=False)
    monkeypatch.setattr(winproc_mod, "pid_alive", fake_pid_alive)
    assert runtime_ref._pid_alive(4242) is None
    assert calls == [4242]
    # 恒真桩语义已退役：探测不可用不再冒充「存活」
    monkeypatch.setattr(winproc_mod, "pid_alive", lambda pid: False)
    assert runtime_ref._pid_alive(4242) is False
    monkeypatch.setattr(winproc_mod, "pid_alive", lambda pid: True)
    assert runtime_ref._pid_alive(4242) is True
    # pid<=0 提前返回（不触 winproc）
    calls.clear()
    assert runtime_ref._pid_alive(0) is False
    assert runtime_ref._pid_alive(-1) is False
    assert calls == []


def test_lock_is_stale_three_valued(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """锁陈旧判定三值适配：None 不判死（时间兜底仍在），False 判死。"""
    import time as time_mod

    from ghrah_bootloader import runtime_ref

    lock = home / ".bootloader.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(runtime_ref, "_pid_alive", lambda pid: None)
    # None + 新鲜 → 不陈旧（探测失败不应误回收锁）
    assert runtime_ref._lock_is_stale(lock, stale_after=600.0) is False  # None+新鲜
    # None + 超时 → 陈旧（600s 时间兜底）
    old = time_mod.localtime(time_mod.time() - 7200)
    old_ts = time_mod.mktime(old)
    import os as _os

    _os.utime(lock, (old_ts, old_ts))
    assert runtime_ref._lock_is_stale(lock, stale_after=600.0) is True
    # False（pid 死）→ 陈旧
    monkeypatch.setattr(runtime_ref, "_pid_alive", lambda pid: False)
    _os.utime(lock, (time_mod.time(), time_mod.time()))
    assert runtime_ref._lock_is_stale(lock, stale_after=600.0) is True


def test_repoint_nt_dispatches_to_winproc(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nt 下 _repoint_link 分派 winproc.repoint_dir_link（段固定 generations）。"""
    from ghrah_bootloader import runtime_ref
    from ghrah_bootloader import winproc as winproc_mod

    distro_root = home / "generic"
    gen_dir = distro_root / "generations" / "2026.09"
    gen_dir.mkdir(parents=True)
    calls: list[tuple[Path, Path]] = []

    def fake_repoint(link: Path, target_abs: Path) -> str:
        calls.append((link, target_abs))
        return "junction"

    monkeypatch.setattr(runtime_ref.os, "name", "nt", raising=False)
    monkeypatch.setattr(winproc_mod, "repoint_dir_link", fake_repoint)
    runtime_ref.repoint_current(distro_root, "2026.09")
    assert calls == [(distro_root / "current", gen_dir.resolve())]
    # last-good 同分派
    runtime_ref.repoint_last_good(distro_root, "2026.09")
    assert calls[-1] == (distro_root / "last-good", gen_dir.resolve())


def test_repoint_nt_maps_winproc_error(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """winproc 失败 → RuntimeRefError（fail-closed 映射，含策略上下文）。"""
    from ghrah_bootloader import runtime_ref
    from ghrah_bootloader import winproc as winproc_mod
    from ghrah_bootloader.errors import RuntimeRefError, WinProcError

    distro_root = home / "generic"
    (distro_root / "generations" / "2026.09").mkdir(parents=True)
    monkeypatch.setattr(runtime_ref.os, "name", "nt", raising=False)

    def bad_repoint(link: Path, target_abs: Path) -> str:
        raise WinProcError("重指复核不一致")

    monkeypatch.setattr(winproc_mod, "repoint_dir_link", bad_repoint)
    with pytest.raises(RuntimeRefError, match="重指 current 失败"):
        runtime_ref.repoint_current(distro_root, "2026.09")


def test_repoint_posix_fast_path_unchanged(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """posix 快路径：仍走 tmp symlink + os.replace（零改动回归位）。"""
    from ghrah_bootloader import runtime_ref
    from ghrah_bootloader import winproc as winproc_mod

    distro_root = home / "generic"
    gen = distro_root / "generations" / "2026.09"
    gen.mkdir(parents=True)
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        winproc_mod,
        "repoint_dir_link",
        lambda lnk, tgt: calls.append((lnk, tgt)) or "junction",
    )
    runtime_ref.repoint_current(distro_root, "2026.09")
    assert calls == []  # posix 不触 winproc
    link = distro_root / "current"
    assert link.is_symlink()
    assert link.resolve() == gen
    _assert_symlink_form(link, "generations/2026.09")


def _assert_symlink_form(link: Path, expected: str) -> None:
    assert os.readlink(link) == expected
