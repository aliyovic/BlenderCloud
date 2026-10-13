"""Build the railway crop IFC, validate it, and save a Bonsai workspace."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def run_blender(blender, script, *arguments, blend_file=None):
    command = [str(blender), "--background"]
    if blend_file:
        command.append(str(blend_file))
    command += ["--python-exit-code", "1", "--python", str(script), "--", *map(str, arguments)]
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstruction", type=Path, default=ROOT.parent / "infrastructure" / "reconstruction")
    parser.add_argument("--out", type=Path, default=ROOT / "output")
    parser.add_argument("--blender", type=Path, default=Path("/Applications/Blender.app/Contents/MacOS/Blender"))
    parser.add_argument("--skip-blender", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    from railway_bim.core import build_ifc
    from railway_bim.validation import validate_ifc

    args.out.mkdir(parents=True, exist_ok=True)
    ifc_path = args.out / "railway_crop.ifc"
    result = build_ifc(args.reconstruction, ifc_path, overwrite=args.overwrite)
    print(json.dumps(result, indent=2), flush=True)
    validation = validate_ifc(ifc_path, args.reconstruction, report_path=args.out / "railway_crop.validation.json")
    print(json.dumps({"validation": validation["status"], "errors": validation["errors"]}, indent=2), flush=True)
    if validation["errors"]:
        return 1
    if not args.skip_blender:
        run_blender(args.blender, ROOT / "scripts" / "build_bonsai_scene.py", "--ifc", ifc_path,
                    "--source-blend", args.reconstruction / "model.blend", "--blend", args.out / "railway_crop.blend",
                    "--reconstruction", args.reconstruction, "--render")
        run_blender(args.blender, ROOT / "scripts" / "check_bonsai_scene.py", "--stage", "saved",
                    "--ifc", ifc_path, "--blend", args.out / "railway_crop.blend",
                    "--reconstruction", args.reconstruction, blend_file=args.out / "railway_crop.blend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
