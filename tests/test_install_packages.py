# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""packages 安装模式测试（B2：产物树物化 / 复用 / 门禁 / no-op sync / 混域 fail-closed）。

不跑真实构建：monkeypatch ``build_artifacts``/``verify_artifacts`` 为假实现，
物化最小产物树骨架（BOOTSTRAP.toml + manifest.json）；git 子进程真实
（候选解析 / 版本命名走真实仓）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ghrah_bootloader.errors import GateError, RuntimeRefError
from ghrah_bootloader.layout import DistroLayout
from ghrah_bootloader.runtime_ref import RuntimeRefManager


@pytest.fixture(autouse=True)
def _no_running_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ghrah_bootloader.runtime_ref.port_in_use", lambda host, port: False)


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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    return h


def _materialize_fake_artifacts(tree: Path, commit: str) -> None:
    """物化最小产物树骨架（身份面 + 清单面）。"""
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "wheels").mkdir(exist_ok=True)
    (tree / "wheels" / "pins.txt").write_text("third-party-pins\n", encoding="utf-8")
    (tree / "BOOTSTRAP.toml").write_text(
        f'[bootstrap]\nsource_commit = "{commit}"\nbuilt_at = "2026-09-25T00:00:00Z"\n',
        encoding="utf-8",
    )
    (tree / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch, repo: Path):
    """假构建/验证：记录调用、物化骨架产物树；可注入失败。"""
    calls: dict[str, list] = {"build": [], "verify": []}
    fail: dict[str, Exception | None] = {"build": None, "verify": None}

    def _build(repo_path, commit, *, out_dir):
        calls["build"].append((commit, Path(out_dir)))
        if fail["build"] is not None:
            raise fail["build"]
        _materialize_fake_artifacts(Path(out_dir), commit)

    def _verify(out_dir):
        calls["verify"].append(Path(out_dir))
        if fail["verify"] is not None:
            raise fail["verify"]

    import ghrah_bootloader.runtime_ref as rr

    monkeypatch.setattr(rr, "build_artifacts", _build)
    monkeypatch.setattr(rr, "verify_artifacts", _verify)
    return type("FakeBuild", (), {"calls": calls, "fail": fail})()


def _mgr(home: Path, repo: Path) -> RuntimeRefManager:
    return RuntimeRefManager(root=home, repo=repo)


def _write_packages_distro(home: Path, repo: Path) -> None:
    distro_root = home / "generic"
    distro_root.mkdir(parents=True, exist_ok=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "packages"

[distro.repo]
path = "{repo}"
""",
        encoding="utf-8",
    )


def _guard_no_uv_pnpm(mgr: RuntimeRefManager, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """拦截并记录 uv/pnpm 子进程调用（git 调用透传真实 _run，候选解析不受扰）。"""
    seen: list[list[str]] = []
    real_run = mgr._run

    def guarded_run(cmd, cwd=None, env=None, check=True):
        first = str(cmd[0])
        if first in ("uv", "pnpm"):
            seen.append([str(c) for c in cmd])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, cwd=cwd, env=env, check=check)

    monkeypatch.setattr(mgr, "_run", guarded_run)
    return seen


# ── P1 全链路 ────────────────────────────────────────────────


def test_p1_packages_install_full_chain(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    head = _git(repo, "rev-parse", "HEAD")

    entry = mgr.ensure(distro="generic", gate="basic")

    assert entry.version == "2026.09"
    assert entry.commit == head
    # build_artifacts 以 out_dir=versions/<ver> 被调（物化即构建，D1）
    assert fake_build.calls["build"] == [(head, home / "generic" / "versions" / "2026.09")]
    # 门禁 = verify_artifacts（D3）
    assert fake_build.calls["verify"] == [home / "generic" / "versions" / "2026.09"]
    # 产物树形态：BOOTSTRAP 在、.git 不在
    tree = home / "generic" / "versions" / "2026.09"
    assert (tree / "BOOTSTRAP.toml").is_file()
    assert not (tree / ".git").exists()
    # current 重指 + boot.toml 写入
    assert (home / "generic" / "current").resolve().name == "2026.09"
    from ghrah_bootloader.layout import BootToml

    boot = BootToml.load(home)
    assert boot.default == "generic/2026.09"
    assert boot.last_good == "generic/2026.09"


# ── P2 同 commit 复用 ────────────────────────────────────────


def test_p2_same_commit_reuses_without_rebuild(home: Path, repo: Path, fake_build) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic", gate="basic")
    bootstrap = home / "generic" / "versions" / first.version / "BOOTSTRAP.toml"
    mtime = bootstrap.stat().st_mtime_ns

    second = mgr.ensure(distro="generic", gate="basic")

    assert second.version == first.version
    assert len(fake_build.calls["build"]) == 1  # 复用判定命中，零重建
    assert bootstrap.stat().st_mtime_ns == mtime
    assert DistroLayout(root=home / "generic").list_versions() == [first.version]


# ── P3 构建失败 = 什么都没发生 ────────────────────────────────


def test_p3_build_failure_leaves_state_untouched(home: Path, repo: Path, fake_build) -> None:
    from ghrah_bootloader.errors import BuildError

    _write_packages_distro(home, repo)
    fake_build.fail["build"] = BuildError("模拟构建失败")
    mgr = _mgr(home, repo)

    with pytest.raises(RuntimeRefError, match="建版本树失败"):
        mgr.ensure(distro="generic", gate="basic")

    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == []  # 无残留（staging 语义 + 树未建）
    assert not layout.current.exists()
    from ghrah_bootloader.layout import BootToml

    assert BootToml.load(home).default is None


# ── P4 门禁失败清理（且零 worktree 子命令） ──────────────────


def test_p4_gate_failure_cleans_tree_without_worktree_cmds(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_packages_distro(home, repo)
    fake_build.fail["verify"] = GateError("产物验证模拟失败")
    mgr = _mgr(home, repo)
    uv_pnpm = _guard_no_uv_pnpm(mgr, monkeypatch)
    # 同时记录 git 调用（断言零 worktree 子命令）
    git_cmds: list[list[str]] = []
    real_run = mgr._run

    def recording_run(cmd, cwd=None, env=None, check=True):
        if str(cmd[0]) == "git":
            git_cmds.append([str(c) for c in cmd])
        return real_run(cmd, cwd=cwd, env=env, check=check)

    monkeypatch.setattr(mgr, "_run", recording_run)

    # 首装路径：gate 失败 → GateError 冒泡 + 新建产物树 rmtree（D7）
    with pytest.raises(GateError):
        mgr.ensure(distro="generic", gate="basic")

    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == []
    assert not layout.current.exists()
    assert uv_pnpm == []
    # 全程无 worktree 子命令（packages 分支零 git 建树副作用）
    assert all("worktree" not in c for c in git_cmds)


# ── P5 sync no-op ───────────────────────────────────────────


def test_p5_packages_sync_is_noop(home: Path, repo: Path, fake_build, monkeypatch) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    uv_pnpm = _guard_no_uv_pnpm(mgr, monkeypatch)

    mgr.ensure(distro="generic", gate="basic")

    assert uv_pnpm == []  # D4：无 uv/pnpm 子进程
    assert not (home / "generic" / "env").exists()  # env 语义不物化（B3 域）


# ── P6 混域 fail-closed ─────────────────────────────────────


def test_p6_mixed_domain_fails_closed(home: Path, repo: Path, fake_build) -> None:
    """混域 fail-closed：旧 worktree 残树不构成 packages 复用依据（D6）。

    基础名被异形残树占用 → 追加 ``-<short7>`` 建新产物树（不复用不覆盖）；
    连短 sha 名也被异形残树占用（不同 commit 无法判定）→ 报「版本名冲突」。
    """
    _write_packages_distro(home, repo)
    head = _git(repo, "rev-parse", "HEAD")
    short = _git(repo, "rev-parse", "--short=7", head)
    # 预置 worktree 形态残树（有 .git 无 BOOTSTRAP）占住基础名与短 sha 名
    for name in ("2026.09", f"2026.09-{short}"):
        stray = home / "generic" / "versions" / name
        stray.mkdir(parents=True)
        (stray / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    mgr = _mgr(home, repo)

    with pytest.raises(RuntimeRefError, match="版本名冲突"):
        mgr.ensure(distro="generic", gate="basic")

    assert fake_build.calls["build"] == []  # 未被静默复用/未误建
    # 显示面（双跳）仍枚举残树（迁移期可见），但 commit 为 None（无 git 可读）
    entries = mgr.list_entries()
    assert sorted(e.version for e in entries) == ["2026.09", f"2026.09-{short}"]


# ── P7 回滚 no-op（不触发 uv/pnpm） ─────────────────────────


def test_p7_rollback_without_sync(home: Path, repo: Path, fake_build, monkeypatch) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic", gate="basic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")
    uv_pnpm = _guard_no_uv_pnpm(mgr, monkeypatch)

    def failing_write_boot(boot_before, old, new):
        raise RuntimeError("boot.toml 写入模拟失败")

    monkeypatch.setattr(mgr, "_write_boot_toml", failing_write_boot)
    with pytest.raises(RuntimeRefError, match="已回退"):
        mgr.ensure(distro="generic", source=f"commit:{second_commit}")

    layout = DistroLayout(root=home / "generic")
    assert layout.current.resolve().name == first.version  # 重指回旧版本
    assert uv_pnpm == []  # 回滚 force sync 亦 no-op（D4）
    assert first.version in layout.list_versions()  # 旧产物树保留
    assert len(layout.list_versions()) == 1  # 新建树已清理


# ── P8 枚举面 ───────────────────────────────────────────────


def test_p8_enumeration_shows_commit_and_mode(
    home: Path, repo: Path, fake_build, capsys: pytest.CaptureFixture
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    head = _git(repo, "rev-parse", "HEAD")
    entry = mgr.ensure(distro="generic", gate="basic")

    entries = mgr.list_entries()
    assert [e.label for e in entries] == [entry.label]
    assert entries[0].commit == head  # 显示面身份 = BOOTSTRAP source_commit
    assert mgr.resolve("generic").label == entry.label

    from ghrah_bootloader.cli import main

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "install_mode=packages" in out
    assert head[:7] in out
    assert main(["runtime", "status", "--distro", "generic"]) == 0
    assert "install_mode = packages" in capsys.readouterr().out
