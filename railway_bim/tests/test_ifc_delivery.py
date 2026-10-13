"""Acceptance checks at the exported IFC and persisted edit boundary.

Authoring gate: the pilot export protects class, inventory, geometry, and survey
status delivery; it fails on omitted assets, wrong coordinates, or false survey
claims. The persisted edit protects the user's changed dimensions and identity;
it fails if only the live Blender mesh changes. Rejection protects the saved IFC
and both sidecars from partial writes when the UI supplies invalid input. The
corrupted export proves the validator detects misplaced geometry independently
of matching metadata. No earlier Railway BIM tests cover these contracts, and
all checks use the production build, edit, and validation entry points.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import numpy as np


PLUGIN = Path(__file__).resolve().parents[1]
RECONSTRUCTION = PLUGIN.parent / "infrastructure" / "reconstruction"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_module("railway_bim_delivery_addon", PLUGIN / "addon" / "__init__.py")
core = _module("railway_bim_delivery_addon.core", PLUGIN / "addon" / "core.py")
validation = _module("railway_bim_delivery_addon.validation", PLUGIN / "addon" / "validation.py")


def _asset(model, asset_id):
    matches = [product for product in model.by_type("IfcProduct")
               if ifcopenshell.util.element.get_psets(product)
               .get("Pset_RailwayBIMAsset", {}).get("AssetId") == asset_id]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {asset_id}, got {len(matches)}")
    return matches[0]


def _vertices(product):
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, product)
    return np.asarray(shape.geometry.verts).reshape(-1, 3)


class RailwayIfcDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="railway-bim-delivery-")
        cls.root = Path(cls.workspace.name)
        cls.original = cls.root / "baseline.ifc"
        core.build_ifc(RECONSTRUCTION, cls.original)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def _copy_delivery(self, name):
        folder = self.root / name
        folder.mkdir()
        target = folder / self.original.name
        for suffix in (".ifc", ".railway-project.json", ".asset-register.json"):
            shutil.copy2(self.original.with_suffix(suffix), target.with_suffix(suffix))
        return target

    def test_pilot_ifc_reopens_with_complete_railway_and_source_coordinates(self):
        model = ifcopenshell.open(str(self.original))
        self.assertEqual(model.schema_identifier, "IFC4X3_ADD2")
        self.assertEqual(len(model.by_type("IfcRail")), 4)
        self.assertEqual(sum(item.PredefinedType == "SLEEPER" for item in model.by_type("IfcTrackElement")), 189)
        self.assertEqual(len(model.by_type("IfcAlignment")), 2)
        report = validation.validate_ifc(self.original, RECONSTRUCTION, expected_asset_count=272)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["status"], "review")
        reasons = {item["code"] for item in report["review_items"]}
        self.assertTrue({"horizontal_crs_assumed", "vertical_datum_unknown", "official_chainage_unknown"}.issubset(reasons))
        self.assertEqual(report["counts"]["assets"], 272)
        register = json.loads(self.original.with_suffix(".asset-register.json").read_text())
        self.assertEqual(len({item["asset_id"] for item in register["assets"]}), 272)

    def test_mast_height_edit_survives_reopen_and_keeps_asset_identity(self):
        path = self._copy_delivery("edited")
        before = _asset(ifcopenshell.open(str(path)), "Mast 01")
        guid = before.GlobalId
        core.rebuild_asset(path, "Mast 01", {"height_m": 13.0})
        reopened = ifcopenshell.open(str(path))
        after = _asset(reopened, "Mast 01")
        self.assertEqual(after.GlobalId, guid)
        vertices = _vertices(after)
        self.assertAlmostEqual(float(np.ptp(vertices[:, 2])), 13.0, places=6)
        pset = ifcopenshell.util.element.get_psets(after)["Pset_RailwayBIMAsset"]
        parameters = json.loads(pset["ParametersJSON"])
        self.assertAlmostEqual(parameters["z_top"] - parameters["z_bottom"], 13.0, places=6)
        self.assertEqual(pset["Revision"], 1)
        register = json.loads(path.with_suffix(".asset-register.json").read_text())
        registered = next(item for item in register["assets"] if item["asset_id"] == "Mast 01")
        self.assertEqual(registered["ifc_guid"], guid)
        self.assertEqual(registered["parameters"], parameters)
        self.assertEqual(validation.validate_ifc(path, RECONSTRUCTION)["errors"], [])

    def test_invalid_edit_leaves_ifc_and_both_sidecars_byte_identical(self):
        path = self._copy_delivery("rejected")
        files = [path, path.with_suffix(".railway-project.json"), path.with_suffix(".asset-register.json")]
        before = {file: file.read_bytes() for file in files}
        for changes in ({"height_m": 0}, {"height_m": float("nan")}, {"height_m": float("inf")},
                        {"height_m": 31.0}, {"unsupported_dimension": 1.0}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    core.rebuild_asset(path, "Mast 01", changes)
                self.assertEqual({file: file.read_bytes() for file in files}, before)

    def test_validator_rejects_shifted_rail_even_when_register_ids_match(self):
        path = self._copy_delivery("misplaced")
        model = ifcopenshell.open(str(path))
        rail = _asset(model, "Rail 01")
        previous = rail.ObjectPlacement
        placement = model.create_entity("IfcAxis2Placement3D", Location=model.create_entity("IfcCartesianPoint", Coordinates=(3.0, 0.0, 0.0)))
        rail.ObjectPlacement = model.create_entity("IfcLocalPlacement", PlacementRelTo=previous, RelativePlacement=placement)
        model.write(str(path))
        report = validation.validate_ifc(path, RECONSTRUCTION)
        self.assertEqual(report["status"], "failed")
        failures = {item["check"] for item in report["errors"]}
        self.assertIn("fitted_rail_endpoints", failures)
        inventory = next(item for item in report["checks"] if item["name"] == "asset_register")
        self.assertEqual(inventory["status"], "passed")


if __name__ == "__main__":
    unittest.main()
