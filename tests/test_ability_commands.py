# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""``ghrah ability`` 命令面：解析器、薄桥参数映射、JSON 信封与失败路径。

不依赖真实 ghrah-core：薄桥的运行时解析与子进程转调一律 monkeypatch，用例只断言
工具链自己拼出的 argv、退出码与输出通道（stdout 只留给 core/错误信封，提示走 stderr）。
所有用例都走 ``GHRAH_HOME`` 临时安装域，不触碰真实 ``~/.ghrah``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghrah_toolchain.ability_bridge import TOOLS_BUILD, TOOLS_CLI, AbilityRuntime
from ghrah_toolchain.cli import _build_parser, main
from ghrah_toolchain.commands import ability as ability_cmd
from ghrah_toolchain.errors import AbilityError

SEVEN = ("build", "install", "list", "show", "verify", "refresh", "uninstall")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时安装域：所有命令只读写它（GHRAH_HOME），并隔离作者环境的 VIRTUAL_ENV。"""
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    return h


@pytest.fixture(autouse=True)
def _unreachable_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认让真实解析/转调不可达；需要桥参数的用例各自替换成记录器。"""

    def unreachable(*args, **kwargs):
        raise AssertionError("本用例不应触达真实运行时解析或子进程转调")

    for name in (
        "resolve_build_runtime",
        "resolve_build_module",
        "resolve_store_runtime",
        "run_module",
    ):
        monkeypatch.setattr(ability_cmd, name, unreachable)


def _runtime(tmp_path: Path, *, distro: str = "generic") -> AbilityRuntime:
    """构造一个不含真实解释器的目标运行时视图（字段按薄桥契约）。"""
    return AbilityRuntime(
        python=tmp_path / "env" / "bin" / "python",
        ability_root=tmp_path / "ghrah-home" / distro / "ability",
        distro=distro,
    )


class _BridgeRecorder:
    """记录薄桥调用，并允许设置 ``run_module`` 的返回码与模块名。"""

    def __init__(self, runtime: AbilityRuntime) -> None:
        self.runtime = runtime
        self.module = TOOLS_BUILD
        self.returncode = 0
        self.build_runtime_calls: list[str | None] = []
        self.module_calls: list[Path] = []
        self.store_runtime_calls: list[tuple[str | None, str | None]] = []
        self.runs: list[tuple[Path, str, list[str]]] = []

    def resolve_build_runtime(self, python: str | None) -> AbilityRuntime:
        self.build_runtime_calls.append(python)
        return self.runtime

    def resolve_build_module(self, python: Path) -> str:
        self.module_calls.append(python)
        return self.module

    def resolve_store_runtime(self, python: str | None, distro: str | None) -> AbilityRuntime:
        self.store_runtime_calls.append((python, distro))
        return self.runtime

    def run_module(self, python: Path, module: str, argv: list[str]) -> int:
        self.runs.append((python, module, list(argv)))
        return self.returncode


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _BridgeRecorder:
    """把薄桥四个入口替换成记录器，返回记录器供断言。"""
    recorder = _BridgeRecorder(_runtime(tmp_path))
    for name in (
        "resolve_build_runtime",
        "resolve_build_module",
        "resolve_store_runtime",
        "run_module",
    ):
        monkeypatch.setattr(ability_cmd, name, getattr(recorder, name))
    return recorder


def _parse(argv: list[str]):
    return _build_parser().parse_args(argv)


def _root(bridge: _BridgeRecorder) -> str:
    assert bridge.runtime.ability_root is not None
    return str(bridge.runtime.ability_root)


# ── 解析器 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["ability", "build", "pkg"],
        ["ability", "install", "pkg"],
        ["ability", "list"],
        ["ability", "show", "acme.reporting@1.4.0"],
        ["ability", "verify", "acme.reporting"],
        ["ability", "refresh", "acme.reporting"],
        ["ability", "uninstall", "acme.reporting@1.4.0"],
    ],
)
def test_seven_subcommands_parse(argv: list[str]) -> None:
    args = _parse(argv)
    assert args.command == "ability"
    assert args.subcommand == argv[1]
    assert callable(args.func)


def test_ability_help_lists_seven_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["ability", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for verb in SEVEN:
        assert verb in out


def test_top_level_help_mentions_ability(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["--help"])
    assert exc.value.code == 0
    assert "ability" in capsys.readouterr().out


def test_build_rejects_distro_option() -> None:
    """build 不落安装，不接受 --distro（参数错误退出 2）。"""
    with pytest.raises(SystemExit) as exc:
        _parse(["ability", "build", "pkg", "--distro", "generic"])
    assert exc.value.code == 2


def test_build_rejects_json_option() -> None:
    """build 的 --json 要等 core 入口支持，本轮不暴露。"""
    with pytest.raises(SystemExit) as exc:
        _parse(["ability", "build", "pkg", "--json"])
    assert exc.value.code == 2


def test_build_defaults() -> None:
    args = _parse(["ability", "build", "pkg"])
    assert args.dir == Path("pkg")
    assert args.check is False
    assert args.python is None


def test_uninstall_parses_all_and_yes() -> None:
    args = _parse(["ability", "uninstall", "acme.reporting", "--all", "--yes"])
    assert args.all_versions is True
    assert args.yes is True
    assert args.target == "acme.reporting"


def test_list_defaults_are_none() -> None:
    args = _parse(["ability", "list"])
    assert args.distro is None
    assert args.python is None
    assert args.json is False


@pytest.mark.parametrize("argv", [["ability"], ["ability", "frobnicate"]])
def test_missing_or_unknown_subcommand_exits_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(argv)
    assert exc.value.code == 2


# ── build 薄桥参数 ───────────────────────────────────────────


def test_build_uses_tools_build_without_verb(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    assert main(["ability", "build", str(pkg)]) == 0
    assert bridge.build_runtime_calls == [None]
    assert bridge.module_calls == [bridge.runtime.python]
    assert bridge.runs == [(bridge.runtime.python, TOOLS_BUILD, [str(pkg)])]


def test_build_passes_check_flag(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    assert main(["ability", "build", str(pkg), "--check"]) == 0
    assert bridge.runs[0][2] == [str(pkg), "--check"]


def test_build_falls_back_to_tools_cli_with_verb(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    """tools.build 缺失时走 tools.cli，并把 verb 前置。"""
    bridge.module = TOOLS_CLI
    pkg = tmp_path / "pkg"
    assert main(["ability", "build", str(pkg), "--check"]) == 0
    assert bridge.runs[0][1] == TOOLS_CLI
    assert bridge.runs[0][2] == ["build", str(pkg), "--check"]


def test_build_forwards_explicit_python(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    assert main(["ability", "build", str(pkg), "--python", "/opt/py/bin/python"]) == 0
    assert bridge.build_runtime_calls == ["/opt/py/bin/python"]
    assert bridge.module_calls == [bridge.runtime.python]


def test_build_returns_core_exit_code(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    bridge.returncode = 1
    assert main(["ability", "build", str(tmp_path / "pkg")]) == 1


# ── store 薄桥参数 ───────────────────────────────────────────


def test_list_argv_and_default_runtime(bridge: _BridgeRecorder) -> None:
    assert main(["ability", "list"]) == 0
    assert bridge.store_runtime_calls == [(None, None)]
    assert bridge.runs == [(bridge.runtime.python, TOOLS_CLI, ["list", "--root", _root(bridge)])]
    assert Path(_root(bridge)).is_absolute()


def test_install_forwards_target_distro_and_python(bridge: _BridgeRecorder, tmp_path: Path) -> None:
    target = str(tmp_path / "pkg")
    assert main(["ability", "install", target, "--distro", "lab", "--python", "/opt/py"]) == 0
    assert bridge.store_runtime_calls == [("/opt/py", "lab")]
    assert bridge.runs[0][2] == ["install", target, "--root", _root(bridge)]


def test_show_passes_full_name_verbatim(bridge: _BridgeRecorder) -> None:
    """工具链不切分/校验身份与版本字符串，@version 原样透传。"""
    assert main(["ability", "show", "acme.reporting@1.4.0"]) == 0
    assert bridge.runs[0][2] == [
        "show",
        "acme.reporting@1.4.0",
        "--root",
        _root(bridge),
    ]


def test_json_is_forwarded_to_core_only(
    bridge: _BridgeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json 交给 core 输出单个对象；工具链成功时不再自己包一层。"""
    assert main(["ability", "list", "--json"]) == 0
    assert bridge.runs[0][2] == ["list", "--json", "--root", _root(bridge)]
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def test_uninstall_forwards_all_and_yes(bridge: _BridgeRecorder) -> None:
    assert (
        main(
            [
                "ability",
                "uninstall",
                "acme.reporting",
                "--all",
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    assert bridge.runs[0][2] == [
        "uninstall",
        "acme.reporting",
        "--json",
        "--all",
        "--yes",
        "--root",
        _root(bridge),
    ]


def test_uninstall_all_conflicts_with_version(
    bridge: _BridgeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """--all 表示全部版本，与 @version 互斥；按参数错误退出 2，不触达 core。"""
    assert main(["ability", "uninstall", "acme.reporting@1.4.0", "--all"]) == 2
    assert bridge.runs == []
    assert bridge.store_runtime_calls == []
    assert "--all" in capsys.readouterr().err


def test_store_returns_core_exit_code(bridge: _BridgeRecorder) -> None:
    """core 的业务失败原样返回，不改写成 0，也不额外输出错误信封。"""
    bridge.returncode = 1
    assert main(["ability", "verify", "acme.reporting"]) == 1
    assert len(bridge.runs) == 1


# ── 失败路径与错误信封 ───────────────────────────────────────


def test_store_missing_tools_cli_reports_clear_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "目标解释器缺少 ghrah.ability.tools.cli：install/list 需要 ghrah-core P2/P5"

    def fail(python, distro):
        raise AbilityError(message)

    monkeypatch.setattr(ability_cmd, "resolve_store_runtime", fail)
    assert main(["ability", "list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_build_runtime_resolution_failure_reports_clear_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "无法定位可执行 ability 命令的运行时：默认发行版 generic 不存在"

    def fail(python):
        raise AbilityError(message)

    monkeypatch.setattr(ability_cmd, "resolve_build_runtime", fail)
    assert main(["ability", "build", "pkg"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_missing_default_distro_reports_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "未配置默认发行版；可用的发行版：lab, generic。请用 --distro 指定"

    def fail(python, distro):
        raise AbilityError(message)

    monkeypatch.setattr(ability_cmd, "resolve_store_runtime", fail)
    assert main(["ability", "list"]) == 1
    assert "可用的发行版" in capsys.readouterr().err


def test_json_envelope_for_toolchain_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "目标解释器缺少 ghrah.ability.tools.cli"

    def fail(python, distro):
        raise AbilityError(message)

    monkeypatch.setattr(ability_cmd, "resolve_store_runtime", fail)
    assert main(["ability", "list", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["scope"] == "ability"
    assert payload["data"] is None
    assert payload["diagnostics"] == [message]


def test_distro_ability_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--distro ability 命中 ~/.ghrah 顶层保留名，必须显式拒绝（依赖布局门禁）。"""
    from ghrah_toolchain.ability_bridge import resolve_store_runtime

    monkeypatch.setattr(ability_cmd, "resolve_store_runtime", resolve_store_runtime)
    assert main(["ability", "list", "--distro", "ability", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "ability"
    assert "保留名" in payload["diagnostics"][0]


# ── 旧用户级存储提示 ─────────────────────────────────────────


def test_legacy_store_warning_on_store_command(
    home: Path, bridge: _BridgeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "ability").mkdir(parents=True)
    assert main(["ability", "list"]) == 0
    captured = capsys.readouterr()
    assert "旧的用户级 Ability 存储" in captured.err
    assert "不读取、不迁移" in captured.err
    assert captured.out == ""


def test_legacy_store_warning_on_build_command(
    home: Path, bridge: _BridgeRecorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "ability").mkdir(parents=True)
    assert main(["ability", "build", str(tmp_path / "pkg")]) == 0
    assert "旧的用户级 Ability 存储" in capsys.readouterr().err


def test_legacy_store_warning_does_not_pollute_json_stdout(
    home: Path, bridge: _BridgeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "ability").mkdir(parents=True)
    assert main(["ability", "list", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "旧的用户级 Ability 存储" in captured.err


def test_no_legacy_store_no_warning(
    bridge: _BridgeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ability", "list"]) == 0
    assert capsys.readouterr().err == ""


def test_doctor_warns_about_legacy_store(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (home / "ability").mkdir(parents=True)
    assert main(["self", "doctor"]) == 0
    captured = capsys.readouterr()
    assert "旧的用户级 Ability 存储" in captured.err
    assert "旧的用户级 Ability 存储" not in captured.out
