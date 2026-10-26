# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ref 状态机：源解析 → 建版本树/组合代 → 门禁 → 快照 → 原子重指 current → 按需 sync。

纯 stdlib（subprocess / pathlib / dataclasses / tomllib / hashlib）。

单一安装形态（``packages``, worktree 形态退役）：

- 版本树 = 构建产物（wheels/tarballs/manifest/BOOTSTRAP.toml），
  身份 = ``BOOTSTRAP.toml source_commit``（纯文件零 git）；
- 其上物化组合代 ``generations/<gen-id>/``（per-gen env）——门禁与 sync 均跑在代上。

切换顺序固定（失败 = 什么都没发生）：

```
guard(运行中拒绝) → lock → 解析候选 commit → 建基座树/复用
  → 建组合代/复用（generations/<gen-id>/，per-gen env）
  → gate（代 env 冒烟，绝不触碰任何发行版级 env/）
  → snapshot（存在旧 current 时无条件拍）
  → repoint current（原子 → generations/）
  → 代复核（指纹一致零操作）
  → 写 boot.toml → 释放锁
```

- gate/snapshot 失败：current / boot.toml 未动；本次新建的候选树与代移除。
- sync 及其后失败：重指回旧版本 + 旧代复核（指纹一致零操作）+ 回写 boot.toml
  + 移除本次新建 → ``RuntimeRefError``。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ghrah_toolchain import snapshot
from ghrah_toolchain.build import build_artifacts, read_source_commit
from ghrah_toolchain.errors import GateError, RuntimeRefError, ToolchainError
from ghrah_toolchain.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    resolve_distro_root,
)
from ghrah_toolchain.lifecycle import DEFAULT_HOST, port_in_use
from ghrah_toolchain.materialize import (
    GenerationSpec,
    generation_complete,
    generation_dir,
    generation_fingerprint,
    materialize_generation,
    read_generation,
)
from ghrah_toolchain.repository import RemoteRepository, RepositorySource, require_git

logger = logging.getLogger(__name__)

_GATE_LEVELS = ("basic", "full", "data")
_STALE_LOCK_SECONDS = 600.0


# ── 锁 ──────────────────────────────────────────────────────


def distro_lock_path(distro_root: Path) -> Path:
    """发行级锁文件路径。"""
    return distro_root / ".bootloader.lock"


def _pid_alive(pid: int) -> bool | None:
    """pid 存活探测（三值：``None`` = 探测不可用，fail-closed 由调用方处理）。

    - POSIX：``os.kill(pid, 0)``（恒返回 bool）；
    - Windows：``winproc.pid_alive``（OpenProcess + GetExitCodeProcess；
      替换早期恒返回 True 的桩实现）。``None`` 仅在探测不可用时出现。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        from ghrah_toolchain import winproc

        return winproc.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _lock_is_stale(path: Path, stale_after: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        pid = int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return True
    except OSError, ValueError:
        # 刚创建的锁可能尚未写入 PID，不能在该竞窗抢走它。
        return path.exists() and time.time() - path.stat().st_mtime > stale_after
    # 探测不可用（None）不判死——时间兜底（600s）仍在，探测失败不应
    # 让锁被误回收（与 pidfile 的 fail-closed 语义一致）
    alive = _pid_alive(pid)
    return alive is False or (alive is None and age > stale_after)


@contextlib.contextmanager
def acquire_distro_lock(distro_root: Path, *, stale_after: float = _STALE_LOCK_SECONDS):
    """``O_CREAT|O_EXCL`` 锁 + 写 pid；陈旧（超时/死 pid）回收重试，否则报错。"""
    path = distro_lock_path(distro_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(path, stale_after):
                logger.warning("回收陈旧引导器锁: %s", path)
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            raise RuntimeRefError(
                f"另一工具链操作正在运行（锁: {path}）；请等待其完成后重试"
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        break
    try:
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


# ── 原子重指（boot 救援共用） ────────────────────────────────


def _repoint_link(distro_root: Path, name: str, target: str) -> None:
    """原子建/更新 ``<name>`` 符号链接指向 ``generations/<target>``。

    段固定 generations：current/last-good 只指向组合代；
    ``versions/`` 仍是基座产物库（枚举显示面），但不再作为引导目标。

    Windows 走 ``winproc.repoint_dir_link`` 三级回退（symlink+replace →
    删旧建新 → junction）。
    """
    link = distro_root / name
    if os.name == "nt":
        from ghrah_toolchain import winproc
        from ghrah_toolchain.errors import WinProcError

        target_abs = (distro_root / "generations" / target).resolve()
        try:
            strategy = winproc.repoint_dir_link(link, target_abs)
        except WinProcError as e:
            raise RuntimeRefError(f"重指 {name} 失败: {e}") from e
        if strategy != "symlink-replace":
            logger.info("Windows 重指采用回退策略 %s: %s", strategy, link)
        return
    tmp = distro_root / f"{name}.tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(f"generations/{target}")
    os.replace(tmp, link)


def repoint_current(distro_root: Path, ver: str) -> None:
    """原子重指 ``current``：``current.tmp-<pid>`` 相对符号链接 + ``os.replace``。"""
    _repoint_link(distro_root, "current", ver)


def repoint_last_good(distro_root: Path, ver: str) -> None:
    """原子建/更新 ``last-good`` 符号链接。"""
    _repoint_link(distro_root, "last-good", ver)


# ── 数据类型 ─────────────────────────────────────────────────


@dataclass
class RefSource:
    """签出源：latest、branch:<name>、tag:<name> 或 commit:<sha>。"""

    kind: str  # latest / branch / tag / commit
    value: str | None

    @classmethod
    def parse(cls, raw: str | None) -> RefSource:
        """解析源串；``None`` 视为默认 ``latest``，非法格式 → ``RuntimeRefError``。"""
        if raw is None:
            return cls(kind="latest", value=None)
        text = raw.strip()
        if text == "latest":
            return cls(kind="latest", value=None)
        if ":" in text:
            kind, _, value = text.partition(":")
            if kind in ("branch", "tag", "commit") and value:
                return cls(kind=kind, value=value)
        raise RuntimeRefError(
            f"非法 ref 源: {raw!r}（应为 latest | branch:<n> | tag:<n> | commit:<sha>）"
        )

    def render(self) -> str:
        if self.kind == "latest":
            return "latest"
        return f"{self.kind}:{self.value}"


@dataclass
class BootEntry:
    """一个已安装的引导项（发行版 / 版本 / 树 / commit）。

    packages 域 ``version`` 为 gen-id（无插件 = 基座版本名），``path`` 为代目录。
    """

    distro: str
    version: str
    path: Path
    commit: str | None
    kind: str = "version"  # version（versions/ 树） | generation（generations/ 代）
    base_version: str = ""

    @property
    def label(self) -> str:
        return f"{self.distro}/{self.version}"

    @property
    def detail(self) -> str:
        """枚举面展示：代注明基座（`<gen-id>（base=<ver> plugins=[…]）`）。"""
        if self.kind != "generation":
            return self.version
        plugins = (
            ",".join(p.name for p in read_generation(self.path).plugins)
            if (read_generation(self.path))
            else ""
        )
        return f"{self.version}（base={self.base_version or '?'} plugins=[{plugins}]）"


# ── 状态机 ───────────────────────────────────────────────────


class RuntimeRefManager:
    """GPT 枚举 / 源解析 / 建树 / 门禁 / 快照 / 重指 / sync 的统一状态机。"""

    def __init__(self, *, root: Path | None = None, repo: str | Path | None = None) -> None:
        self._root = root or ghrah_root()
        self._repo = repo

    # ── 只读面 ──

    def list_entries(self) -> list[BootEntry]:
        """枚举全部发行版 × 版本/组合代。

        显示面身份双跳（BOOTSTRAP → git → None）；packages 域同时枚举基座
        versions/ 与组合代 generations/（代标注 base 与插件集）。
        """
        entries: list[BootEntry] = []
        for distro in list_distros(self._root):
            layout = DistroLayout(root=self._root / distro)
            for ver in layout.list_versions():
                tree = layout.versions / ver
                entries.append(
                    BootEntry(
                        distro=distro,
                        version=ver,
                        path=tree,
                        commit=self._tree_commit(tree),
                    )
                )
            for gen in layout.list_generations():
                gen_dir = layout.generations / gen
                info = read_generation(gen_dir)
                entries.append(
                    BootEntry(
                        distro=distro,
                        version=gen,
                        path=gen_dir,
                        commit=info.base_commit if info else None,
                        kind="generation",
                        base_version=info.base_version if info else "",
                    )
                )
        return entries

    def resolve(self, distro: str | None = None) -> BootEntry:
        """解析当前引导项：``current`` 指向（distro 取参数或 boot.toml default）。"""
        boot = BootToml.load(self._root)
        d = distro or (boot.default.split("/", 1)[0] if boot.default else "")
        if not d:
            raise RuntimeRefError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
        entry = self._current_entry(d)
        if entry is None:
            raise RuntimeRefError(
                f"current 未指向任何版本: {self._root / d / 'current'}（先完成 install）"
            )
        return entry

    # ── 切换面 ──

    def ensure(
        self,
        *,
        distro: str | None = None,
        update: bool = False,
        fetch: bool = False,
        source: str | RefSource | None = None,
        version: str | None = None,
        gate: str = "basic",
        initial_config: str | None = None,
    ) -> BootEntry:
        """安装/切换到候选 commit 并重指 ``current``（全程持锁）。"""
        require_git()
        if gate != "basic":
            raise GateError("目前只支持 basic 门禁，full/data 尚未实现")
        if version and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", version) is None:
            raise RuntimeRefError(f"非法版本名: {version!r}")
        # 实例锁覆盖构建与切换，安装域锁只覆盖共享引导菜单的提交。
        boot_before = BootToml.load(self._root)
        d = distro or (boot_before.default.split("/", 1)[0] if boot_before.default else "")
        if not d:
            raise RuntimeRefError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
        distro_root = resolve_distro_root(d, self._root)
        with acquire_distro_lock(distro_root):
            from ghrah_toolchain.instances import assert_instance_stopped
            from ghrah_toolchain.toml_io import atomic_write

            assert_instance_stopped(distro_root)
            path = distro_root / "distro.toml"
            created_config = initial_config is not None and not path.exists()
            if created_config:
                atomic_write(path, initial_config)
            try:
                distro_toml = DistroToml.load(distro_root)
                self._guard_not_running(distro_toml)
                old = self._current_entry(d)
                if update and old is None:
                    raise RuntimeRefError(f"发行版 {d} 尚未安装；请先 ghrah distro install")
                logger.info("[%s] 解析来源，准备构建／复用产物与运行环境", d)
                return self._ensure_locked(
                    d,
                    distro_root,
                    distro_toml,
                    boot_before,
                    old,
                    update=update,
                    fetch=fetch,
                    source=source,
                    version=version,
                    gate=gate,
                )
            except Exception:
                if created_config and not (distro_root / "current").exists():
                    path.unlink(missing_ok=True)
                raise

    def _ensure_locked(
        self,
        distro: str,
        distro_root: Path,
        distro_toml: DistroToml,
        boot_before: BootToml,
        old: BootEntry | None,
        *,
        update: bool,
        fetch: bool,
        source: str | RefSource | None,
        version: str | None,
        gate: str,
    ) -> BootEntry:
        distro_root.mkdir(parents=True, exist_ok=True)
        location = self._repo_source(distro_toml)
        if location.remote:
            context = RemoteRepository(self._root, location).prepare(
                refresh=fetch, allow_clone=(not update or fetch)
            )
        else:
            context = contextlib.nullcontext(Path(location.location))
        with context as repo:
            return self._ensure_repository_locked(
                distro,
                distro_root,
                distro_toml,
                boot_before,
                old,
                repo,
                fetch=fetch and not location.remote,
                source=source,
                version=version,
                gate=gate,
            )

    def _ensure_repository_locked(
        self,
        distro: str,
        distro_root: Path,
        distro_toml: DistroToml,
        boot_before: BootToml,
        old: BootEntry | None,
        repo: Path,
        *,
        fetch: bool,
        source: str | RefSource | None,
        version: str | None,
        gate: str,
    ) -> BootEntry:
        src = self._coerce_source(source, distro_toml)
        commit = self._resolve_candidate(src, fetch=fetch, repo=repo)

        reused_ver = self._find_version_by_commit(distro, commit)
        if reused_ver is not None:
            ver, tree, created = reused_ver, distro_root / "versions" / reused_ver, False
        else:
            base_ver = version or self._derive_version_name(repo, commit)
            ver, reused = self._claim_version_name(distro, base_ver, commit, repo)
            tree = distro_root / "versions" / ver
            created = not reused
            if created:
                logger.info("[%s] 构建基座产物 %s", distro, ver)
                self._create_version_tree(distro, ver, commit, repo)

        return self._ensure_generation_locked(
            distro,
            distro_root,
            boot_before,
            old,
            ver,
            tree,
            commit,
            base_created=created,
            src=src,
            gate=gate,
        )

    def _ensure_generation_locked(
        self,
        distro: str,
        distro_root: Path,
        boot_before: BootToml,
        old: BootEntry | None,
        ver: str,
        tree: Path,
        commit: str,
        *,
        base_created: bool,
        src: RefSource,
        gate: str,
    ) -> BootEntry:
        """packages 域四段续建：基座就位后物化组合代 → 门禁在代 → 重指到代。

        失败清理分层：新建基座与新建代同删；复用的基座/代保留。
        """
        spec = self._generation_spec(tree, ver, commit)
        gen = spec.gen_id()
        gen_dir = generation_dir(distro_root, spec)
        gen_created = not gen_dir.exists()
        try:
            if gen_created:
                logger.info("[%s] 准备组合代环境 %s", distro, gen)
                materialize_generation(distro_root, tree, spec)
            new_entry = BootEntry(
                distro=distro,
                version=gen,
                path=gen_dir,
                commit=commit,
                kind="generation",
                base_version=ver,
            )
            logger.info("[%s] 检查 %s 门禁", distro, gate)
            self._gate(gen_dir, gate)
        except Exception:
            self._cleanup_generation(gen_created, gen_dir)
            self._cleanup_tree(base_created, tree)
            raise
        try:
            if old is not None and old.path == new_entry.path:
                self._sync(distro, gen)
                logger.info("[%s] 已是所选组合代，无需切换", distro)
                return new_entry
            if old is not None:
                logger.info("[%s] 保存更新前的数据快照", distro)
                self._snapshot(distro_root, DistroToml.load(distro_root), old, new_entry, src, gate)
        except Exception:
            self._cleanup_generation(gen_created, gen_dir)
            self._cleanup_tree(base_created, tree)
            raise
        from ghrah_toolchain.instances import _locked

        with _locked(self._root):
            boot_before = BootToml.load(self._root)
            try:
                repoint_current(distro_root, gen)
                self._sync(distro, gen)
                self._write_boot_toml(boot_before, old, new_entry)
                if old is None:
                    repoint_last_good(distro_root, gen)
            except Exception as e:
                logger.error("切换失败，开始回退: %s", e)
                try:
                    self._rollback(distro, old, boot_before)
                except Exception as rollback_error:
                    raise RuntimeRefError(
                        f"切换失败且回退未完成，请检查 current/boot.toml；"
                        f"保留候选供恢复: {e}; {rollback_error}"
                    ) from rollback_error
                self._cleanup_generation(gen_created, gen_dir)
                self._cleanup_tree(base_created, tree)
                target = old.label if old is not None else "（无版本）"
                raise RuntimeRefError(
                    f"切换到 {new_entry.label} 失败，已回退至 {target}: {e}"
                ) from e
        self._log_change(old, new_entry, src, gate)
        return new_entry

    def _generation_spec(self, tree: Path, ver: str, commit: str) -> GenerationSpec:
        """从基座产物树读 pins_sha256，组装代身份输入（插件集当前恒空）。"""
        pins = tree / "wheels" / "pins.txt"
        if not pins.is_file():
            raise RuntimeRefError(f"基座产物树无 pins.txt: {tree}")
        pins_sha256 = hashlib.sha256(pins.read_bytes()).hexdigest()
        return GenerationSpec(base_version=ver, base_commit=commit, pins_sha256=pins_sha256)

    def _cleanup_generation(self, created: bool, gen_dir: Path) -> None:
        """失败时移除本次新建的组合代（复用的代保留）。"""
        if created:
            shutil.rmtree(gen_dir, ignore_errors=True)

    # ── 源解析 ──

    def _resolve_repo(self, distro_root: Path, distro_toml: DistroToml) -> Path:
        """只读解析；远程仓只使用已有缓存，状态和 --check 不隐式联网。"""
        source = self._repo_source(distro_toml)
        if source.remote:
            return RemoteRepository(self._root, source).cached()
        return Path(source.location)

    def _repo_source(self, distro_toml: DistroToml) -> RepositorySource:
        """显式参数 > 环境 > 实例声明，远程 URL 不混入 path 字段。"""
        if self._repo is not None:
            return RepositorySource.parse(self._repo)
        env = os.environ.get("GHRAH_MONOREPO_ROOT")
        if env:
            return RepositorySource.parse(env)
        if distro_toml.repo_url:
            source = RepositorySource.parse(distro_toml.repo_url)
            if not source.remote:
                raise RuntimeRefError("distro.repo.url 必须是 Git URL")
            return source
        if distro_toml.repo_path:
            return RepositorySource.parse(distro_toml.repo_path)
        raise RuntimeRefError(
            "未指定运行时仓：提供 --repo、设置 env GHRAH_MONOREPO_ROOT 或设置 distro.repo.path/url"
        )

    @staticmethod
    def _coerce_source(source: str | RefSource | None, distro_toml: DistroToml) -> RefSource:
        if isinstance(source, RefSource):
            return source
        return RefSource.parse(source if source is not None else distro_toml.ref_source)

    def _resolve_candidate(self, source: RefSource, *, fetch: bool, repo: Path) -> str:
        """解析候选 commit 并经 ``rev-parse --verify <x>^{commit}`` 归一化（peel 注解标签）。"""
        require_git()
        if fetch:
            cp = self._run(["git", "-C", str(repo), "fetch", "--all"], check=False)
            if cp.returncode != 0:
                raise RuntimeRefError(f"git fetch 失败，未切换版本: {cp.stderr.strip()}")
        if source.kind == "latest" and (repo / "HEAD").is_file():
            # bare 远程缓存的默认分支，不以其他试验分支的日期决定安装目标。
            sha = "HEAD"
        elif source.kind == "latest":
            # 注解标签的 %(objectname) 是 tag 对象，须 peel 成 commit
            out = self._run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "for-each-ref",
                    "--sort=-committerdate",
                    "--count=1",
                    "--format=%(if)%(*objectname)%(then)%(*objectname)%(else)%(objectname)%(end)",
                ]
            ).stdout.strip()
            if not out:
                raise RuntimeRefError(f"仓库无 ref 可解析 latest: {repo}")
            sha = out.splitlines()[0].strip()
        else:
            sha = source.value or ""
            if source.kind == "branch":
                sha = f"refs/heads/{sha}"
            elif source.kind == "tag":
                sha = f"refs/tags/{sha}"
        cp = self._run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{sha}^{{commit}}",
            ],
            check=False,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            raise RuntimeRefError(
                f"候选 ref 解析失败: {source.render()}（{cp.stderr.strip() or '未知 ref'}）"
            )
        return cp.stdout.strip()

    # ── 版本命名与建树 ──

    def _derive_version_name(self, repo: Path, commit: str) -> str:
        out = self._run(
            ["git", "-C", str(repo), "show", "-s", "--format=%cd", "--date=format:%Y.%m", commit]
        ).stdout.strip()
        if not out:
            raise RuntimeRefError(f"无法读取候选提交日期: {commit}")
        return out

    def _claim_version_name(
        self, distro: str, base_ver: str, commit: str, repo: Path
    ) -> tuple[str, bool]:
        """裁定版本名：同名同 commit 复用；同名不同 commit 追加 ``-<short7>``；仍冲突报错。"""
        candidates = [base_ver]
        layout = DistroLayout(root=self._root / distro)
        if (layout.versions / base_ver).is_dir():
            short = self._run(
                ["git", "-C", str(repo), "rev-parse", "--short=7", commit]
            ).stdout.strip()
            if short:
                candidates.append(f"{base_ver}-{short}")
        for ver in candidates:
            tree = layout.versions / ver
            if not tree.is_dir():
                return ver, False
            if self._version_identity(tree) == commit:
                return ver, True
        raise RuntimeRefError(
            f"版本名冲突: {layout.versions / candidates[-1]} 已存在且 commit 不同（版本名不可变）"
        )

    def _find_version_by_commit(self, distro: str, commit: str) -> str | None:
        """复用已装 commit（回退/重复 install 不建重复树——「已装不动」）。

        身份严格取 BOOTSTRAP.toml（packages 形态唯一身份）。
        """
        layout = DistroLayout(root=self._root / distro)
        for ver in layout.list_versions():
            if self._version_identity(layout.versions / ver) == commit:
                return ver
        return None

    def _create_version_tree(self, distro: str, ver: str, commit: str, repo: Path) -> Path:
        """建版本树：物化即构建（产物树，wheels/tarballs/observer-web/BOOTSTRAP）。"""
        layout = DistroLayout(root=self._root / distro)
        tree = layout.versions / ver
        tree.parent.mkdir(parents=True, exist_ok=True)
        if tree.exists():
            if self._version_identity(tree) == commit:
                return tree
            raise RuntimeRefError(f"版本目录已存在且 commit 不同: {tree}")
        # 物化即构建：staging 隔离 + os.replace 原子落位；
        # 失败时 staging 清理、目标不存在——「失败 = 什么都没发生」。
        try:
            build_artifacts(repo, commit, out_dir=tree)
        except ToolchainError as e:
            raise RuntimeRefError(f"建版本树失败 {ver}: {e}") from e
        return tree

    def _cleanup_tree(self, created: bool, tree: Path) -> None:
        """失败时移除本次新建的候选基座树（既有树保留——失败 = 什么都没发生）。"""
        if not created:
            return
        shutil.rmtree(tree, ignore_errors=True)

    def _version_identity(self, tree: Path) -> str | None:
        """版本树身份（复用判定面）：BOOTSTRAP.toml source_commit。"""
        return read_source_commit(tree)

    def _tree_commit(self, tree: Path) -> str | None:
        """显示面身份：BOOTSTRAP.toml source_commit（无 git 回退）。"""
        return read_source_commit(tree)

    def _current_entry(self, distro: str) -> BootEntry | None:
        layout = DistroLayout(root=self._root / distro)
        if not layout.current.exists():
            return None
        tree = layout.current.resolve()
        self._entry_kind(layout, tree)
        info = read_generation(tree)
        return BootEntry(
            distro=distro,
            version=tree.name,
            path=tree,
            commit=info.base_commit if info else None,
            kind="generation",
            base_version=info.base_version if info else "",
        )

    @staticmethod
    def _entry_kind(layout: DistroLayout, tree: Path) -> tuple[str, str]:
        """判定 current 指向的树形态（generations-only 逃逸校验）。

        current/last-good 只允许指向 ``generations/<gen-id>``；
        两段之外（含 ``versions/``）→ ``RuntimeRefError``（fail-closed）。
        返回 (kind, base_version) 以兼容调用面。
        """
        try:
            gen_rel = tree.relative_to(layout.generations.resolve())
        except ValueError, OSError:
            gen_rel = None
        if gen_rel is not None and len(gen_rel.parts) == 1:
            return "generation", ""
        raise RuntimeRefError(
            f"current 指向 generations/ 之外的路径: {tree}"
            "（引导目标只允许组合代；versions/ 为基座产物库，不可引导）"
        )

    # ── 门禁 ──

    def _gate(self, gen_dir: Path, level: str) -> str:
        """basic 门禁：在组合代 env 上冒烟。

        代物化本身已含「可装」，此处补「可 import + 入口在」。门禁绝不触碰
        任何发行版级 ``env/`。
        """
        if level in ("full", "data"):
            raise GateError(f"门禁级别 {level} 未启用（尚未实现）")
        if level != "basic":
            raise GateError(f"未知门禁级别: {level}（应为 basic | full | data）")
        try:
            self._gate_generation(gen_dir)
        except ToolchainError as e:
            raise GateError(f"门禁 basic 未通过（代冒烟失败）: {e}") from e
        return "passed"

    def _gate_generation(self, gen_dir: Path) -> None:
        """代 env 冒烟：import 五包 + ``ghrah-subject-server`` 入口存在。

        入口为存在性校验（真实入口 main() 不解析参数，``--help`` 会直接启动
        服务器占端口；启动冒烟属 boot 域验证）。
        """
        venv = gen_dir / "env" / ".venv"
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not py.is_file():
            raise GateError(f"代 env 解释器缺失: {py}（代未完整物化）")
        cp = self._run(
            [
                str(py),
                "-c",
                "import ghrah.core, ghrah.protocol, ghrah.subject, ghrah.plugin, ghrah.taskstore",
            ],
            check=False,
        )
        if cp.returncode != 0:
            raise GateError(
                f"代 env import 冒烟失败: {cp.stderr.strip()[-800:] or cp.stdout.strip()[-800:]}"
            )
        sub = "Scripts" if os.name == "nt" else "bin"
        suffix = ".exe" if os.name == "nt" else ""
        server = venv / sub / f"ghrah-subject-server{suffix}"
        if not server.is_file():
            raise GateError(f"代 env 缺少 ghrah-subject-server 入口: {server}")

    # ── 快照（委托 snapshot 模块） ──

    def _snapshot(
        self,
        distro_root: Path,
        distro_toml: DistroToml,
        old: BootEntry,
        new: BootEntry,
        source: RefSource,
        gate: str,
    ) -> Path | None:
        layout = DistroLayout(root=distro_root)
        snap = snapshot.take(
            layout,
            distro_toml,
            old_entry=old,
            new_entry=new,
            source=source.render(),
            gate=gate,
        )
        if snap is not None:
            snapshot.roll(layout, distro_toml, distro_toml.snapshot_keep)
        return snap

    # ── 代复核 ──

    def _sync(self, distro: str, ver: str, *, force: bool = False) -> None:
        """代复核：重指后校验 ``generations/<ver>`` 指纹与完整性，不一致/缺代才重建物化。

        per-gen env 各代隔离，正常路径零操作（单形态，无发行级 env sync）。
        """
        self._sync_generation(distro, ver, force=force)

    def _sync_generation(self, distro: str, ver: str, *, force: bool) -> None:
        """代复核：指纹与完整性均达标即零操作；否则按代声明重建物化。"""
        gen_dir = self._root / distro / "generations" / ver
        info = read_generation(gen_dir)
        if info is None:
            raise RuntimeRefError(f"代缺失或不可读: {gen_dir}（先完成 install）")
        base_tree = self._root / distro / "versions" / info.base_version
        spec = GenerationSpec(
            base_version=info.base_version,
            base_commit=info.base_commit,
            pins_sha256=info.pins_sha256,
            plugins=info.plugins,
        )
        consistent = (
            not force
            and info.fingerprint == generation_fingerprint(spec)
            and generation_complete(gen_dir)
        )
        if not consistent:
            logger.warning("代指纹/完整性不达标，重建物化: %s", gen_dir)
            materialize_generation(self._root / distro, base_tree, spec, rebuild=True)

    # ── boot.toml 写入与回退 ──

    def _write_boot_toml(
        self, boot_before: BootToml, old: BootEntry | None, new: BootEntry
    ) -> None:
        fallback = boot_before.fallback
        if boot_before.default and boot_before.default != new.label:
            fallback = boot_before.default
        last_good = boot_before.last_good
        if old is None and last_good is None:
            last_good = new.label
        BootToml(default=new.label, fallback=fallback, last_good=last_good).save(self._root)

    def _rollback(self, distro: str, old: BootEntry | None, boot_before: BootToml) -> None:
        distro_root = self._root / distro
        if old is not None:
            repoint_current(distro_root, old.version)
            self._sync(distro, old.version, force=True)
        else:
            current = distro_root / "current"
            with contextlib.suppress(OSError):
                current.unlink()
        boot_before.save(self._root)

    def _log_change(
        self, old: BootEntry | None, new: BootEntry, source: RefSource, gate: str
    ) -> None:
        logger.info(
            "版本变更: %s → %s (commit=%s source=%s gate=%s)",
            old.label if old is not None else "∅",
            new.label,
            new.commit,
            source.render(),
            gate,
        )

    def _guard_not_running(self, distro_toml: DistroToml) -> None:
        if port_in_use(DEFAULT_HOST, distro_toml.subject_port):
            raise RuntimeRefError(
                f"端口 {distro_toml.subject_port} 被占用（OS 运行中？）——"
                "冷切换要求先执行 ghrah distro stop NAME"
            )

    # ── 子进程统一入口（便于测试 monkeypatch） ──

    @staticmethod
    def _run(
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            # 子进程输出 UTF-8；中文 Windows 缺省 GBK 解码会崩读线程
            # （stderr 变 None → 报错信息丢失），显式钉 UTF-8 + 容错替换。
            encoding="utf-8",
            errors="replace",
            check=check,
        )
