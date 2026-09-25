# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""packages 安装模式测试（B2/B3：产物树物化 / 组合代物化 / 复用 / 门禁在代 / 回滚）。

不跑真实构建与装配：monkeypatch ``build_artifacts``/``materialize_generation``
为假实现，物化最小骨架（BOOTSTRAP/manifest + GENERATION/.deps-fingerprint/
env 骨架）；git 子进程真实（候选解析 / 版本命名走真实仓）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ghrah_bootloader.errors import GateError, RuntimeRefError
from ghrah_bootloader.layout import BootToml, DistroLayout
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
        py.chmod(0o755)  # 代冒烟会真实拉起解释器探针
    (gen_dir / "env" / "node" / "node_modules").mkdir(parents=True, exist_ok=True)
    (gen_dir / GENERATION_TOML).write_text(
        render_generation_toml(spec, built_at="2026-09-25T00:00:00Z"), encoding="utf-8"
    )
    (gen_dir / FINGERPRINT_NAME).write_text(generation_fingerprint(spec) + "\n", encoding="utf-8")


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch, repo: Path):
    """假构建/物化/门禁：记录调用、物化骨架；可注入失败。

    ``_gate_generation`` 直通（代冒烟语义由 P4 注入 GateError 单独锁定；
    真实装配冒烟属 integration I-P1）。
    """
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
        if first in ("uv", "pnpm", "cmd"):
            seen.append([str(c) for c in cmd])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, cwd=cwd, env=env, check=check)

    monkeypatch.setattr(mgr, "_run", guarded_run)
    return seen


def _gen_entry_dir(home: Path, gen: str) -> Path:
    return home / "generic" / "generations" / gen


# ── P1 全链路（基座 + 代四段） ────────────────────────────────


def test_p1_packages_install_full_chain(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    head = _git(repo, "rev-parse", "HEAD")

    entry = mgr.ensure(distro="generic", gate="basic")

    assert entry.version == "2026.09"  # 无插件 gen_id = 基座版本名（D11）
    assert entry.commit == head
    assert entry.kind == "generation"
    # build_artifacts 以 out_dir=versions/<ver> 被调（物化即构建，B2 D1）
    assert fake_build.calls["build"] == [(head, home / "generic" / "versions" / "2026.09")]
    # 代物化以基座树为输入（B3 四段）
    assert fake_build.calls["materialize"] == [
        (home / "generic" / "versions" / "2026.09", "2026.09", False)
    ]
    # 产物树形态：BOOTSTRAP 在、.git 不在
    tree = home / "generic" / "versions" / "2026.09"
    assert (tree / "BOOTSTRAP.toml").is_file()
    assert not (tree / ".git").exists()
    # 代形态：GENERATION + 指纹 + env 骨架；current → generations/<gen>
    gen = _gen_entry_dir(home, "2026.09")
    assert (gen / "GENERATION.toml").is_file()
    assert (gen / ".deps-fingerprint").is_file()
    assert (home / "generic" / "current").resolve() == gen.resolve()
    boot = BootToml.load(home)
    assert boot.default == "generic/2026.09"
    assert boot.last_good == "generic/2026.09"
    assert (home / "generic" / "last-good").resolve() == gen.resolve()


# ── P2 双层复用（基座零重建 + 代零物化） ──────────────────────


def test_p2_same_commit_reuses_without_rebuild(home: Path, repo: Path, fake_build) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic", gate="basic")
    bootstrap = home / "generic" / "versions" / first.version / "BOOTSTRAP.toml"
    mtime = bootstrap.stat().st_mtime_ns
    fp = _gen_entry_dir(home, first.version) / ".deps-fingerprint"
    fp_mtime = fp.stat().st_mtime_ns

    second = mgr.ensure(distro="generic", gate="basic")

    assert second.version == first.version
    assert len(fake_build.calls["build"]) == 1  # 基座复用判定命中，零重建
    assert len(fake_build.calls["materialize"]) == 1  # 代复用命中，零物化
    assert bootstrap.stat().st_mtime_ns == mtime
    assert fp.stat().st_mtime_ns == fp_mtime
    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == [first.version]
    assert layout.list_generations() == [first.version]


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
    assert layout.list_generations() == []
    assert not layout.current.exists()
    assert BootToml.load(home).default is None


# ── P4 门禁失败清理（新建代与新建基座同删；零 worktree 子命令） ─


def test_p4_gate_failure_cleans_tree_and_generation(
    home: Path, repo: Path, fake_build, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    uv_pnpm = _guard_no_uv_pnpm(mgr, monkeypatch)
    # 记录 git 调用（断言零 worktree 子命令）
    git_cmds: list[list[str]] = []
    real_run = mgr._run

    def recording_run(cmd, cwd=None, env=None, check=True):
        if str(cmd[0]) == "git":
            git_cmds.append([str(c) for c in cmd])
        return real_run(cmd, cwd=cwd, env=env, check=check)

    monkeypatch.setattr(mgr, "_run", recording_run)

    def raising_gate(self_or_dir, gen_dir=None):
        if fake_build.fail["gate"] is not None:
            raise fake_build.fail["gate"]

    fake_build.fail["gate"] = GateError("代冒烟模拟失败")
    monkeypatch.setattr(RuntimeRefManager, "_gate_generation", raising_gate)

    # 首装路径：gate 失败 → GateError 冒泡 + 新建代与新建基座同删（D10）
    with pytest.raises(GateError):
        mgr.ensure(distro="generic", gate="basic")

    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == []
    assert layout.list_generations() == []
    assert not layout.current.exists()
    assert uv_pnpm == []
    assert all("worktree" not in c for c in git_cmds)


# ── P5 uv/pnpm 只经物化层发生 ────────────────────────────────


def test_p5_uv_pnpm_only_via_materialize(home: Path, repo: Path, fake_build, monkeypatch) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    uv_pnpm = _guard_no_uv_pnpm(mgr, monkeypatch)

    mgr.ensure(distro="generic", gate="basic")

    # 物化被 fake 层拦截（不落 _run）；绕过物化层的裸 uv/pnpm 调用不存在
    assert uv_pnpm == []
    assert len(fake_build.calls["materialize"]) == 1
    # 发行版级 env/ 不物化（per-gen env 属代内；worktree 语义 B4 前不动）
    assert not (home / "generic" / "env").exists()


# ── P6 混域 fail-closed ──────────────────────────────────────


def test_p6_mixed_domain_fails_closed(home: Path, repo: Path, fake_build) -> None:
    """混域 fail-closed：旧 worktree 残树不构成 packages 复用依据（B2 D6）。

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
    assert sorted(e.version for e in entries if e.kind == "version") == [
        "2026.09",
        f"2026.09-{short}",
    ]


# ── P7 回滚（代复核零操作路径） ──────────────────────────────


def test_p7_rollback_repoints_old_generation(
    home: Path, repo: Path, fake_build, monkeypatch
) -> None:
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
    # 重指回旧代（current → generations/<旧gen>）
    assert layout.current.resolve() == _gen_entry_dir(home, first.version).resolve()
    assert uv_pnpm == []  # 回滚 = 代复核（指纹一致零操作，D6）
    assert first.version in layout.list_versions()  # 旧基座树保留
    assert len(layout.list_versions()) == 1  # 新建基座树已清理
    assert layout.list_generations() == [first.version]  # 新建代已清理


# ── P8 枚举面 ────────────────────────────────────────────────


def test_p8_enumeration_shows_generation_and_mode(
    home: Path, repo: Path, fake_build, capsys: pytest.CaptureFixture
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    head = _git(repo, "rev-parse", "HEAD")
    entry = mgr.ensure(distro="generic", gate="basic")

    entries = mgr.list_entries()
    versions = [e for e in entries if e.kind == "version"]
    gens = [e for e in entries if e.kind == "generation"]
    assert [e.label for e in versions] == [f"generic/{entry.version}"]
    assert versions[0].commit == head  # 显示面身份 = BOOTSTRAP source_commit
    assert [g.label for g in gens] == [entry.label]
    assert gens[0].base_version == entry.version
    assert "base=2026.09" in gens[0].detail
    assert mgr.resolve("generic").label == entry.label
    assert mgr.resolve("generic").kind == "generation"

    from ghrah_bootloader.cli import main

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "install_mode=packages" in out
    assert "generations=['2026.09']" in out
    assert head[:7] in out
    assert main(["runtime", "status", "--distro", "generic"]) == 0
    status_out = capsys.readouterr().out
    assert "install_mode = packages" in status_out
    assert "generations = " in status_out
    assert "base=2026.09" in status_out
    assert main(["distro", "list"]) == 0
    list_out = capsys.readouterr().out
    assert "generations: 2026.09（base=2026.09 plugins=[]）" in list_out


# ── P9 代物化失败 = 什么都没发生 ──────────────────────────────


def test_p9_generation_failure_cleans_new_base(home: Path, repo: Path, fake_build) -> None:
    from ghrah_bootloader.errors import MaterializeError

    _write_packages_distro(home, repo)
    fake_build.fail["materialize"] = MaterializeError("代物化模拟失败")
    mgr = _mgr(home, repo)

    with pytest.raises(MaterializeError):
        mgr.ensure(distro="generic", gate="basic")

    layout = DistroLayout(root=home / "generic")
    assert layout.list_versions() == []  # 新建基座树同删（D10）
    assert layout.list_generations() == []
    assert not layout.current.exists()


def test_p9b_generation_failure_keeps_reused_base(home: Path, repo: Path, fake_build) -> None:
    """基座已复用时失败：基座保留（既有产物库不可变），仅本次无代。"""
    from ghrah_bootloader.errors import MaterializeError

    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    mgr.ensure(distro="generic", gate="basic")
    # 破坏代使其不完整 → 下次 ensure 复用基座 + 重建代路径 → 注入物化失败
    shutil.rmtree(_gen_entry_dir(home, "2026.09"))
    fake_build.fail["materialize"] = MaterializeError("代重建模拟失败")
    with pytest.raises(MaterializeError):
        mgr.ensure(distro="generic", gate="basic")
    assert "2026.09" in DistroLayout(root=home / "generic").list_versions()  # 基座保留


# ── P10 切换失败回滚 → 重指旧代 ──────────────────────────────


def test_p10_switch_failure_rolls_back_to_old_generation(
    home: Path, repo: Path, fake_build, monkeypatch
) -> None:
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic", gate="basic")
    second_commit = _commit(repo, "b", "2026-09-20T10:00:00 +0000")

    real_repoint = __import__(
        "ghrah_bootloader.runtime_ref", fromlist=["repoint_current"]
    ).repoint_current
    state = {"failed": False}

    def failing_repoint_after_first(distro_root, ver, *, segment=None):
        if not state["failed"] and ver != first.version:
            # 让「写 boot.toml」失败更直接：改用 _write_boot_toml 注入
            pass
        return real_repoint(distro_root, ver, segment=segment)

    monkeypatch.setattr("ghrah_bootloader.runtime_ref.repoint_current", failing_repoint_after_first)

    def failing_write_boot(boot_before, old, new):
        raise RuntimeError("boot.toml 写入模拟失败")

    monkeypatch.setattr(mgr, "_write_boot_toml", failing_write_boot)
    with pytest.raises(RuntimeRefError, match="已回退"):
        mgr.ensure(distro="generic", source=f"commit:{second_commit}")

    layout = DistroLayout(root=home / "generic")
    assert layout.current.resolve() == _gen_entry_dir(home, first.version).resolve()
    assert len(layout.list_generations()) == 1  # 新建代清理，旧代保留


# ── P11 基座/代双层复用分离 ──────────────────────────────────


def test_p11_deleted_generation_rebuilt_base_reused(home: Path, repo: Path, fake_build) -> None:
    """删代后重装：基座复用（零构建）+ 代重建（物化重调）。"""
    _write_packages_distro(home, repo)
    mgr = _mgr(home, repo)
    first = mgr.ensure(distro="generic", gate="basic")
    assert len(fake_build.calls["build"]) == 1
    assert len(fake_build.calls["materialize"]) == 1

    shutil.rmtree(_gen_entry_dir(home, first.version))
    second = mgr.ensure(distro="generic", gate="basic")

    assert second.version == first.version
    assert len(fake_build.calls["build"]) == 1  # 基座零重建
    assert len(fake_build.calls["materialize"]) == 2  # 代重建
    assert _gen_entry_dir(home, first.version).is_dir()
