"""Check packaged assets, then import the wheel in a clean isolated environment."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--find-links", type=Path)
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the clean wheel check")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        required = {
            "toolframe_audit/page.py",
            "toolframe_audit/docs/usage.md",
            "toolframe_audit/migrations/0001_init.sql",
            "toolframe_audit/migrations/0001_init.postgresql.sql",
            "toolframe_audit/migrations/outbox.sql",
        }
        assert required <= set(names), required - set(names)
        assert not any(
            name.startswith(("tests/", "examples/", ".local/", "build/"))
            for name in names
        )
        metadata = archive.read("toolframe_audit-0.2.0.dist-info/METADATA").decode()
        assert "Requires-Python: >=3.14" in metadata
        assert "Requires-Dist: toolframe-sdk==0.4.0" in metadata
        assert "License-Expression: Apache-2.0" in metadata
    with tempfile.TemporaryDirectory(prefix="audit-wheel-") as temporary:
        environment = Path(temporary) / "venv"
        subprocess.run([uv, "venv", "--python", "3.14", str(environment)], check=True)
        python = environment / "bin/python"
        install = [uv, "pip", "install", "--python", str(python), "--no-sources"]
        if arguments.find_links:
            install.extend(["--find-links", str(arguments.find_links.resolve())])
        subprocess.run([*install, str(wheel)], check=True)
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                """
from importlib.metadata import entry_points, version
from importlib.resources import files
from toolframe_audit import drain, enqueue, outbox_migration
from toolframe_audit.page import render
from toolframe_audit.plugin import plugin
assert version('toolframe-audit') == '0.2.0'
assert plugin.id == 'audit' and plugin.provider_kind is None
assert plugin.services['audit.events'].methods['ingest'].audience == 'internal'
assert all(not method.public_api for method in plugin.services['audit.events'].methods.values())
assert plugin.store.migrations[0].for_dialect('postgresql').sql
assert outbox_migration().for_dialect('postgresql').sql
assert files('toolframe_audit').joinpath('docs/usage.md').read_text()
assert any(point.name == 'audit' and point.load().id == 'audit' for point in entry_points(group='toolframe.plugins'))
print('Clean Audit wheel import and assets passed')
""",
            ],
            check=True,
            cwd=temporary,
        )


if __name__ == "__main__":
    main()
