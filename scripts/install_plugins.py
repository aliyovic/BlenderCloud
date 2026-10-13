"""Install the current release archives from a background Blender process."""

from pathlib import Path
import importlib
import json
import tomllib

import addon_utils
import bpy


ROOT = Path(__file__).resolve().parents[1]


def main():
    installed = []
    for folder, scene_property in (("scan_viewer", "scan_point_budget"),
                                   ("swiss_gis", "swiss_gis"),
                                   ("railway_bim", "railway_bim")):
        root = ROOT / folder
        manifest = tomllib.loads((root / "addon/blender_manifest.toml").read_text())
        archive = root / "dist" / f"{manifest['id']}-{manifest['version']}-macos-arm64.zip"
        if not archive.is_file():
            raise FileNotFoundError(f"Build the extension before installation: {archive}")
        if folder == "scan_viewer" and "scan_viewer" in bpy.context.preferences.addons:
            addon_utils.disable("scan_viewer", default_set=True)
        result = bpy.ops.extensions.package_install_files(
            directory=str(archive.parent), files=[{"name": archive.name}],
            repo="user_default", enable_on_install=True, overwrite=True,
        )
        if result != {"FINISHED"} or not hasattr(bpy.types.Scene, scene_property):
            raise RuntimeError(f"The installed {manifest['id']} did not register.")
        package = importlib.import_module("bl_ext.user_default." + manifest["id"])
        if not Path(package.__file__).is_file():
            raise RuntimeError(f"The installed {manifest['id']} cannot be imported.")
        installed.append({"id": manifest["id"], "version": manifest["version"], "archive": str(archive)})
    bpy.ops.wm.save_userpref()
    print("PLUGINS_INSTALLED", json.dumps(installed), flush=True)


if __name__ == "__main__":
    main()
