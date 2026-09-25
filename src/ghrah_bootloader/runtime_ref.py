# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ref 状态机：源解析 → 建版本树 → 门禁 → 快照 → 原子重指 current → 按需 sync。

纯 stdlib（subprocess / pathlib / dataclasses / tomllib / hashlib / tempfile）。

双安装模式（``[distro] install_mode``）：

- ``worktree``：版本树 = ``git worktree add --detach``（旧链路，过渡期保留）；
- ``packages``（缺省）：版本树 = B1 构建产物（wheels/tarballs/manifest/BOOTSTRAP.toml），
  身份 = ``BOOTSTRAP.toml source_commit``（纯文件零 git），门禁 = ``verify_artifacts``，
  sync = no-op（env 语义由 B3 per-gen env 重建）。

切换顺序固定（失败 = 什么都没发生）：

```
guard(运行中拒绝) → lock → 解析候选 commit → 建树/复用
  → gate（临时 env / 产物验证，绝不触碰发行版 env/）
  → snapshot（存在旧 current 时无条件拍）
  → repoint current（原子）→ 按需 sync + 复核自管 .pth（worktree 模式）
  → 写 boot.toml → 释放锁
```

- gate/snapshot 失败：current / env / boot.toml 未动；本次新建的候选树移除。
- sync 及其后失败：重指回旧版本 + 旧版本强制重 sync + 回写 boot.toml +
  移除本次新建树 → ``RuntimeRefError``。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ghrah_bootloader import snapshot
from ghrah_bootloader.build import build_artifacts, read_source_commit, verify_artifacts
from ghrah_bootloader.errors import BootloaderError, GateError, RuntimeRefError
from ghrah_bootloader.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    managed_pth_path,
    resolve_distro_root,
    verify_managed_pth,
    write_managed_pth,
)
from ghrah_bootloader.lifecycle import DEFAULT_HOST, port_in_use

logger = logging.getLogger(__name__)

_GATE_LEVELS = ("basic", "full", "data")
_STALE_LOCK_SECONDS = 600.0


# ── 锁（D8） ─────────────────────────────────────────────────


def distro_lock_path(distro_root: Path) -> Path:
    """发行级锁文件路径。"""
    return distro_root / ".bootloader.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "posix":
        # Windows 无可移植的 pid 存活探测（os.kill 语义不同），依赖超时回收
        return True
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
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return True
    return age > stale_after or not _pid_alive(pid)


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
                f"另一引导器操作正在运行（锁: {path}）；请等待其完成或先 stop 后重试"
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


def repoint_current(distro_root: Path, ver: str) -> None:
    """原子重指 ``current``：``current.tmp-<pid>`` 相对符号链接 + ``os.replace``。"""
    tmp = distro_root / f"current.tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(f"versions/{ver}")
    os.replace(tmp, distro_root / "current")


def repoint_last_good(distro_root: Path, ver: str) -> None:
    """原子建/更新 ``last-good`` 符号链接。"""
    tmp = distro_root / f"last-good.tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(f"versions/{ver}")
    os.replace(tmp, distro_root / "last-good")


# ── 数据类型 ─────────────────────────────────────────────────


@dataclass
class RefSource:
    """签出源：``latest``（全局最近 ref tip）| ``branch:<name>`` | ``commit:<sha>``。"""

    kind: str  # latest / branch / commit
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
            if kind in ("branch", "commit") and value:
                return cls(kind=kind, value=value)
        raise RuntimeRefError(f"非法 ref 源: {raw!r}（应为 latest | branch:<n> | commit:<sha>）")

    def render(self) -> str:
        if self.kind == "latest":
            return "latest"
        return f"{self.kind}:{self.value}"


@dataclass
class BootEntry:
    """一个已安装的引导项（发行版 / 版本 / 树 / commit）。"""

    distro: str
    version: str
    path: Path
    commit: str | None

    @property
    def label(self) -> str:
        return f"{self.distro}/{self.version}"


# ── 状态机 ───────────────────────────────────────────────────


class RuntimeRefManager:
    """GPT 枚举 / 源解析 / 建树 / 门禁 / 快照 / 重指 / sync 的统一状态机。"""

    def __init__(self, *, root: Path | None = None, repo: Path | None = None) -> None:
        self._root = root or ghrah_root()
        self._repo = repo

    # ── 只读面 ──

    def list_entries(self) -> list[BootEntry]:
        """枚举全部发行版 × 版本（commit 取各树 ``git rev-parse HEAD``，失败置 None）。"""
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
    ) -> BootEntry:
        """安装/切换到候选 commit 并重指 ``current``（D5 顺序；全程持锁）。"""
        boot_before = BootToml.load(self._root)
        d = distro or (boot_before.default.split("/", 1)[0] if boot_before.default else "")
        if not d:
            raise RuntimeRefError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
        distro_root = resolve_distro_root(d, self._root)  # 校验发行版名
        distro_toml = DistroToml.load(distro_root)
        self._guard_not_running(distro_toml)
        old = self._current_entry(d)
        if update and old is None:
            raise RuntimeRefError(
                f"发行版 {d} 尚未安装（无 current）；请先 ghrah-bootloader install"
            )
        with acquire_distro_lock(distro_root):
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
        del update  # 语义检查已在入口完成（无 current 的 update 被拒绝）
        distro_root.mkdir(parents=True, exist_ok=True)
        repo = self._resolve_repo(distro_root, distro_toml)
        mode = distro_toml.install_mode
        src = self._coerce_source(source, distro_toml)
        commit = self._resolve_candidate(src, fetch=fetch, repo=repo)

        reused_ver = self._find_version_by_commit(distro, commit, mode=mode)
        if reused_ver is not None:
            ver, tree, created = reused_ver, distro_root / "versions" / reused_ver, False
        else:
            base_ver = version or self._derive_version_name(repo, commit)
            ver, reused = self._claim_version_name(distro, base_ver, commit, repo, mode=mode)
            tree = distro_root / "versions" / ver
            created = not reused
            if created:
                self._create_version_tree(distro, ver, commit, repo, mode=mode)

        new_entry = BootEntry(distro=distro, version=ver, path=tree, commit=commit)
        try:
            self._gate(tree, gate, mode=mode)
        except Exception:
            self._cleanup_tree(created, tree, repo, mode=mode)
            raise
        try:
            if old is not None:
                self._snapshot(distro_root, distro_toml, old, new_entry, src, gate)
        except Exception:
            self._cleanup_tree(created, tree, repo, mode=mode)
            raise
        try:
            repoint_current(distro_root, ver)
            self._sync(distro, ver, mode=mode)
            self._write_boot_toml(boot_before, old, new_entry)
            if old is None:
                repoint_last_good(distro_root, ver)
        except Exception as e:
            logger.error("切换失败，开始回退: %s", e)
            try:
                self._rollback(distro, old, boot_before, mode=mode)
            except Exception:
                logger.error("回退失败（人工介入检查 current/env/boot.toml）", exc_info=True)
            self._cleanup_tree(created, tree, repo, mode=mode)
            target = old.label if old is not None else "（无版本）"
            raise RuntimeRefError(f"切换到 {new_entry.label} 失败，已回退至 {target}: {e}") from e
        self._log_change(old, new_entry, src, gate)
        return new_entry

    # ── 源解析（D9/D10） ──

    def _resolve_repo(self, distro_root: Path, distro_toml: DistroToml) -> Path:
        """仓路径优先级：``--repo`` > env ``GHRAH_MONOREPO_ROOT`` > ``distro.toml`` > 报错。"""
        if self._repo is not None:
            return self._repo
        env = os.environ.get("GHRAH_MONOREPO_ROOT")
        if env:
            return Path(env).expanduser()
        if distro_toml.repo_path:
            return Path(distro_toml.repo_path).expanduser()
        raise RuntimeRefError(
            "未指定运行时仓：提供 --repo、设置 env GHRAH_MONOREPO_ROOT "
            f"或在 {distro_root / 'distro.toml'} 写 [distro.repo].path"
        )

    @staticmethod
    def _coerce_source(source: str | RefSource | None, distro_toml: DistroToml) -> RefSource:
        if isinstance(source, RefSource):
            return source
        return RefSource.parse(source if source is not None else distro_toml.ref_source)

    def _resolve_candidate(self, source: RefSource, *, fetch: bool, repo: Path) -> str:
        """解析候选 commit 并经 ``rev-parse --verify <x>^{commit}`` 归一化（peel 注解标签）。"""
        if fetch:
            cp = self._run(["git", "-C", str(repo), "fetch", "--all"], check=False)
            if cp.returncode != 0:
                logger.warning("git fetch 失败（按本地可见 ref 继续）: %s", cp.stderr.strip())
        if source.kind == "latest":
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
        cp = self._run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"],
            check=False,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            raise RuntimeRefError(
                f"候选 ref 解析失败: {source.render()}（{cp.stderr.strip() or '未知 ref'}）"
            )
        return cp.stdout.strip()

    # ── 版本命名与建树（D1） ──

    def _derive_version_name(self, repo: Path, commit: str) -> str:
        out = self._run(
            ["git", "-C", str(repo), "show", "-s", "--format=%cd", "--date=format:%Y.%m", commit]
        ).stdout.strip()
        if not out:
            raise RuntimeRefError(f"无法读取候选提交日期: {commit}")
        return out

    def _claim_version_name(
        self, distro: str, base_ver: str, commit: str, repo: Path, *, mode: str
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
            if self._version_identity(tree, mode) == commit:
                return ver, True
        raise RuntimeRefError(
            f"版本名冲突: {layout.versions / candidates[-1]} 已存在且 commit 不同（版本名不可变）"
        )

    def _find_version_by_commit(self, distro: str, commit: str, *, mode: str) -> str | None:
        """复用已装 commit（回退/重复 install 不建重复树——「已装不动」）。

        复用判定按模式严格取身份：packages 只认 BOOTSTRAP.toml，worktree 只认
        git——混域（手改模式后的旧形态树）不构成复用依据（fail-closed）。
        """
        layout = DistroLayout(root=self._root / distro)
        for ver in layout.list_versions():
            if self._version_identity(layout.versions / ver, mode) == commit:
                return ver
        return None

    def _create_version_tree(
        self, distro: str, ver: str, commit: str, repo: Path, *, mode: str
    ) -> Path:
        """建版本树：worktree = ``git worktree add --detach``；packages = B1 构建产物。"""
        layout = DistroLayout(root=self._root / distro)
        tree = layout.versions / ver
        tree.parent.mkdir(parents=True, exist_ok=True)
        if tree.exists():
            if self._version_identity(tree, mode) == commit:
                return tree
            raise RuntimeRefError(f"版本目录已存在且 commit 不同: {tree}")
        if mode == "packages":
            # 物化即构建：staging 隔离 + os.replace 原子落位（B1 机械）；
            # 失败时 staging 清理、目标不存在——「失败 = 什么都没发生」。
            try:
                build_artifacts(repo, commit, out_dir=tree)
            except BootloaderError as e:
                raise RuntimeRefError(f"建版本树失败 {ver}: {e}") from e
            return tree
        try:
            self._run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), commit])
        except subprocess.CalledProcessError as e:
            shutil.rmtree(tree, ignore_errors=True)
            raise RuntimeRefError(f"建版本树失败 {ver}: {e.stderr.strip()}") from e
        return tree

    def _cleanup_tree(self, created: bool, tree: Path, repo: Path, *, mode: str) -> None:
        """失败时移除本次新建的候选树（既有树保留——失败 = 什么都没发生）。"""
        if not created:
            return
        if mode == "packages":
            shutil.rmtree(tree, ignore_errors=True)
            return
        self._run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)], check=False)
        self._run(["git", "-C", str(repo), "worktree", "prune"], check=False)
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)

    def _version_identity(self, tree: Path, mode: str) -> str | None:
        """按模式取版本树身份（复用判定面）：packages=BOOTSTRAP；worktree=git。"""
        if mode == "packages":
            return read_source_commit(tree)
        cp = self._run(["git", "-C", str(tree), "rev-parse", "HEAD"], check=False)
        return cp.stdout.strip() if cp.returncode == 0 else None

    def _tree_commit(self, tree: Path) -> str | None:
        """显示面身份（双跳）：BOOTSTRAP.toml → git rev-parse → None。

        兼容存量域内 worktree 形态树（迁移期显示不缺失）；严格复用判定走
        :meth:`_version_identity`。
        """
        commit = read_source_commit(tree)
        if commit is not None:
            return commit
        cp = self._run(["git", "-C", str(tree), "rev-parse", "HEAD"], check=False)
        return cp.stdout.strip() if cp.returncode == 0 else None

    def _current_entry(self, distro: str) -> BootEntry | None:
        layout = DistroLayout(root=self._root / distro)
        if not layout.current.exists():
            return None
        tree = layout.current.resolve()
        try:
            tree.relative_to(layout.versions.resolve())
        except ValueError as e:
            raise RuntimeRefError(f"current 指向 versions/ 之外的路径: {tree}") from e
        return BootEntry(
            distro=distro, version=tree.name, path=tree, commit=self._tree_commit(tree)
        )

    # ── 门禁（D4） ──

    def _gate(self, tree: Path, level: str, *, mode: str = "worktree") -> str:
        """basic 门禁：worktree = 临时 env uv sync + import 冒烟；packages = 产物验证。

        门禁绝不触碰发行版 ``env/``（packages 的 verify_artifacts 亦在临时 venv 装配）。
        """
        if level in ("full", "data"):
            raise GateError(f"门禁级别 {level} 未启用（收束批次开启）")
        if level != "basic":
            raise GateError(f"未知门禁级别: {level}（应为 basic | full | data）")
        if mode == "packages":
            try:
                verify_artifacts(tree)
            except BootloaderError as e:
                raise GateError(f"门禁 basic 未通过（产物验证失败）: {e}") from e
            return "passed"
        tmp = Path(tempfile.mkdtemp(prefix="ghrah-gate-"))
        venv = tmp / "venv"
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
        try:
            try:
                self._run(["uv", "sync"], cwd=tree, env=env)
                self._run(
                    [str(py), "-c", "import ghrah.core, ghrah.protocol, ghrah.subject"],
                    cwd=tree,
                )
            except subprocess.CalledProcessError as e:
                raise GateError(
                    f"门禁 basic 未通过: {' '.join(map(str, e.cmd))}: {e.stderr.strip()}"
                ) from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return "passed"

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

    # ── 按需 sync（D2/D3） ──

    def _sync(self, distro: str, ver: str, *, force: bool = False, mode: str = "worktree") -> None:
        """依赖指纹变化才 uv sync / pnpm install；始终复核自管 ``.pth``。

        packages 模式 no-op：产物树无 env 语义可同步（per-gen env 属 B3）。
        """
        if mode == "packages":
            return
        layout = DistroLayout(root=self._root / distro)
        tree = layout.versions / ver
        fingerprint = self._deps_fingerprint(tree)
        fp_path = layout.env / ".deps-fingerprint"
        fp_old = fp_path.read_text(encoding="utf-8").strip() if fp_path.is_file() else None
        needs = force or fp_old != fingerprint
        if needs:
            env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(layout.venv)}
            self._run(["uv", "sync"], cwd=tree, env=env)
            self._remove_editable_artifacts(layout.venv)
            self._run(["pnpm", "install"], cwd=tree)
        if needs or not verify_managed_pth(layout.venv, layout.current):
            write_managed_pth(layout.venv, layout.current)
        if not needs:
            return
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(fingerprint + "\n", encoding="utf-8")
        py = layout.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # 断言导入路径源头含 current 段：__file__ 会被 realpath 解析成 versions/<ver>/...，
        # 故校验 sys.path 中的字面 current 段（.pth 未被 editable 工件遮蔽）。
        code = (
            "import sys, ghrah.core; "
            "hits = [p for p in sys.path if 'current' in p and 'ghrah-core' in p]; "
            "print('OK' if hits and ghrah.core.__file__ in "
            "{p + '/ghrah/core/__init__.py' for p in hits} else 'BAD')"
        )
        cp = self._run(
            [str(py), "-c", code],
            cwd=tree,
            check=False,
        )
        if cp.returncode != 0 or cp.stdout.strip() != "OK":
            raise RuntimeRefError(
                "自管 .pth 断言失败：ghrah.core 导入未经 current 段"
                f"（stdout={cp.stdout.strip()!r}）"
            )

    def _deps_fingerprint(self, tree: Path) -> str:
        """sha256(uv.lock + 根 pyproject.toml + ghrah-*/pyproject.toml + pnpm-lock.yaml)。"""
        rels = ["uv.lock", "pyproject.toml", "pnpm-lock.yaml"]
        files = [tree / r for r in rels]
        files += sorted(tree.glob("ghrah-*/pyproject.toml"))
        digest = hashlib.sha256()
        for path in sorted(files):
            if not path.is_file():
                continue
            digest.update(str(path.relative_to(tree)).encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _remove_editable_artifacts(self, venv: Path) -> None:
        """删除 site-packages 内引用 ghrah 包的 editable 工件。

        覆盖两种形态（计划 §五 R1 风险的实测落地）：
        - setuptools：``__editable__.<pkg>-<ver>.pth``（spike 时代形态）；
        - uv：``_editable_impl_<pkg>.pth``（内容为版本树绝对路径，
          按名排序先于自管 ``ghrah_bootloader_managed.pth`` 处理，
          会遮蔽 current 段——必须清除）。
        """
        site = managed_pth_path(venv).parent
        for artifact in site.glob("*editable*"):
            if artifact.name == "ghrah_bootloader_managed.pth":
                continue
            try:
                content = artifact.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            if "ghrah" in artifact.name.lower() or "ghrah" in content:
                artifact.unlink(missing_ok=True)

    # ── boot.toml 写入与回退（D5/D7/D12） ──

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

    def _rollback(
        self, distro: str, old: BootEntry | None, boot_before: BootToml, *, mode: str = "worktree"
    ) -> None:
        distro_root = self._root / distro
        if old is not None:
            repoint_current(distro_root, old.version)
            self._sync(distro, old.version, force=True, mode=mode)
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
                "冷切换要求先停止运行中的实例（boot 终端 Ctrl-C）"
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
            check=check,
        )
