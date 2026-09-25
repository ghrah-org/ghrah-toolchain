# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""真实 uv sync / pnpm install 集成用例（@integration，默认跳过）。

动机：批次 2+3+4 首装真实验收暴露 `.pth` realpath 断言缺陷（单测 mock `_run`
不可见，见任务书 §九）——本文件以真实 monorepo + 真实工具链锁定：

1. install 全链路（建树 → 门禁 → sync → 自管 .pth → 重指 current）；
2. ``import ghrah.core`` 的导入源经 ``current`` 段（与重指切换语义一致）；
3. 依赖指纹一致时二次 install 同 commit 纯复用（不重建 env）；
4. pnpm 依赖物化（node_modules + vite 可执行探针）。

运行：``uv run pytest -q -m integration``（需 uv / pnpm / git 与 monorepo）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ghrah_bootloader.runtime_ref import RuntimeRefManager

pytestmark = pytest.mark.integration

_SRC = Path(__file__).resolve().parent.parent / "src"

_MONOREPO_DEFAULT = Path(__file__).resolve().parent.parent.parent / "ghrah-monorepo"


def _monorepo() -> Path:
    repo = Path(os.environ.get("GHRAH_MONOREPO_ROOT", str(_MONOREPO_DEFAULT)))
    if not (repo / "ghrah-core" / "pyproject.toml").is_file():
        pytest.skip(f"monorepo 不可用: {repo}")
    return repo


def _require(*bins: str) -> None:
    for b in bins:
        if shutil.which(b) is None:
            pytest.skip(f"环境缺少 {b}，跳过真实集成用例")


@pytest.fixture
def repo() -> Path:
    return _monorepo()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    # 钉死环境：防止外层 env 泄漏影响 uv/pnpm 解析
    monkeypatch.delenv("GHRAH_MONOREPO_ROOT", raising=False)
    h.mkdir(parents=True)
    # 本文件既有用例锁定 worktree 链路（回归面）：预写显式模式
    distro_root = h / "generic"
    distro_root.mkdir(parents=True, exist_ok=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "worktree"

[distro.repo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    yield h
    # install 用例在 h 下真实 `git worktree add`——pytest 清 tmp 只删目录，
    # 不删源仓 .git/worktrees/ 元数据（残留会累积成僵尸记录，旧 tmp 未过期
    # 时连 prune 都无法回收）。teardown 显式 remove + prune。
    for wt in sorted(h.glob("*/versions/*")):
        if (wt / ".git").is_file():  # worktree 标记文件（非主仓 .git 目录）
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
                check=False,
            )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def home_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> Path:
    """packages 模式安装域（B2 I-P1：真实构建产物树）。"""
    h = tmp_path / "ghrah-home-packages"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    monkeypatch.delenv("GHRAH_MONOREPO_ROOT", raising=False)
    distro_root = h / "generic"
    distro_root.mkdir(parents=True)
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
install_mode = "packages"

[distro.repo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    return h


def self_commit(repo: Path) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout.strip()


def _venv_python(home: Path, distro: str) -> Path:
    bin_py = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    py = home / distro / "env" / ".venv" / bin_py
    assert py.is_file(), f"venv 解释器缺失: {py}"
    return py


def _import_source_assert(py: Path, home: Path, distro: str) -> None:
    """断言导入源经 current 段（对齐 runtime_ref._sync 的真实断言口径）。

    __file__ 会被 realpath 解析成 versions/<ver>/...，故校验 sys.path 中
    的字面 current 段且 __file__ 与之逐段一致（真实验收修正后的口径）。
    """
    code = (
        "import sys, ghrah.core; "
        "hits = [p for p in sys.path if 'current' in p and 'ghrah-core' in p]; "
        "assert hits, f'no current segment in sys.path: {sys.path}'; "
        "src = hits[0] + '/ghrah/core/__init__.py'; "
        "import os; assert os.path.exists(src), f'missing {src}'; "
        "real = os.path.realpath(ghrah.core.__file__); "
        "assert real == os.path.realpath(src) or ghrah.core.__file__ == src, "
        "f'__file__ {ghrah.core.__file__} != current-segment source {src}'; "
        "print(ghrah.core.__file__)"
    )
    cp = subprocess.run([str(py), "-c", code], capture_output=True, text=True, check=True)
    out = cp.stdout.strip()
    # sys.path 字面 current 段存在；__file__ 或其 realpath 落 current/versions 树
    assert "current" in out or "versions" in out, out


class TestInstallSyncPth:
    def test_install_creates_tree_env_and_pth(self, home: Path, repo: Path) -> None:
        _require("uv", "pnpm")
        mgr = RuntimeRefManager(root=home, repo=repo)
        entry = mgr.ensure(distro="generic", gate="basic")
        assert entry.path.is_dir()
        assert (home / "generic" / "current").is_symlink()

        py = _venv_python(home, "generic")
        _import_source_assert(py, home, "generic")

    def test_reinstall_same_commit_reuses_tree(self, home: Path, repo: Path) -> None:
        _require("uv", "pnpm")
        mgr = RuntimeRefManager(root=home, repo=repo)
        first = mgr.ensure(distro="generic", gate="basic")
        venv_mtime = (home / "generic" / "env" / ".venv" / "pyvenv.cfg").stat().st_mtime_ns
        second = mgr.ensure(distro="generic", gate="basic")
        assert first.path == second.path
        assert second.commit == first.commit
        # commit 已装复用：env 未被重建
        assert (home / "generic" / "env" / ".venv" / "pyvenv.cfg").stat().st_mtime_ns == venv_mtime

    def test_two_commits_repoint_without_sync(self, home: Path, repo: Path) -> None:
        """切换 = 纯重指：依赖指纹一致时重指 current 后即导入新版本源。"""
        _require("uv", "pnpm")
        cp = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
            capture_output=True,
            text=True,
            check=True,
        )
        prev_commit = cp.stdout.strip()

        mgr = RuntimeRefManager(root=home, repo=repo)
        new_entry = mgr.ensure(distro="generic", gate="basic")
        old_entry = mgr.ensure(distro="generic", source=f"commit:{prev_commit}", gate="basic")
        assert old_entry.path != new_entry.path
        # current 现指向旧版本，导入源应经 current 段命中旧树
        current = (home / "generic" / "current").resolve()
        assert current == old_entry.path.resolve()
        py = _venv_python(home, "generic")
        _import_source_assert(py, home, "generic")


class TestPnpmInstall:
    def test_node_modules_materialized_and_vite_available(self, home: Path, repo: Path) -> None:
        _require("uv", "pnpm")
        mgr = RuntimeRefManager(root=home, repo=repo)
        entry = mgr.ensure(distro="generic", gate="basic")

        webui = entry.path / "ghrah-observer-webui" / "packages" / "observer-web"
        node_modules = webui / "node_modules"
        assert node_modules.is_dir(), "pnpm install 未物化 node_modules"
        # workspace 根 node_modules（pnpm 虚拟 store 链）也应存在
        assert (entry.path / "node_modules").is_dir() or node_modules.is_dir()

        vite = node_modules / ".bin" / ("vite.cmd" if os.name == "nt" else "vite")
        assert vite.exists(), f"vite 可执行缺失: {vite}"

    def test_observer_web_build_config_present(self, home: Path, repo: Path) -> None:
        """构建配置在树内（pnpm install 后 type-check/build 可跑的前提）。"""
        _require("uv", "pnpm")
        mgr = RuntimeRefManager(root=home, repo=repo)
        entry = mgr.ensure(distro="generic", gate="basic")
        webui = entry.path / "ghrah-observer-webui" / "packages" / "observer-web"
        assert (webui / "package.json").is_file()
        assert (webui / "vite.config.ts").is_file()


class TestInstallPackagesMode:
    """B2/B3 I-P1：packages 模式真实构建物化 + 代物化 + 双层复用（真实 monorepo，分钟级）。"""

    def test_install_materializes_artifact_tree_and_reuses(
        self, home_packages: Path, repo: Path
    ) -> None:
        _require("uv", "pnpm", "git")
        mgr = RuntimeRefManager(root=home_packages, repo=repo)
        entry = mgr.ensure(distro="generic", gate="basic", source=f"commit:{self_commit(repo)}")

        # 基座产物树（entry.path 是代目录；无插件 gen_id = 基座版本名 → 同名异位）
        base_tree = home_packages / "generic" / "versions" / entry.version
        tree = base_tree
        # 产物树形态：wheels 五包 + tarballs 三包 + observer-web + manifest + BOOTSTRAP
        assert not (tree / ".git").exists(), "产物树不得含 .git（安装域自包含）"
        wheels = sorted(p.name for p in (tree / "wheels").glob("*.whl"))
        assert len(wheels) == 5, f"应有五个 wheel: {wheels}"
        assert (tree / "wheels" / "pins.txt").is_file()
        tarballs = sorted(p.name for p in (tree / "tarballs").glob("*.tgz"))
        assert len(tarballs) == 3, f"应有三个 tarball: {tarballs}"
        assert (tree / "observer-web" / "package.json").is_file()
        assert (tree / "manifest.json").is_file()
        bootstrap_commit = (tree / "BOOTSTRAP.toml").read_text(encoding="utf-8")
        assert entry.commit in bootstrap_commit

        # B3：组合代物化（无插件 gen_id = 基座版本名；entry.path 即代目录）
        from ghrah_bootloader.materialize import FINGERPRINT_NAME, read_generation

        assert entry.kind == "generation"
        gen = home_packages / "generic" / "generations" / entry.version
        assert entry.path == gen
        info = read_generation(gen)
        assert info is not None and info.base_commit == entry.commit
        assert (gen / FINGERPRINT_NAME).is_file()
        # current / last-good → generations/<gen>
        assert (home_packages / "generic" / "current").resolve() == gen.resolve()
        assert (home_packages / "generic" / "last-good").resolve() == gen.resolve()

        # 代 env 冒烟：导入源是 gen env 内真实包（不经 current 段——.pth 语义已退役）
        py = gen / "env" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cp = subprocess.run(
            [str(py), "-c", "import ghrah.core; print(ghrah.core.__file__)"],
            capture_output=True,
            text=True,
            check=True,
        )
        src = cp.stdout.strip()
        assert "site-packages" in src and str(gen) in src, (
            f"导入源应落代 env 内（wheel 平铺）: {src}"
        )
        assert "current" not in src and "versions" not in src
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        suffix = ".exe" if os.name == "nt" else ""
        assert (gen / "env" / ".venv" / bin_dir / f"ghrah-subject-server{suffix}").is_file()

        # webui：env/node 物化 + @ghrah/* 经 file: 可解析
        node = gen / "env" / "node"
        assert (node / "node_modules").is_dir()
        lock = node / "pnpm-lock.yaml"
        assert lock.is_file(), "pnpm install 未固化 lock（同代复用零解析的前提）"

        # 枚举面：代条目 commit = GENERATION base_commit
        entries = mgr.list_entries()
        gens = [e for e in entries if e.kind == "generation"]
        assert [e.commit for e in gens] == [entry.commit]

        # 同 commit 二次 install：基座 + 代双层复用（零重建零物化）
        fp = gen / FINGERPRINT_NAME
        fp_mtime = fp.stat().st_mtime_ns
        second = mgr.ensure(distro="generic", gate="basic", source=f"commit:{entry.commit}")
        assert second.version == entry.version
        assert second.path == entry.path
        assert fp.stat().st_mtime_ns == fp_mtime
        from ghrah_bootloader.layout import DistroLayout

        layout = DistroLayout(root=home_packages / "generic")
        assert layout.list_versions() == [entry.version]
        assert layout.list_generations() == [entry.version]

    def test_ip2_switch_between_commits_generation_lifecycle(
        self, home_packages: Path, repo: Path
    ) -> None:
        """I-P2：A → B → A 三次 install；第三次基座 + 代双层复用；current 终值 = A 代。"""
        _require("uv", "pnpm", "git")
        new_commit = self_commit(repo)
        prev = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        mgr = RuntimeRefManager(root=home_packages, repo=repo)
        a = mgr.ensure(distro="generic", gate="basic", source=f"commit:{prev}")
        b = mgr.ensure(distro="generic", gate="basic", source=f"commit:{new_commit}")
        assert a.version != b.version, "双 commit 应落不同版本名（同月短 sha 后缀）"
        assert (home_packages / "generic" / "generations" / b.version).is_dir()

        again = mgr.ensure(distro="generic", gate="basic", source=f"commit:{prev}")
        assert again.version == a.version
        assert again.path == a.path  # A 代复用
        current = (home_packages / "generic" / "current").resolve()
        assert current == (home_packages / "generic" / "generations" / a.version).resolve()
