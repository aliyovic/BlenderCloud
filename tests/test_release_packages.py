"""Protect domain package delivery in the extension archives.

This boundary catches a missing package or runtime submodule after source layout
changes. Core tests run from the checkout and cannot detect that omission. The
test imports the archive from another directory without checkout search paths.
"""

from pathlib import Path
import os
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_support import stage_plugin, write_archive


def test_packaged_domains_import_without_the_checkout(tmp_path):
    packages = []
    for name in ("railway_bim", "swiss_gis"):
        stage = tmp_path / f"{name}-stage"
        stage_plugin(ROOT / name, stage)
        archive = write_archive(stage, tmp_path / f"{name}.zip")
        unpacked = tmp_path / f"{name}-installed"
        with zipfile.ZipFile(archive) as package:
            package.extractall(unpacked)
        packages.append(unpacked)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in packages)
    code = """
from pathlib import Path
import railway_bim.core
import railway_bim.geometry
import railway_bim.validation
import railway_bim.pilot
import swiss_gis.core
assert callable(railway_bim.core.build_ifc)
assert callable(railway_bim.core.rebuild_asset)
assert callable(railway_bim.validation.validate_ifc)
assert callable(swiss_gis.core.prepare)
assert Path(railway_bim.core.__file__).is_relative_to(Path.cwd())
assert Path(swiss_gis.core.__file__).is_relative_to(Path.cwd())
contract = railway_bim.pilot.import_swiss_crop(
    {'rails': [{'id': f'Rail {i:02}'} for i in range(1, 5)], 'sleepers': [],
     'terrain': {'grid_step_m': 0.2}},
    {'sha256': railway_bim.pilot.SOURCE_SHA256, 'origin': [2689160., 1254972., 452.]})
assert len(contract['tracks']) == 2
assert contract['reference']['horizontal']['status'] == 'ASSUMED'
"""
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, check=True)
