"""Run portable checks, or the separately configured Blender integration checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from build_support import ROOT, find_blender


def run(command: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def portable():
    scan = ROOT / "scan_viewer"
    run(["cargo", "fmt", "--all", "--", "--check"], cwd=scan)
    run(["cargo", "clippy", "--locked", "--all-targets", "--all-features", "--", "-D", "warnings"], cwd=scan)
    documentation = os.environ.copy()
    documentation["RUSTDOCFLAGS"] = "-D warnings"
    run(["cargo", "doc", "--locked", "--no-deps"], cwd=scan, env=documentation)
    run(["cargo", "test", "--locked", "--all-targets"], cwd=scan)
    run(["cargo", "build", "--locked"], cwd=scan)
    environment = os.environ.copy()
    environment["SCAN_CACHE_BINARY"] = str(scan / "target" / "debug" / "scan-cache")
    run([sys.executable, "-m", "unittest", "discover", "-s", "scan_viewer/tests", "-p", "test_worker_protocol.py"], env=environment)
    run([sys.executable, "-m", "pytest", "-q", "swiss_gis/tests/test_local_project.py",
         "railway_bim/tests/test_project_contract.py", "railway_bim/tests/test_corridor_delivery.py",
         "scan_viewer/tests/test_transport.py", "tests"], env=environment)


def blender_checks(binary: str, pilot_ifc: Path):
    if not pilot_ifc.is_file():
        raise FileNotFoundError(f"The Blender pilot check requires an existing IFC: {pilot_ifc}")
    project = json.loads(pilot_ifc.with_suffix(".railway-project.json").read_text())
    reconstruction = Path(project["reconstruction_dir"]).expanduser()
    if not reconstruction.is_absolute():
        reconstruction = pilot_ifc.parent / reconstruction
    reconstruction = reconstruction.resolve()
    if not reconstruction.is_dir():
        raise FileNotFoundError(f"The pilot acceptance check requires its reconstruction: {reconstruction}")
    run([binary, "--background", "--python-exit-code", "1", "--python",
         str(ROOT / "railway_bim/tests/check_ifc_delivery.py"), "--",
         "--reconstruction-dir", str(reconstruction)])
    run([binary, "--background", "--python-exit-code", "1", "--python",
         str(ROOT / "scan_viewer/tests/blender_portable.py")])
    run([binary, "--background", "--python-exit-code", "1", "--python",
         str(ROOT / "swiss_gis/tests/blender_integration.py"), "--", "--pilot-ifc", str(pilot_ifc)])
    run([binary, "--background", "--python-exit-code", "1", "--python",
         str(ROOT / "railway_bim/tests/check_extension.py"), "--", "--ifc", str(pilot_ifc)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("portable", "blender"), default="portable")
    parser.add_argument("--blender", help="Blender executable, also configurable through BLENDER_BIN")
    parser.add_argument("--pilot-ifc", type=Path,
                        default=ROOT / "railway_bim/output/railway_crop.ifc")
    args = parser.parse_args()
    if args.suite == "portable":
        portable()
    else:
        blender = find_blender(args.blender)
        if not blender:
            parser.error("Blender is required for --suite blender. Supply --blender or BLENDER_BIN.")
        blender_checks(blender, args.pilot_ifc.resolve())


if __name__ == "__main__":
    main()
