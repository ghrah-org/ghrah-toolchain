# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""工具链运行时独立性门禁；允许已声明的第三方依赖。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_boot_import_graph_has_no_ghrah() -> None:
    code = (
        "import sys\n"
        "import ghrah_toolchain.cli\n"
        "bad = [m for m in sys.modules if m == 'ghrah' or m.startswith('ghrah.')]\n"
        "print('BAD:' + ','.join(bad) if bad else 'CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(_SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("CLEAN"), proc.stdout


def test_declared_dependencies_do_not_load_runtime() -> None:
    """schema 与进程管理依赖可用，不加载目标运行时业务包。"""
    code = (
        "import sys\n"
        "import ghrah_toolchain.manifest, ghrah_toolchain.instances\n"
        "import jsonschema, psutil, packaging\n"
        "assert not any(m == 'ghrah' or m.startswith('ghrah.') for m in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PYTHONPATH": str(_SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_provisioning_surface() -> None:
    """退役约定：包内不得再有 provisioning/resources 路径与 ProvisioningError。"""
    pkg = _SRC / "ghrah_toolchain"
    assert not (pkg / "provisioning.py").exists()
    assert not (pkg / "resources").exists()
    for py in pkg.rglob("*.py"):
        content = py.read_text(encoding="utf-8")
        assert "provision" not in content.lower(), f"{py} 残留 provisioning 引用"
