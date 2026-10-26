# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""本地开发快环（dev）：工作树构建 → 按包增量 → 本地起服验证。

新增代替修改：本模块与 ``dev`` 子命令是全新入口，只读写
``<distro_root>/.dev/`` 私有命名空间，绝不触碰 ``current`` / ``last-good`` /
``boot.toml`` 及正式 ``build``/``install``/``runtime``/``start`` 的外部行为。
其他成员不使用 ``dev`` 命令时，行为与现在完全一致。

快环语义：

- 构建源 = **工作树**（``git ls-files`` 取 tracked + untracked 非忽略文件），
  无需先 ``git commit``。
- 增量维度 = **包**（五包 wheel 各算一个指纹单元；依赖闭包 + webui 各自成单元）。
- 组合代 = ``.dev/gen``（持久、可原地升级）：Python 单包改动只重编该 wheel 后
  ``uv pip install --force-reinstall --no-deps`` 覆盖进 dev venv；依赖闭包或
  webui 改动走全量重建并整体重物化。
- 端口 = 接管正式 subject/webui 端口；本地开发态与发布态一次只启动一个。
- Windows 硬约束：运行中的 Python 持有 ``.pyd/.pyc`` 文件锁，故
  「停旧 → 装 → 起新」顺序不可颠倒（构建期可让旧服继续跑）。

与既有命令的分工：``install`` 建具名实例并重指 ``current``；``dev`` 从同一发行版
的**工作树**构建，产物落在 ``.dev/`` 而永不重指 ``current``——因此 dev 迭代不会
污染发布态身份。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ghrah_toolchain.boot import (
    _ENV_SERVER_HOST,
    _ENV_SERVER_PORT,
    BootTarget,
    boot_pid_path,
    os_server_command,
    resolve_boot_target,
    stop_boot_session,
    webui_static_root,
)
from ghrah_toolchain.build import (
    PYTHON_PACKAGES,
    VERIFY_IMPORTS,
    _build_webui,
    _build_wheels,
    _export_pins,
    _rmtree,
)
from ghrah_toolchain.errors import BuildError, ToolchainError
from ghrah_toolchain.layout import (
    DistroLayout,
    DistroToml,
    build_data_plane_env,
    ensure_data_dirs,
    resolve_data_root,
)
from ghrah_toolchain.lifecycle import DEFAULT_HOST, BootStack, StackConfig
from ghrah_toolchain.materialize import _install_python_env, _uv_env, _venv_python
from ghrah_toolchain.runtime_ref import _pid_alive

logger = logging.getLogger(__name__)

# .dev 命名空间子路径 / 文件名
DEV_DIR_NAME = ".dev"
STATE_FILE = "state.json"
DEV_PID = "dev.pid"
ARTIFACTS = "artifacts"
GEN = "gen"
WORK = ".work"
STATE_SCHEMA = 1
DEPS_FILES = ("pyproject.toml", "uv.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml")
WEBUI_UNIT = "ghrah-observer-webui"
# dev 会话自身的日志（与 start 的 <distro>/logs/<token>.log 对齐）
LOGFILE = "dev.log"


# ── 子进程与 git ─────────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        # 子进程（pnpm/uv/git）输出 UTF-8；中文 Windows 缺省 GBK 解码会崩读线程
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and cp.returncode != 0:
        raise BuildError(
            f"命令失败({cp.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"{cp.stderr.strip()[-2000:] or cp.stdout.strip()[-2000:]}"
        )
    return cp


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], check=False)


def _git_head(repo: Path) -> str:
    cp = _git(repo, "rev-parse", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else "-"


def _ls_files(repo: Path, *prefixes: str) -> list[str]:
    """tracked + untracked（非忽略）文件清单（repo 相对路径，``/`` 分隔）。"""
    cp = _git(repo, "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", *prefixes)
    if cp.returncode != 0:
        raise BuildError(f"git ls-files 失败: {cp.stderr.strip() or '源仓非 git 工作树'}")
    return [p for p in cp.stdout.split("\0") if p]


def _snapshot_worktree(repo: Path, dst_src: Path) -> Path:
    """把工作树（tracked + untracked 非忽略）拷到 ``dst_src``（即 monorepo 根）。"""
    _rmtree(dst_src) if dst_src.exists() else None
    dst_src.mkdir(parents=True)
    for rel in _ls_files(repo):
        src = repo / rel
        if not src.is_file():
            continue
        dst = dst_src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 字节级拷贝（Python 文本文件而非符号链接；逐块流入）
        with src.open("rb") as fin, dst.open("wb") as fout:
            while True:
                chunk = fin.read(1 << 20)
                if not chunk:
                    break
                fout.write(chunk)
    return dst_src


# ── 指纹（按包内容摘要，工作树 vs 上次 dev 构建） ─────────────


def _files_digest(repo: Path, relpaths: list[str]) -> str:
    h = hashlib.sha256()
    parts: list[str] = []
    for rel in sorted(relpaths):
        p = repo / rel
        if not p.is_file():
            continue
        ch = hashlib.sha256()
        with p.open("rb") as f:
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                ch.update(block)
        parts.append(f"{rel}\0{ch.hexdigest()}")
    h.update("\n".join(parts).encode("utf-8"))
    return h.hexdigest()


def _unit_digest(repo: Path, rel_dir: str) -> str:
    return _files_digest(repo, _ls_files(repo, rel_dir))


def _compute_fingerprints(repo: Path) -> dict[str, str]:
    fp: dict[str, str] = {}
    for unit in PYTHON_PACKAGES:
        fp[unit] = _unit_digest(repo, unit)
    fp[WEBUI_UNIT] = _unit_digest(repo, WEBUI_UNIT)
    deps = [f for f in DEPS_FILES if (repo / f).is_file()]
    fp["deps"] = _files_digest(repo, deps)
    return fp


# ── state（上次 dev 构建基线） ────────────────────────────────


def _load_state(dev_root: Path) -> dict | None:
    path = dev_root / STATE_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None


def _save_state(dev_root: Path, state: dict) -> None:
    dev_root.mkdir(parents=True, exist_ok=True)
    tmp = dev_root / f"{STATE_FILE}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dev_root / STATE_FILE)


# ── 上下文解析 ───────────────────────────────────────────────


@dataclass
class DevContext:
    """dev 会话上下文：复用 ``resolve_boot_target`` 的解析结果（含布局与配置）。"""

    target: BootTarget
    repo: Path

    @property
    def distro(self) -> str:
        return self.target.distro

    @property
    def distro_root(self) -> Path:
        return self.target.layout.root

    @property
    def layout(self) -> DistroLayout:
        return self.target.layout

    @property
    def toml(self) -> DistroToml:
        return self.target.distro_toml

    @property
    def dev_root(self) -> Path:
        return self.target.layout.root / DEV_DIR_NAME

    @property
    def gen(self) -> Path:
        return self.dev_root / GEN

    @property
    def artifacts(self) -> Path:
        return self.dev_root / ARTIFACTS


def _resolve_context(args: argparse.Namespace) -> DevContext:
    """解析 dev 上下文：目标（``--distro`` > boot.toml default）+ 源仓工作树。

    复用 ``resolve_boot_target``：发行版解析、``current`` 校验、``distro.toml``
    读取都只有那一份实现。代价是 dev 需要发行版已安装并重指过 ``current``——
    与 ``install`` 的产物一致，不额外要求别的。
    """
    target = resolve_boot_target(args.distro, None)
    repo_path = target.distro_toml.repo_path
    if not repo_path:
        raise ToolchainError("distro.toml 未配置源仓路径（[distro.repo].path）；dev 需要源仓工作树")
    repo = Path(repo_path).expanduser()
    if not (repo / ".git").is_dir():
        raise ToolchainError(
            f"源仓无效（需 git 工作树）: {repo}；检查 distro.toml [distro.repo].path"
        )
    return DevContext(target=target, repo=repo)


# ── 构建（全量 / 增量） ──────────────────────────────────────


def _build_wheels_subset(src: Path, wheels_dir: Path, packages: list[str]) -> list[Path]:
    """逐包 ``uv build --wheel``（只构建 ``packages`` 子集，镜像 build._build_wheels）。"""
    wheels_dir.mkdir(parents=True, exist_ok=True)
    for pkg in packages:
        pkg_dir = src / pkg
        if not pkg_dir.is_dir():
            raise BuildError(f"源树缺少 Python 包目录: {pkg_dir}")
        _run(
            [
                "uv",
                "build",
                str(pkg_dir),
                "--wheel",
                f"--out-dir={wheels_dir}",
                "--no-create-gitignore",
            ]
        )
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise BuildError(f"未产出任何 wheel: {wheels_dir}")
    return wheels


def _build_full(src: Path, artifacts: Path) -> None:
    """全量：五包 wheels + pins + webui（tarballs + observer-web dist）→ 原子落位。"""
    tmp = artifacts.parent / f"{ARTIFACTS}.tmp-{os.getpid()}"
    _rmtree(tmp) if tmp.exists() else None
    try:
        _build_wheels(src, tmp / "wheels")
        _export_pins(src, tmp / "wheels")
        _build_webui(src, tmp / "tarballs", tmp)
        if artifacts.exists():
            _rmtree(artifacts)
        artifacts.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, artifacts)
    except BaseException:
        _rmtree(tmp) if tmp.exists() else None
        raise


def _wheel_for(artifacts: Path, pkg: str) -> Path:
    prefix = pkg.replace("-", "_")
    matches = sorted((artifacts / "wheels").glob(f"{prefix}-*.whl"))
    if len(matches) != 1:
        raise BuildError(f"wheel 定位失败（{pkg} → {len(matches)} 个候选）: {artifacts / 'wheels'}")
    return matches[0]


def _build_incremental(src: Path, artifacts: Path, packages: list[str]) -> None:
    """增量：只重编 ``packages`` 的 wheel 覆盖进既有 artifacts/wheels。"""
    wheels_dir = artifacts / "wheels"
    _build_wheels_subset(src, wheels_dir, packages)


# ── dev 代物化 / 升级 ────────────────────────────────────────


def _gen_ready(gen: Path) -> bool:
    return _venv_python(gen / "env" / ".venv").is_file()


def _materialize_dev(distro_root: Path, artifacts: Path, gen: Path) -> None:
    """全量物化 .dev/gen/env/.venv（staging + 原子落位；不含 webui node 环境）。"""
    staging = gen.parent / f"{GEN}.tmp-{os.getpid()}"
    _rmtree(staging) if staging.exists() else None
    try:
        _install_python_env(distro_root, artifacts, staging / "env" / ".venv")
        if gen.exists():
            _rmtree(gen)
        gen.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, gen)
    except BaseException:
        _rmtree(staging) if staging.exists() else None
        raise


def _upgrade_dev(distro_root: Path, artifacts: Path, gen: Path, packages: list[str]) -> None:
    """增量升级：把变化包的 wheel 覆盖进既有 dev venv（其余不动）。"""
    py = _venv_python(gen / "env" / ".venv")
    wheels = [_wheel_for(artifacts, pkg) for pkg in packages]
    cmd = ["uv", "pip", "install", "--force-reinstall", "--no-deps", f"--python={py}"]
    cmd += [str(w) for w in wheels]
    _run(cmd, env=_uv_env(distro_root))


def _smoke(gen: Path) -> None:
    """轻量冒烟：dev venv 内 import 五包 + 入口存在（不出全量 verify_artifacts）。"""
    py = _venv_python(gen / "env" / ".venv")
    _run([str(py), "-c", VERIFY_IMPORTS])
    os_server_command(gen / "env" / ".venv")  # 入口存在性校验（缺失即抛）


# ── 进程生命周期（dev 会话 / 发布态会话） ─────────────────────


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        cp = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if cp.returncode != 0:
            logger.warning(
                "taskkill 失败(pid=%s, rc=%s): %s",
                pid,
                cp.returncode,
                (cp.stderr or cp.stdout or "").strip(),
            )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(25):  # ~5s 宽限后强杀
            if _pid_alive(pid) is False:
                return
            time.sleep(0.2)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _stop_dev_session(ctx: DevContext) -> bool:
    """停掉运行中的 dev 会话（读 .dev/dev.pid）。返回是否确曾发出停止。

    停不掉时**抛错**而不是继续：``dev down`` 谎报「已停止」、或 ``dev up`` 在旧
    会话仍持有 venv 文件锁的情况下继续升级，两者都比直接失败更难查。
    """
    pidfile = ctx.dev_root / DEV_PID
    if not pidfile.is_file():
        return False
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except OSError, ValueError:
        pid = 0
    if pid and _pid_alive(pid) is True:
        print(f"[dev] 停止旧的 dev 会话 (pid={pid}) …", flush=True)
        _kill_tree(pid)
        deadline = time.monotonic() + 15.0
        while _pid_alive(pid) is not False and time.monotonic() < deadline:
            time.sleep(0.2)
        if _pid_alive(pid) is not False:
            raise ToolchainError(
                f"无法停止 dev 会话 (pid={pid})；请手动结束该进程后重试（pidfile: {pidfile}）"
            )
    with contextlib.suppress(OSError):
        pidfile.unlink()
    return pid != 0


def _read_dev_pid(ctx: DevContext) -> int:
    """读 ``.dev/dev.pid``（缺失/损坏 → 0）。"""
    pidfile = ctx.dev_root / DEV_PID
    if not pidfile.is_file():
        return 0
    try:
        return int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except OSError, ValueError:
        return 0


def _dev_session_alive(ctx: DevContext) -> bool:
    """dev 会话是否确在运行（读 .dev/dev.pid + 存活探测）。"""
    pid = _read_dev_pid(ctx)
    return pid != 0 and _pid_alive(pid) is True


def _print_access_hint(ctx: DevContext, pid: int | None = None) -> None:
    """打印访问地址／日志／停止方式（就绪与「已在运行」两条路径共用）。"""
    print(f"[dev]   前端页面  http://localhost:{ctx.toml.webui_port}", flush=True)
    print(
        f"[dev]   后端接口  http://127.0.0.1:{ctx.toml.subject_port}"
        f"（/health、ws ws://127.0.0.1:{ctx.toml.subject_port}/ws）",
        flush=True,
    )
    print(f"[dev]   运行日志  {ctx.dev_root / LOGFILE}", flush=True)
    if pid:
        print(f"[dev]   会话 pid  {pid}（停止用 ghrah dev down）", flush=True)
    else:
        print("[dev]   停止：Ctrl-C，或在另一终端运行 ghrah dev down", flush=True)


def _stop_release_if_running(ctx: DevContext) -> None:
    """开发态接管正式端口前：停掉正在运行的发布态会话（一次只启动一个）。"""
    if not boot_pid_path(ctx.distro_root).is_file():
        return
    print("[dev] 发布态会话正在运行，先停止它（接管正式端口）…")
    stop_boot_session(
        ctx.distro,
        subject_port=ctx.toml.subject_port,
        webui_port=ctx.toml.webui_port,
    )


def _dev_webui_root(ctx: DevContext) -> Path:
    """webui 静态 serve 根：dev 产物优先，否则复用发布态基座 dist。

    发布态分支走 ``boot.webui_static_root``（代 → 基座树 dist 的单一实现），
    缺失即 fail-closed，不在本模块重拼路径。
    """
    dev_dist = ctx.artifacts / "observer-web" / "dist"
    if dev_dist.is_dir():
        return dev_dist
    return webui_static_root(ctx.target)


# ── 起服（前台监督，镜像 run_boot 但钉 .dev/gen） ─────────────


async def _serve_dev(ctx: DevContext, *, open_browser: bool) -> int:
    data_root = resolve_data_root(ctx.layout, ctx.toml)
    ensure_data_dirs(data_root)
    sport = ctx.toml.subject_port
    wport = ctx.toml.webui_port
    config = StackConfig(
        host=DEFAULT_HOST,
        subject_port=sport,
        webui_port=wport,
        open_browser=open_browser,
    )
    os_env = {
        **build_data_plane_env(data_root),
        _ENV_SERVER_HOST: config.host,
        _ENV_SERVER_PORT: str(sport),
    }
    webui_root = _dev_webui_root(ctx)
    stack = BootStack(
        config,
        os_command=os_server_command(ctx.gen / "env" / ".venv"),
        os_cwd=ctx.gen,
        os_env=os_env,
        webui_static_root=webui_root,
        webui_ws_url=config.subject_ws_url(),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except NotImplementedError, RuntimeError, ValueError:
            logger.debug("signal handler unavailable: %s", sig)
    pidfile = ctx.dev_root / DEV_PID
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    try:
        pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as exc:
        raise ToolchainError(
            f"无法写 dev pidfile: {pidfile}（{exc}）；该目录需可写。"
            "若你用的是 `uv run ghrah ...`：该解释器位于工作区内，受限环境下只能写"
            "工作区，写 ~/.ghrah 会被拒——改用 `uv tool install --editable .` 装出的 "
            "`ghrah` 命令再试"
        ) from exc
    try:
        print(
            f"[dev] 正在启动 OS 与前端（subject :{sport} / webui :{wport}）…",
            flush=True,
        )
        await stack.start()
        print("", flush=True)
        print("[dev] ===== dev 环境已就绪 =====", flush=True)
        _print_access_hint(ctx)
        print(f"[dev]   开发 venv {ctx.gen / 'env' / '.venv'}", flush=True)
        print(
            "[dev]   浏览器已自动打开（要关闭用 --no-browser）"
            if open_browser
            else "[dev]   浏览器未打开（--no-browser）",
            flush=True,
        )
        while True:
            if stop.is_set():
                logger.info("收到停止信号，逆序收束 dev 子进程（webui → OS）")
                return 0
            proc = stack.os_process
            if proc is not None and proc.poll() is not None:
                logger.error("dev OS 子进程意外退出 (code=%s)，收束其余子进程", proc.returncode)
                return 1
            await asyncio.sleep(0.5)
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError, RuntimeError, ValueError:
                logger.debug("signal handler removal failed: %s", sig)
        with contextlib.suppress(OSError):
            pidfile.unlink()
        await asyncio.shield(stack.stop())


# ── 命令面 ───────────────────────────────────────────────────


def dev_status(ctx: DevContext) -> int:
    fp = _compute_fingerprints(ctx.repo)
    state = _load_state(ctx.dev_root)
    changed: list[str] = []
    if state is None:
        changed = ["deps", *PYTHON_PACKAGES, WEBUI_UNIT]
    else:
        for key in ("deps", *PYTHON_PACKAGES, WEBUI_UNIT):
            if state.get("units", {}).get(key) != fp[key]:
                changed.append(key)
    if not changed:
        print("[dev] 无变更（工作树与上次 dev 构建一致）")
    else:
        deps = "deps" in changed
        py = [p for p in PYTHON_PACKAGES if p in changed]
        webui = WEBUI_UNIT in changed
        plan = (
            "全量（依赖闭包/webui 变更或首次）"
            if (deps or webui or state is None)
            else f"增量（{', '.join(py)}）"
        )
        print(f"[dev] 检测到变更 → {plan}")
        if py:
            print(f"[dev]   Python 包: {', '.join(py)}")
        if deps:
            print("[dev]   依赖闭包: pyproject.toml / uv.lock / pnpm-lock / pnpm-workspace")
        if webui:
            print(f"[dev]   webui: {WEBUI_UNIT}")
    if _dev_session_alive(ctx):
        print("[dev] 当前有 dev 会话在运行：", flush=True)
        _print_access_hint(ctx, _read_dev_pid(ctx))
    else:
        print("[dev] 当前没有运行中的 dev 会话（ghrah dev up 起服）", flush=True)
    return 0


def dev_up(
    ctx: DevContext, *, no_restart: bool, force_full: bool, open_browser: bool = True
) -> int:
    logger.info("dev up: distro=%s repo=%s head=%s", ctx.distro, ctx.repo, _git_head(ctx.repo))
    fp = _compute_fingerprints(ctx.repo)
    state = _load_state(ctx.dev_root)
    gen = ctx.gen
    artifacts = ctx.artifacts

    deps_changed = state is not None and state.get("units", {}).get("deps") != fp["deps"]
    py_changed = [
        u for u in PYTHON_PACKAGES if state is None or state.get("units", {}).get(u) != fp[u]
    ]
    webui_changed = state is not None and state.get("units", {}).get(WEBUI_UNIT) != fp[WEBUI_UNIT]
    pins_ok = (artifacts / "wheels" / "pins.txt").is_file()
    do_full = (
        force_full
        or state is None
        or deps_changed
        or webui_changed
        or not pins_ok
        or not _gen_ready(gen)
    )
    needs_build = do_full or bool(py_changed)

    if needs_build:
        work = ctx.dev_root / WORK
        work.mkdir(parents=True, exist_ok=True)
        src = work / f"src-{os.getpid()}"

        try:
            print(f"[dev] 快照工作树 → {src}", flush=True)
            _snapshot_worktree(ctx.repo, src)

            if do_full:
                print(
                    "[dev] 全量构建（wheels + pins + webui，含 pnpm 打包，可能数分钟）…", flush=True
                )
                _build_full(src, artifacts)
                rebuild_packages = list(PYTHON_PACKAGES)
            else:
                print(f"[dev] 增量构建（{', '.join(py_changed)}）…", flush=True)
                _build_incremental(src, artifacts, py_changed)
                rebuild_packages = py_changed

            # Windows 文件锁：先停旧 dev（释放 dev venv），再 install
            _stop_dev_session(ctx)

            if do_full or not _gen_ready(gen):
                print("[dev] 全量物化 dev venv …", flush=True)
                _materialize_dev(ctx.distro_root, artifacts, gen)
            else:
                print("[dev] 增量升级 dev venv …", flush=True)
                _upgrade_dev(ctx.distro_root, artifacts, gen, rebuild_packages)

            print("[dev] 冒烟（import 五包 + 入口）…", flush=True)
            _smoke(gen)
        finally:
            _rmtree(src) if src.exists() else None

        _save_state(
            ctx.dev_root,
            {
                "schema_version": STATE_SCHEMA,
                "repo": str(ctx.repo),
                "head": _git_head(ctx.repo),
                "units": fp,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    if no_restart:
        print(
            "[dev] 已构建/升级，未启动服务（--no-restart）"
            if needs_build
            else "[dev] 无变更，未启动服务（--no-restart）",
            flush=True,
        )
        return 0

    if not needs_build and _dev_session_alive(ctx):
        print("[dev] 无变更，且 dev 服务已在运行 → 无需重启", flush=True)
        _print_access_hint(ctx, _read_dev_pid(ctx))
        return 0

    _stop_release_if_running(ctx)
    return asyncio.run(_serve_dev(ctx, open_browser=open_browser))


def dev_down(ctx: DevContext) -> int:
    if not (ctx.dev_root / DEV_PID).is_file():
        print("[dev] dev 会话未在运行")
        return 0
    _stop_dev_session(ctx)
    print("[dev] 已停止 dev 会话")
    return 0


def dev_reset(ctx: DevContext) -> int:
    if ctx.dev_root.exists():
        _rmtree(ctx.dev_root)
        print(f"[dev] 已清空 dev 命名空间: {ctx.dev_root}")
    else:
        print("[dev] 无 dev 命名空间（已干净）")
    return 0


def _setup_dev_session_io(ctx: DevContext) -> None:
    """dev 会话 IO 约定：stdout 逐行 flush + 自身日志落 ``.dev/dev.log``。

    三件事都是对「跑起来像没在跑」的修复：

    1. ``print`` 在管道／IDE 控制台下是块缓冲——不显式逐行 flush，构建阶段的
       ``[dev] ...`` 一行都不会及时出现，而子进程（uvicorn）因继承 fd 立刻可见，
       观感就是"卡住且没有日志"。
    2. ``start`` 的后台会话有 ``<distro>/logs/<token>.log``；dev 此前不留任何日志
       文件。这里挂一个 INFO 级 FileHandler，把监督侧的 INFO 记录留下来。
    3. 日志文件是**补充手段、不是必需品**：``delay=True`` 惰性打开，且打不开就
       只告警——绝不让「写不了日志」把 ``dev down`` 这类命令一起弄挂。
    """
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    log_path = ctx.dev_root / LOGFILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    except OSError:
        logger.warning("dev 日志文件不可用，本次仅输出到控制台: %s", log_path, exc_info=True)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def run_dev_command(args: argparse.Namespace) -> int:
    """``dev`` 子命令统一分发（status/up/down/reset）。"""
    ctx = _resolve_context(args)
    _setup_dev_session_io(ctx)
    cmd = args.subcommand
    if cmd == "status":
        return dev_status(ctx)
    if cmd == "up":
        return dev_up(
            ctx,
            no_restart=bool(getattr(args, "no_restart", False)),
            force_full=bool(getattr(args, "full", False)),
            open_browser=not bool(getattr(args, "no_browser", False)),
        )
    if cmd == "down":
        return dev_down(ctx)
    if cmd == "reset":
        return dev_reset(ctx)
    raise ToolchainError(f"未知 dev 子命令: {cmd}")
