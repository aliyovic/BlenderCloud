"""Check that Finder replaces an enabled older add-on with its bundled release."""

from pathlib import Path
import os
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
import tomllib

import bpy

root = Path(__file__).resolve().parents[1]
fixture = tempfile.TemporaryDirectory(prefix="scan-viewer-finder-")
fixture_path = Path(fixture.name).resolve()
old_path = fixture_path / "old/scan_viewer"
old_path.mkdir(parents=True)
with zipfile.ZipFile(root / "dist/scan_viewer-0.4.0-macos-arm64.zip") as archive:
    archive.extractall(old_path)
sys.path.insert(0, str(old_path.parent))
assert bpy.ops.preferences.addon_enable(module="scan_viewer") == {'FINISHED'}
import scan_viewer as old_addon
assert Path(old_addon.__file__).resolve().parent == old_path, old_addon.__file__

resources = fixture_path / "Resources"
bundle = resources / "addons/scan_viewer"
bundle.mkdir(parents=True)
manifest = tomllib.loads((root / "addon/blender_manifest.toml").read_text())
package = root / "dist" / f"{manifest['id']}-{manifest['version']}-macos-arm64.zip"
with zipfile.ZipFile(package) as archive:
    archive.extractall(bundle)
launcher = resources / "open_cloud.py"
shutil.copy2(root / "scripts/macos_launcher/open_cloud.py", launcher)
source = root / "tests/cras_binary_10000.ply"
sys.argv = ["Blender", "--", str(source)]
exec(compile(launcher.read_text(), str(launcher), 'exec'), {'__file__': str(launcher)})
started = time.monotonic()


def verify():
    try:
        addon = sys.modules.get("scan_viewer")
        scans = [obj for obj in bpy.context.scene.objects if 'scan_cache' in obj]
        if time.monotonic()-started > 15:
            raise RuntimeError("Finder launch timed out")
        if not scans:
            return 0.1
        assert addon is not old_addon
        assert Path(addon.__file__).resolve().parent == bundle
        assert scans[0]['scan_source_points'] == 10_000
        assert scans[0].scan_point_size == 1.0
        assert addon._binary().read_bytes() == (bundle / 'bin/scan-cache').read_bytes()
        addon.unregister()
        fixture.cleanup()
        print("FINDER_BUNDLE_OK", flush=True)
        bpy.ops.wm.quit_blender()
        return None
    except Exception:
        print(traceback.format_exc(), flush=True)
        fixture.cleanup()
        os._exit(1)


bpy.app.timers.register(verify, first_interval=1.0)
