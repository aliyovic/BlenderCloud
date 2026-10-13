"""Prepare a local project bundle without launching Blender."""
from pathlib import Path
import argparse
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from swiss_gis import core


def main():
    parser = argparse.ArgumentParser(description="Crop local LV95 scans and GeoTIFFs into an offline Blender project")
    parser.add_argument("scan")
    parser.add_argument("output")
    parser.add_argument("--terrain-folder", default="")
    parser.add_argument("--imagery-folder", default="")
    parser.add_argument("--margin", type=float, default=50)
    parser.add_argument("--max-points", type=int, default=750000)
    parser.add_argument("--terrain-step", type=float, default=2)
    parser.add_argument("--vertical-datum", choices=["UNKNOWN", "LN02", "LHN95", "ELLIPSOIDAL"], default="UNKNOWN")
    parser.add_argument("--require-crs", action="store_true")
    parser.add_argument("--center", type=float, nargs=2, metavar=("EAST", "NORTH"))
    parser.add_argument("--project-id", help="Reuse the persistent project UUID when preparing another observation")
    args = parser.parse_args()
    project = core.Project(scan=args.scan, terrain_folder=args.terrain_folder, imagery_folder=args.imagery_folder,
                           margin=args.margin, max_points=args.max_points, terrain_step=args.terrain_step,
                           vertical_datum=args.vertical_datum, assume_lv95=not args.require_crs, center=args.center)
    if args.project_id:
        project.project_id = args.project_id
    try:
        manifest = core.prepare(project, args.output, print)
    except (core.DataError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"Prepared {manifest['preview_points']:,} points in {Path(args.output).resolve()}")
    for warning in manifest["warnings"]:
        print(warning)


if __name__ == "__main__":
    main()
