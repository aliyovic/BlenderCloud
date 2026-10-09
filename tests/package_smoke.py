import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

import bpy

root = Path(__file__).resolve().parents[1]
package = root / "dist/scan_viewer-0.4.0-macos-arm64.zip"
with tempfile.TemporaryDirectory(prefix="scan-viewer-package-") as directory:
    extension = Path(directory) / "scan_viewer"
    extension.mkdir()
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extension)
    spec = importlib.util.spec_from_file_location("scan_viewer_package", extension / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = addon
    spec.loader.exec_module(addon)
    addon.register()

    source = Path("/Users/ali/Desktop/pointclouds/ZH/swisssurface3d_2024_2669-1250_2056_5728.copc.laz")
    origin = json.loads(subprocess.check_output([str(addon._binary()), "inspect-origin", str(source)]))
    assert origin == [2669000.0, 1250000.0, 0.0]

    from scan_viewer_package.streaming import RustClient
    client = RustClient(addon._binary())
    try:
        planned, _ = client.request({"op":"plan", "views":[{"source":str(source),"object":"test",
            "origin":origin,"width":1000,"height":1000,
            "matrix":[[0.001,0,0,-0.5],[0,0.001,0,-0.5],[0,0,0.001,-0.5],[0,0,0,1]]}],
            "budget":100000,"pixel_error":1.5,"cap":65536})
        node = planned["nodes"][0]
        decoded, payload = client.request(dict(op="node",source=str(source),stamp=node["stamp"],
            node=node["node"],origin=origin,cap=65536))
        assert decoded["count"] > 0 and len(payload) == decoded["count"]*18
    finally:
        client.close()

    assert bpy.ops.scan_viewer.import_folder(directory="/Users/ali/Desktop/pointclouds") == {'FINISHED'}
    while addon._jobs or addon._pending:
        addon._poll_jobs()
        time.sleep(0.1)
    assert len([obj for obj in bpy.data.objects if "scan_cache" in obj]) == 15
    assert bpy.ops.scan_viewer.frame_all() == {'FINISHED'}
    addon.unregister()
    print("PACKAGE_SMOKE_OK", package)
