"""Command-line access to Railway BIM construction, validation, and asset edits."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Create an IFC from a fitted crop")
    build.add_argument("reconstruction_dir", type=Path)
    build.add_argument("output_ifc", type=Path)
    build.add_argument("--overwrite", action="store_true")
    validate = commands.add_parser("validate", help="Check an exported IFC")
    validate.add_argument("ifc_path", type=Path)
    validate.add_argument("--reconstruction-dir", type=Path)
    validate.add_argument("--report", type=Path)
    edit = commands.add_parser("edit", help="Change one asset dimension and preserve its GUID")
    edit.add_argument("ifc_path", type=Path)
    edit.add_argument("asset_id")
    edit.add_argument("--changes", required=True, help="JSON object of reconstruction dimension changes")
    corridor = commands.add_parser("plan-corridor", help="Plan overlapping extraction sections from supplied local geometry")
    corridor.add_argument("project_json", type=Path)
    corridor.add_argument("geometry_json", type=Path, help="JSON with bounds_local_m and optional path_local_m")
    corridor.add_argument("output_json", type=Path)
    corridor.add_argument("--section-length", type=float, default=500.0)
    corridor.add_argument("--overlap", type=float, default=20.0)
    corridor.add_argument("--local-limit", type=float, default=1000.0)
    corridor.add_argument("--overwrite", action="store_true")
    reconcile = commands.add_parser("reconcile-corridor", help="Compare project-wide asset observations across planned sections")
    reconcile.add_argument("plan_json", type=Path)
    reconcile.add_argument("output_json", type=Path)
    reconcile.add_argument("observations", type=Path, nargs="+")
    reconcile.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            from railway_bim.core import build_ifc

            result = build_ifc(args.reconstruction_dir, args.output_ifc, overwrite=args.overwrite)
        elif args.command == "validate":
            from railway_bim.validation import validate_ifc

            result = validate_ifc(args.ifc_path, args.reconstruction_dir, report_path=args.report)
        elif args.command == "edit":
            from railway_bim.core import rebuild_asset

            result = rebuild_asset(args.ifc_path, args.asset_id, json.loads(args.changes))
        elif args.command == "plan-corridor":
            from railway_bim.corridor import plan_corridor, read_project, write_document

            document = plan_corridor(read_project(args.project_json), json.loads(args.geometry_json.read_text()),
                                     section_length_m=args.section_length, overlap_m=args.overlap,
                                     max_local_extent_m=args.local_limit)
            path = write_document(document, args.output_json, overwrite=args.overwrite,
                                  references={"source_project": args.project_json, "source_geometry": args.geometry_json})
            result = {"status": "planned", "plan_path": path, "segment_count": len(document["segments"]),
                      "route_length_m": document["route_length_m"]}
        else:
            from railway_bim.corridor import reconcile_observations, write_document

            document = reconcile_observations(json.loads(args.plan_json.read_text()),
                                              [json.loads(path.read_text()) for path in args.observations])
            path = write_document(document, args.output_json, overwrite=args.overwrite,
                                  references={"source_plan": args.plan_json, "source_observations": args.observations})
            result = {"status": document["status"], "reconciliation_path": path,
                      "asset_count": len(document["assets"]), "conflict_count": len(document["conflicts"])}
        print(json.dumps(result, indent=2, allow_nan=False))
        return 1 if result.get("status") in {"failed", "conflict"} or result.get("errors") else 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
