"""Install the Finder opener and associate point-cloud file extensions."""

import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "scripts/macos_launcher"
APP = Path("/Applications/Open Point Cloud in Blender.app")
BUNDLE_ID = "ch.aouada.open-point-cloud-in-blender"
EXTENSIONS = ["las", "laz", "e57", "ply", "xyz", "pts", "asc"]
BLENDER = Path("/Applications/Blender.app")
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


def run(*args):
    return subprocess.run([str(arg) for arg in args], check=True, capture_output=True, text=True).stdout


def install():
    if not (BLENDER / "Contents/MacOS/Blender").is_file():
        raise RuntimeError("Install Blender in /Applications/Blender.app first.")
    if APP.exists():
        with (APP / "Contents/Info.plist").open("rb") as stream:
            if plistlib.load(stream).get("CFBundleIdentifier") != BUNDLE_ID:
                raise RuntimeError(f"Another application already uses {APP}.")

    with tempfile.TemporaryDirectory(prefix="scan-viewer-finder-") as temp:
        temp = Path(temp)
        helper = temp / "associations"
        run("/usr/bin/xcrun", "swiftc", SOURCES / "associations.swift", "-o", helper)
        before = json.loads(run(helper, "snapshot", *EXTENSIONS))
        backup = Path.home() / "Library/Application Support/Scan Viewer/finder-associations-before.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_text(json.dumps(before, indent=2) + "\n")

        staged = temp / APP.name
        run("/usr/bin/osacompile", "-o", staged, SOURCES / "launcher.applescript")
        resources = staged / "Contents/Resources"
        shutil.copy2(SOURCES / "open_cloud.py", resources)
        addon = resources / "addons/scan_viewer"
        addon.mkdir(parents=True)
        manifest = tomllib.loads((ROOT / "addon/blender_manifest.toml").read_text())
        package = ROOT / "dist" / f"{manifest['id']}-{manifest['version']}-macos-arm64.zip"
        if not package.is_file():
            subprocess.run(["sh", str(ROOT / "scripts/build_extension.sh")], cwd=ROOT, check=True)
        with zipfile.ZipFile(package) as archive:
            archive.extractall(addon)
        (addon / "bin/scan-cache").chmod(0o755)
        shutil.copy2(ROOT / "LICENSE", addon)
        shutil.copy2(BLENDER / "Contents/Resources/blender_icon.icns", resources / "blender.icns")

        plist_path = staged / "Contents/Info.plist"
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)
        types = []
        documents = []
        for entry in before:
            ext = entry["extension"]
            identifier = entry["type"]
            if identifier.startswith("dyn."):
                identifier = f"ch.aouada.scan-viewer.{ext}"
            types.append({
                "UTTypeIdentifier": identifier,
                "UTTypeDescription": f"{ext.upper()} point cloud",
                "UTTypeConformsTo": ["public.data"],
                "UTTypeTagSpecification": {"public.filename-extension": [ext]},
            })
            documents.append({
                "CFBundleTypeName": f"{ext.upper()} point cloud",
                "CFBundleTypeExtensions": [ext],
                "CFBundleTypeRole": "Viewer",
                "CFBundleTypeIconFile": "blender.icns",
                "LSHandlerRank": "Default",
                "LSItemContentTypes": [identifier],
            })
        info.update({
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": "Open Point Cloud in Blender",
            "CFBundleDisplayName": "Open Point Cloud in Blender",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "CFBundleIconFile": "blender.icns",
            "LSUIElement": True,
            "CFBundleDocumentTypes": documents,
            "UTImportedTypeDeclarations": types,
        })
        with plist_path.open("wb") as stream:
            plistlib.dump(info, stream)
        run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", staged)

        if APP.exists():
            shutil.rmtree(APP)
        shutil.copytree(staged, APP)
        run(LSREGISTER, "-f", APP)
        run(helper, "set", APP, *EXTENSIONS)
        print(f"Installed {APP}")
        print(run(helper, "snapshot", *EXTENSIONS))


if __name__ == "__main__":
    install()
