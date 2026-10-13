"""Install the packaged extension into Blender's user extension repository."""

import argparse
from pathlib import Path
import sys
import tomllib

import bpy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "addon" / "blender_manifest.toml").read_text())
    filename = f"{manifest['id']}-{manifest['version']}-macos-arm64.zip"
    parser.add_argument("--zip", type=Path, default=root / "dist" / filename)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    archive = args.zip.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    result = bpy.ops.extensions.package_install_files(
        directory=str(archive.parent), files=[{"name": archive.name}], repo="user_default",
        enable_on_install=True, overwrite=True,
    )
    if result != {"FINISHED"} or not hasattr(bpy.types.Scene, "railway_bim"):
        raise RuntimeError("Railway BIM did not install and register.")
    bpy.ops.wm.save_userpref()
    print("RAILWAY_BIM_INSTALLED", archive, flush=True)


if __name__ == "__main__":
    main()
