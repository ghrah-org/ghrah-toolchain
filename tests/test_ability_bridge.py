# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ability_bridge（薄桥）单元测试：解释器解析、模块探测与子进程转调。

不依赖真实 ghrah-core：``module_available`` 一律用 monkeypatch 换成查表假实现，
子进程调用点用 monkeypatch 记录 argv；所有 ``~/.ghrah`` 读写都经 ``GHRAH_HOME``
重定向到 ``tmp_path``，不碰真实用户目录。
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from ghrah_toolchain import ability_bridge
from ghrah_toolchain.ability_bridge import (
    TOOLS_BUILD,
    TOOLS_CLI,
    AbilityRuntime,
    default_distro,
    legacy_user_store_path,
    module_available,
    resolve_build_module,
    resolve_build_runtime,
    resolve_store_runtime,
    run_module,
    warn_legacy_store,
)
from ghrah_toolchain.errors import AbilityError, LayoutError

# ── 夹具与辅助 ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """隔离宿主环境：清 VIRTUAL_ENV、GHRAH_HOME 指向 tmp、cwd 换成独立项目根。"""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("GHRAH_HOME", str(root))
    work = tmp_path / "cwd"
    work.mkdir()
    # 项目根标记：cwd 祖先扫描到此为止，避免依赖测试机 /tmp 之上的真实布局。
    (work / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    monkeypatch.chdir(work)
    return root


@pytest.fixture
def fake_modules(monkeypatch: pytest.MonkeyPatch):
    """把 ``module_available`` 换成按解释器路径查表的假实现，返回登记函数。"""
    table: dict[str, set[str]] = {}

    def available(python: Path, module: str) -> bool:
        return module in table.get(str(python), set())

    monkeypatch.setattr(ability_bridge, "module_available", available)

    def allow(python: Path, *modules: str) -> None:
        table.setdefault(str(python), set()).update(modules)

    return allow


def _venv_python(venv: Path) -> Path:
    """与产品同口径的 venv 解释器路径（测试同时建两平台文件，不按 os.name 分支）。"""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _make_interpreter(venv: Path, *, executable: bool = True) -> Path:
    """建 venv 的两个平台解释器占位文件；POSIX 下按 ``executable`` 设可执行位。"""
    python = _venv_python(venv)
    for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("", encoding="utf-8")
    if os.name != "nt" and executable:
        python.chmod(0o755)
    return python


def _make_distro_venv(root: Path, distro: str) -> Path:
    """建 ``<distro>/current/env/.venv`` 并返回其解释器路径。"""
    venv = root / distro / "current" / "env" / ".venv"
    return _make_interpreter(venv)


def _write_boot(root: Path, default: str | None) -> None:
    """写最小 boot.toml（只关心 default 引导项）。"""
    content = f'default = "{default}"\n' if default is not None else ""
    (root / "boot.toml").write_text(content, encoding="utf-8")


def _distro_python(root: Path, distro: str) -> Path:
    """默认发行版候选解释器的期望路径（不落盘，用于断言错误文案）。"""
    return _venv_python(root / distro / "current" / "env" / ".venv")


# ── AbilityRuntime 与默认发行版 ──────────────────────────────


def test_ability_runtime_is_frozen() -> None:
    runtime = AbilityRuntime(python=Path("/x/python"), ability_root=None, distro=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        runtime.python = Path("/y")


def test_default_distro_reads_first_segment(home: Path) -> None:
    _write_boot(home, "generic/2026.1")
    assert default_distro() == "generic"


def test_default_distro_without_default_lists_available(home: Path) -> None:
    _write_boot(home, None)
    (home / "alpha").mkdir()
    (home / "beta").mkdir()
    with pytest.raises(AbilityError) as exc:
        default_distro()
    message = str(exc.value)
    assert "未配置默认发行版" in message
    assert "alpha" in message
    assert "beta" in message


def test_default_distro_missing_boot_toml(home: Path) -> None:
    with pytest.raises(AbilityError, match="未配置默认发行版"):
        default_distro()


# ── build 运行时解析 ─────────────────────────────────────────


def test_build_runtime_prefers_explicit_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    explicit = _make_interpreter(tmp_path / "explicit")
    env_python = _make_interpreter(tmp_path / "env-venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "env-venv"))
    fake_modules(explicit, TOOLS_BUILD)
    fake_modules(env_python, TOOLS_BUILD)

    runtime = resolve_build_runtime(str(explicit))

    assert runtime.python == explicit
    assert runtime.ability_root is None
    assert runtime.distro is None


def test_build_runtime_uses_virtual_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    venv = tmp_path / "env-venv"
    python = _make_interpreter(venv)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    fake_modules(python, TOOLS_CLI)

    assert resolve_build_runtime().python == python


def test_build_runtime_finds_cwd_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    python = _make_interpreter(project / ".venv")
    work = project / "src" / "pkg"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    fake_modules(python, TOOLS_BUILD)

    assert resolve_build_runtime().python == python


def test_build_runtime_stops_at_git_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules, home: Path
) -> None:
    outer = tmp_path / "outer"
    outer_python = _make_interpreter(outer / ".venv")
    project = outer / "project"
    project.mkdir()
    (project / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    work = project / "deep"
    work.mkdir()
    monkeypatch.chdir(work)
    # 外层 .venv 可用，但它在项目根之上，不应被纳入候选。
    fake_modules(outer_python, TOOLS_BUILD)
    _write_boot(home, None)

    with pytest.raises(AbilityError) as exc:
        resolve_build_runtime()

    message = str(exc.value)
    assert "未找到 .venv 目录" in message
    assert str(outer_python) not in message


def test_build_runtime_falls_back_to_default_distro(home: Path, fake_modules) -> None:
    _write_boot(home, "generic/2026.1")
    python = _make_distro_venv(home, "generic")
    fake_modules(python, TOOLS_BUILD)

    runtime = resolve_build_runtime()

    assert runtime.python == python
    assert runtime.distro == "generic"
    assert runtime.ability_root is None


def test_build_runtime_explicit_missing_module_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    explicit = _make_interpreter(tmp_path / "explicit")
    env_python = _make_interpreter(tmp_path / "env-venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "env-venv"))
    # 环境候选本可用，但显式 --python 失败必须立即报错、不回退。
    fake_modules(env_python, TOOLS_BUILD)

    with pytest.raises(AbilityError) as exc:
        resolve_build_runtime(str(explicit))

    message = str(exc.value)
    assert str(explicit) in message
    assert "不自动回退" in message


def test_build_runtime_no_candidates_lists_every_attempt(home: Path) -> None:
    _write_boot(home, "generic/2026.1")

    with pytest.raises(AbilityError) as exc:
        resolve_build_runtime()

    message = str(exc.value)
    assert "--python 未提供" in message
    assert "$VIRTUAL_ENV 未设置" in message
    assert "未找到 .venv 目录" in message
    assert str(_distro_python(home, "generic")) in message
    assert "解释器不存在" in message


def test_build_runtime_without_default_lists_distros(home: Path) -> None:
    _write_boot(home, None)
    (home / "alpha").mkdir()

    with pytest.raises(AbilityError) as exc:
        resolve_build_runtime()

    message = str(exc.value)
    assert "未配置默认发行版" in message
    assert "alpha" in message


@pytest.mark.skipif(os.name == "nt", reason="POSIX 可执行位语义")
def test_build_runtime_rejects_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    venv = tmp_path / "venv"
    python = _make_interpreter(venv, executable=False)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    fake_modules(python, TOOLS_BUILD)

    with pytest.raises(AbilityError, match="不可执行"):
        resolve_build_runtime()


@pytest.mark.skipif(os.name == "nt", reason="Windows 创建符号链接需额外权限")
def test_symlinked_venv_python_kept_and_forwarded_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_modules
) -> None:
    base = tmp_path / "base-python"
    base.write_text("", encoding="utf-8")
    base.chmod(0o755)
    venv = tmp_path / "venv"
    link = _venv_python(venv)
    link.parent.mkdir(parents=True)
    link.symlink_to(base)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    fake_modules(link, TOOLS_BUILD)

    runtime = resolve_build_runtime()

    # 绝不 resolve()：传子进程的必须是 venv 路径，否则丢掉 sys.prefix。
    assert runtime.python == link
    assert runtime.python.is_symlink()
    assert str(runtime.python) != str(base)

    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ability_bridge.subprocess, "run", fake_run)
    assert run_module(runtime.python, TOOLS_BUILD, ["x"]) == 0
    assert recorded[0][0] == str(link)


# ── build 入口模块选择 ───────────────────────────────────────


def test_resolve_build_module_prefers_tools_build(tmp_path: Path, fake_modules) -> None:
    python = _make_interpreter(tmp_path / "venv")
    fake_modules(python, TOOLS_BUILD, TOOLS_CLI)

    assert resolve_build_module(python) == TOOLS_BUILD


def test_resolve_build_module_falls_back_to_tools_cli(tmp_path: Path, fake_modules) -> None:
    python = _make_interpreter(tmp_path / "venv")
    fake_modules(python, TOOLS_CLI)

    assert resolve_build_module(python) == TOOLS_CLI


def test_resolve_build_module_missing_both(tmp_path: Path, fake_modules) -> None:
    python = _make_interpreter(tmp_path / "venv")

    with pytest.raises(AbilityError) as exc:
        resolve_build_module(python)

    message = str(exc.value)
    assert "缺少 Ability 工具" in message
    assert TOOLS_BUILD in message
    assert TOOLS_CLI in message


# ── store 运行时解析 ─────────────────────────────────────────


def test_store_runtime_uses_default_distro(home: Path, fake_modules) -> None:
    _write_boot(home, "lab/2026.1")
    python = _make_distro_venv(home, "lab")
    fake_modules(python, TOOLS_CLI)

    runtime = resolve_store_runtime()

    assert runtime.python == python
    assert runtime.distro == "lab"
    assert runtime.ability_root == home / "lab" / "ability"
    assert runtime.ability_root.is_absolute()


def test_store_runtime_distro_override(home: Path, fake_modules) -> None:
    _write_boot(home, "lab/2026.1")
    python = _make_distro_venv(home, "other")
    fake_modules(python, TOOLS_CLI)

    runtime = resolve_store_runtime(distro="other")

    assert runtime.python == python
    assert runtime.distro == "other"
    assert runtime.ability_root == home / "other" / "ability"


def test_store_runtime_explicit_python_does_not_fall_back(home: Path, fake_modules) -> None:
    _write_boot(home, "lab/2026.1")
    fake_modules(_make_distro_venv(home, "lab"), TOOLS_CLI)
    explicit = _make_interpreter(home / "explicit-venv")

    with pytest.raises(AbilityError) as exc:
        resolve_store_runtime(python=str(explicit))

    message = str(exc.value)
    assert str(explicit) in message
    assert "不自动回退" in message


def test_store_runtime_requires_tools_cli(home: Path, fake_modules) -> None:
    python = _make_distro_venv(home, "lab")
    fake_modules(python, TOOLS_BUILD)  # 只有 build 入口，没有统一入口

    with pytest.raises(AbilityError) as exc:
        resolve_store_runtime(distro="lab")

    message = str(exc.value)
    assert TOOLS_CLI in message
    assert str(python) in message
    assert "P2/P5" in message


def test_store_runtime_missing_distro_reports_install_hint(home: Path) -> None:
    with pytest.raises(AbilityError) as exc:
        resolve_store_runtime(distro="ghost")

    message = str(exc.value)
    assert "发行版目录不存在" in message
    assert "ghrah distro install" in message


def test_store_runtime_missing_current_reports_install_hint(home: Path) -> None:
    (home / "lab").mkdir()

    with pytest.raises(AbilityError) as exc:
        resolve_store_runtime(distro="lab")

    assert "current 不存在" in str(exc.value)


def test_store_runtime_rejects_reserved_ability_name(home: Path) -> None:
    with pytest.raises(LayoutError, match="保留名"):
        resolve_store_runtime(distro="ability")


# ── 模块探测 ─────────────────────────────────────────────────


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_module_available_uses_probe_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, expected: bool
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(ability_bridge.subprocess, "run", fake_run)
    python = tmp_path / "python"

    assert module_available(python, TOOLS_CLI) is expected
    assert recorded["cmd"][0] == str(python)
    assert recorded["cmd"][1] == "-c"
    assert recorded["cmd"][-1] == TOOLS_CLI
    assert recorded["kwargs"]["check"] is False
    assert "timeout" in recorded["kwargs"]


@pytest.mark.parametrize(
    "error",
    [OSError("无法启动"), subprocess.TimeoutExpired("python", 30.0)],
    ids=["oserror", "timeout"],
)
def test_module_available_treats_launch_errors_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    def fake_run(cmd, **kwargs):
        raise error

    monkeypatch.setattr(ability_bridge.subprocess, "run", fake_run)

    assert module_available(tmp_path / "python", TOOLS_BUILD) is False


# ── 子进程转调 ───────────────────────────────────────────────


def test_run_module_passes_argv_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 7)

    monkeypatch.setattr(ability_bridge.subprocess, "run", fake_run)
    python = tmp_path / "python"

    code = run_module(python, TOOLS_CLI, ["install", "/pkg", "--root", "/r"])

    assert code == 7
    assert recorded["cmd"] == [str(python), "-m", TOOLS_CLI, "install", "/pkg", "--root", "/r"]
    # 只传 check=False：继承宿主 stdio/stdin/环境，不 capture、不注入 env、不设超时。
    assert recorded["kwargs"] == {"check": False}


def test_run_module_propagates_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(cmd, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(ability_bridge.subprocess, "run", fake_run)

    with pytest.raises(OSError):
        run_module(tmp_path / "python", TOOLS_CLI, [])


# ── 旧用户级存储提示 ─────────────────────────────────────────


def test_warn_legacy_store_silent_when_absent(home: Path, capsys: pytest.CaptureFixture) -> None:
    assert legacy_user_store_path() is None
    assert warn_legacy_store() is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_warn_legacy_store_reports_to_stderr_only(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    legacy = home / "ability"
    legacy.mkdir()

    assert legacy_user_store_path() == legacy
    assert warn_legacy_store() is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(legacy) in captured.err
    assert "已不再使用" in captured.err
