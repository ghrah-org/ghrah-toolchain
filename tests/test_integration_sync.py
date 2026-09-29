# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""真实构建 / 代物化集成用例（@integration，默认跳过）。

单形态（packages）：本文件锁定真实 monorepo + 真实工具链下的：

1. install 全链路（真实构建产物树 → 代物化 → 门禁 → 重指 current）；
2. 双层复用（同 commit 二次 install 零重建零物化）；
3. 切换（A → B → A，代复用）；
4. boot 全链路（OS 经代内 env 真实拉起 + webui 静态 serve 真实 dist）。

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
def home_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path) -> Path:
    """packages 单形态安装域（真实构建产物树 + 代物化）。"""
    h = tmp_path / "ghrah-home-packages"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    monkeypatch.delenv("GHRAH_MONOREPO_ROOT", raising=False)
    distro_root = h / "generic"
    distro_root.mkdir(parents=True)
    (distro_root / "distro.toml").write_text(
        f"""
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


class TestInstallPackagesMode:
    """packages 模式真实构建物化 + 代物化 + 双层复用（真实 monorepo，分钟级）。"""

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

        # 组合代物化（无插件 gen_id = 基座版本名；entry.path 即代目录）
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

        # 代 env 冒烟：导入源是 gen env 内真实包（wheel 平铺安装）
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


class TestBootGeneration:
    """packages 域真实 boot 全链路（真实构建，分钟级）。

    - ``boot --no-webui``：OS 经 ``current/env/.venv`` 的真实
      ``ghrah-subject-server`` 拉起（健康检查真实通过）→ stop 端口释放；
    - ``boot``：webui 静态 serve 真实 dist 探活 + index WS 注入断言 → stop 端口释放。
    """

    @staticmethod
    async def _wait_port(port: int, *, listening: bool, timeout: float) -> None:
        import asyncio as _asyncio
        import time as _time

        from ghrah_bootloader.lifecycle import port_in_use

        deadline = _time.monotonic() + timeout
        while port_in_use("127.0.0.1", port) != listening:
            assert _time.monotonic() < deadline, (
                f"port {port} not {'listening' if listening else 'released'} in {timeout}s"
            )
            await _asyncio.sleep(0.5)

    async def test_ip3_boot_os_gen_env_and_static_webui(
        self, home_packages: Path, repo: Path, free_port_factory
    ) -> None:
        import asyncio as _asyncio
        import http.client as _http

        from ghrah_bootloader.boot import resolve_boot_target, run_boot

        _require("uv", "pnpm", "git")
        mgr = RuntimeRefManager(root=home_packages, repo=repo)
        entry = mgr.ensure(distro="generic", gate="basic", source=f"commit:{self_commit(repo)}")
        target = resolve_boot_target("generic", None, root=home_packages)
        assert target.tree == entry.path.resolve()

        # --no-webui：OS 经代内 env 真实拉起（健康检查过）→ stop 端口释放
        sport = free_port_factory()
        stop = _asyncio.Event()
        task = _asyncio.create_task(
            run_boot(target, no_webui=True, no_browser=True, subject_port=sport, stop_event=stop)
        )
        try:
            await self._wait_port(sport, listening=True, timeout=60.0)
        finally:
            stop.set()
            code = await _asyncio.wait_for(task, timeout=30.0)
        assert code == 0
        await self._wait_port(sport, listening=False, timeout=5.0)

        # 全栈：webui 静态 serve 真实 dist + index WS 注入 → stop 端口释放
        wport = free_port_factory()
        sport2 = free_port_factory()
        stop2 = _asyncio.Event()
        task2 = _asyncio.create_task(
            run_boot(
                target,
                no_browser=True,
                subject_port=sport2,
                webui_port=wport,
                stop_event=stop2,
            )
        )
        try:
            await self._wait_port(wport, listening=True, timeout=60.0)
            conn = _http.HTTPConnection("127.0.0.1", wport, timeout=10.0)
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            conn.close()
            assert resp.status == 200
            assert f'window.__GHRAH_SUBJECT_WS_URL__="ws://127.0.0.1:{sport2}/ws"' in body, (
                f"WS URL 注入缺失: {body[:200]}"
            )
        finally:
            stop2.set()
            code2 = await _asyncio.wait_for(task2, timeout=30.0)
        assert code2 == 0
        await self._wait_port(wport, listening=False, timeout=5.0)
