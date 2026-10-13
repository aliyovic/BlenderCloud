"""Protect geographic placement and bounded local data reads at the bundle boundary."""
from pathlib import Path
import importlib.util
import json
import socket
import sys

import laspy
import numpy as np
import pytest
from pyproj import CRS
import rasterio
from rasterio.transform import from_origin


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("swiss_core_test", ROOT / "addon/core.py")
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


from fixtures import fixture_scan, fixture_raster


def test_bundle_preserves_laz_coordinates_and_only_reads_intersecting_rasters(tmp_path, monkeypatch):
    # A failure here catches large-coordinate float32 subtraction, missed tiles,
    # nodata bridging, or north/south image reversal in the published bundle.
    def no_network(*args, **kwargs):
        raise AssertionError("The local preparation attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    xyz = fixture_scan(tmp_path / "scan.laz")
    terrain = tmp_path / "terrain"
    imagery = tmp_path / "imagery"
    terrain.mkdir()
    imagery.mkdir()
    z = np.full((8, 8), 450, dtype=np.float32)
    z[3, 3] = -9999
    fixture_raster(terrain / "here.tif", z)
    fixture_raster(terrain / "elsewhere.tif", z, x=2670000)
    rgb = np.zeros((3, 8, 8), dtype=np.uint8)
    rgb[0, :4] = 255
    rgb[2, 4:] = 255
    fixture_raster(imagery / "aerial.tif", rgb)
    out = tmp_path / "bundle"
    manifest = core.prepare(core.Project(str(tmp_path / "scan.laz"), str(terrain), str(imagery),
                                        margin=1, ground_step=1, terrain_step=1, max_points=9), out)
    with np.load(out / "scan.npz") as points:
        restored = points["points"].astype(np.float64) + np.array(manifest["origin"])
        np.testing.assert_allclose(restored, xyz, atol=2e-6, rtol=0)
        np.testing.assert_array_equal(points["source_ids"], np.arange(9))
    assert len(manifest["terrain_sources"]) == 1
    assert len(manifest["skipped_terrain"]) == 1
    assert manifest["terrain_sources"][0]["window"][2] < 8
    with np.load(out / "terrain.npz") as terrain_data:
        assert len(terrain_data["faces"]) < 9
        np.testing.assert_allclose(terrain_data["vertices"][:, 2] + manifest["origin"][2], 450, atol=1e-5)
    with rasterio.open(out / "aerial.png") as image:
        pixels = image.read()
        assert pixels[0, 0].max() == 255
        assert pixels[2, -1].max() == 255
    assert core.load_manifest(out)["source"]["crs_assumed"]
    contract = core.load_manifest(out)["project_contract"]
    assert contract["schema"] == "railway-project-contract/1"
    assert contract["reference"]["local_origin_m"] == manifest["origin"]
    assert contract["reference"]["horizontal"]["status"] == "ASSUMED"


@pytest.mark.parametrize("scenario, message", [
    ("unconfirmed", "no CRS"), ("wrong_crs", "Convert it"),
    ("vertical_mismatch", "datum mismatch"), ("empty_area", "no sampled"),
    ("oversized_grid", "four million"), ("cancel", "canceled"),
])
def test_invalid_input_never_publishes_a_partial_project(tmp_path, scenario, message):
    scan = tmp_path / "scan.laz"
    fixture_scan(scan, "EPSG:32632" if scenario == "wrong_crs" else None)
    project = core.Project(str(scan), margin=1, max_points=9)
    if scenario == "unconfirmed":
        project.assume_lv95 = False
    if scenario == "vertical_mismatch":
        folder = tmp_path / "terrain"
        folder.mkdir()
        fixture_raster(folder / "terrain.tif", np.full((8, 8), 450, dtype=np.float32), datum="LHN95")
        project.terrain_folder = str(folder)
        project.vertical_datum = "LN02"
    if scenario == "empty_area":
        project.center = (2689250, 1254900)
    if scenario == "oversized_grid":
        project.ground_step = .0001
    out = tmp_path / "out"
    with pytest.raises(core.DataError, match=message):
        core.prepare(project, out, cancel=lambda: scenario == "cancel")
    assert not out.exists()
    assert not list(tmp_path.glob("out.partial-*"))


@pytest.mark.parametrize("extent, empty_folder", [(2, False), (998, False), (998, True)])
def test_preview_budget_and_missing_surroundings_remain_explicit(tmp_path, extent, empty_folder):
    # A railway area may fit the ground budget while exceeding an unused aerial
    # grid's budget. Absent imagery must not prevent publishing its scan bundle.
    xyz = fixture_scan(tmp_path / "scan.laz")
    if extent > 2:
        xyz[:, 0] = xyz[0, 0] + (xyz[:, 0] - xyz[0, 0]) * extent / 2
        xyz[:, 1] = xyz[0, 1] + (xyz[:, 1] - xyz[0, 1]) * 398 / 2
        source = laspy.read(tmp_path / "scan.laz")
        source.x, source.y = xyz[:, :2].T
        source.write(tmp_path / "scan.laz")
    imagery = tmp_path / "imagery"
    if empty_folder:
        imagery.mkdir()
    out = tmp_path / "out"
    project = core.Project(str(tmp_path / "scan.laz"), margin=1, max_points=4,
                           imagery_folder=str(imagery) if empty_folder else "")
    manifest = core.prepare(project, out)
    assert manifest["preview_points"] == 3
    with np.load(out / "scan.npz") as scan:
        np.testing.assert_allclose(scan["points"] + np.array(manifest["origin"]), xyz[[0, 3, 6]], atol=2e-6, rtol=0)
    assert manifest["vertical_datum"] == "UNKNOWN"
    assert not manifest["terrain_sources"] and not manifest["imagery_sources"]
    assert not (out / "aerial.png").exists()
    with np.load(out / "terrain.npz") as terrain:
        assert terrain["vertices"].shape == (0, 3) and terrain["faces"].shape == (0, 4)
    assert any("not aerial imagery" in note for note in manifest["warnings"])
    before = (out / "project.json").read_bytes()
    with pytest.raises(core.DataError, match="preserved"):
        core.prepare(core.Project(str(tmp_path / "scan.laz")), out)
    assert (out / "project.json").read_bytes() == before
    if empty_folder:
        fixture_raster(imagery / "aerial.tif", np.full((3, 8, 8), 255, dtype=np.uint8))
        selected = tmp_path / "selected-imagery"
        with pytest.raises(core.DataError, match="four million"):
            core.prepare(project, selected)
        assert not selected.exists() and not list(tmp_path.glob("selected-imagery.partial-*"))
