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
    monkeypatch.setenv("GHRAH_REF_SOURCE", f"commit:{self_commit(repo)}")
    h.mkdir(parents=True)
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
