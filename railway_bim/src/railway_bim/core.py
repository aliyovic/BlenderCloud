"""Build and edit IFC 4.3 railway assets from the fitted source reconstruction.

This module uses IfcOpenShell and NumPy. It can run in Blender or ordinary
Python and does not change Blender state.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import uuid

import ifcopenshell
import ifcopenshell.util.element
import numpy as np

from . import geometry
from .contract import legacy_reference, validate_contract
from .pilot import import_swiss_crop


PROJECT_SCHEMA = "railway-bim-project/2"
REGISTER_SCHEMA = "railway-bim-asset-register/2"
LEGACY_PROJECT_SCHEMA = "railway-bim-project/1"
LEGACY_REGISTER_SCHEMA = "railway-bim-asset-register/1"
IFC_SCHEMA = "IFC4X3_ADD2"
ASSET_PSET = "Pset_RailwayBIMAsset"
EDIT_LIMITS = {
    "sleepers": {"width_u": (.03, 1.), "length_v": (1., 5.), "depth": (.03, .8)},
    "masts": {"height_m": (.2, 30.)},
    "rails": {"head_width_m": (.04, .12)},
}
_GUID_NAMESPACE = uuid.UUID("4cfda21a-b1d9-5b7a-9f39-90b47f53a705")
_COLOURS = {
    "rails": (.20, .24, .26), "sleepers": (.43, .40, .34),
    "masts": (.48, .52, .56), "wires": (.20, .16, .12),
    "supports": (.34, .40, .44), "droppers": (.34, .40, .44),
    "troughs": (.66, .67, .65), "counterweights": (.43, .40, .34),
    "terrain": (.30, .35, .22),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _guid(identity_seed, key):
    return ifcopenshell.guid.compress(uuid.uuid5(_GUID_NAMESPACE, identity_seed + ":" + key).hex)


def _sidecars(ifc_path):
    path = Path(ifc_path).expanduser().resolve()
    return path.with_suffix(".railway-project.json"), path.with_suffix(".asset-register.json")


def _load_inputs(reconstruction_dir, project_contract=None):
    root = Path(reconstruction_dir).expanduser().resolve()
    work = root if (root / "component_spec.json").is_file() else root / "work"
    missing = [str(work / name) for name in
               ("component_spec.json", "alignment.json", "source_header.json")
               if not (work / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing reconstruction inputs: " + ", ".join(missing))
    spec, alignment, header = [json.loads((work / name).read_text()) for name in
                               ("component_spec.json", "alignment.json", "source_header.json")]
    _json(spec)
    _json(alignment)
    _json(header)
    if not isinstance(header.get("sha256"), str) or len(header["sha256"]) != 64:
        raise ValueError("The source header needs its recorded SHA-256 fingerprint")
    if spec.get("terrain") and not (work / "terrain.npz").is_file():
        raise FileNotFoundError("Missing reconstruction terrain: " + str(work / "terrain.npz"))
    for key in ("along", "across"):
        vector = np.asarray(alignment[key], dtype=float)
        if vector.shape != (2,) or not np.isfinite(vector).all():
            raise ValueError("Invalid reconstruction coordinate basis")
    basis = np.column_stack((alignment["along"], alignment["across"]))
    if not np.allclose(basis.T @ basis, np.eye(2), atol=1e-8) or np.linalg.det(basis) < .999:
        raise ValueError("The reconstruction basis must be a right-handed orthonormal rotation")
    origin = np.asarray(header["origin"], dtype=float)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise ValueError("The source header needs a finite three-coordinate local origin")
    if project_contract is None:
        manifest = root / "project.json"
        if not manifest.is_file() and work == root:
            manifest = root.parent / "project.json"
        project_contract = (json.loads(manifest.read_text()) if manifest.is_file()
                            else import_swiss_crop(spec, header))
    contract = validate_contract(project_contract, spec, header)
    return root, work, spec, alignment, header, contract


class _Author:
    def __init__(self, file, header, alignment, body_context=None, axis_context=None, contract=None):
        self.file, self.header, self.alignment = file, header, alignment
        self.sha = header["sha256"]
        self.contract = contract if contract is not None else {
            "sampling": {"rail_sample_count": 280, "alignment_sample_count": 280}}
        self.identity_seed = self.contract.get("project_id", self.sha)
        self.reference = self.contract.get("reference", legacy_reference(header))
        self.body_context, self.axis_context = body_context, axis_context
        self.styles = {}

    def point(self, coordinates):
        return self.file.create_entity("IfcCartesianPoint", Coordinates=tuple(float(v) for v in coordinates))

    def direction(self, coordinates):
        return self.file.create_entity("IfcDirection", DirectionRatios=tuple(float(v) for v in coordinates))

    def axis(self, location=(0., 0., 0.), axis=(0., 0., 1.), ref=(1., 0., 0.)):
        return self.file.create_entity("IfcAxis2Placement3D", Location=self.point(location),
                                       Axis=self.direction(axis), RefDirection=self.direction(ref))

    def placement(self, parent=None):
        return self.file.create_entity("IfcLocalPlacement", PlacementRelTo=parent,
                                       RelativePlacement=self.axis())

    def root(self, ifc_class, key, **kwargs):
        return self.file.create_entity(ifc_class, GlobalId=_guid(self.identity_seed, key), **kwargs)

    def pset(self, entity, name, properties):
        items = []
        for key, value in properties.items():
            if value is None:
                continue
            if isinstance(value, bool):
                nominal = self.file.create_entity("IfcBoolean", value)
            elif isinstance(value, int):
                nominal = self.file.create_entity("IfcInteger", value)
            elif isinstance(value, float):
                nominal = self.file.create_entity("IfcReal", value)
            else:
                nominal = self.file.create_entity("IfcText", str(value))
            items.append(self.file.create_entity("IfcPropertySingleValue", Name=key, NominalValue=nominal))
        pset = self.root("IfcPropertySet", entity.GlobalId + ":pset:" + name, Name=name, HasProperties=items)
        self.root("IfcRelDefinesByProperties", entity.GlobalId + ":pset-rel:" + name,
                  RelatedObjects=[entity], RelatingPropertyDefinition=pset)
        return pset

    def aggregate(self, parent, children):
        self.root("IfcRelAggregates", parent.GlobalId + ":decomposition",
                  RelatingObject=parent, RelatedObjects=children)

    def contain(self, part, products):
        if products:
            self.root("IfcRelContainedInSpatialStructure", part.GlobalId + ":containment",
                      RelatedElements=products, RelatingStructure=part)

    def style(self, item, group):
        if group not in self.styles:
            colour = self.file.create_entity("IfcColourRgb", Red=_COLOURS[group][0],
                                             Green=_COLOURS[group][1], Blue=_COLOURS[group][2])
            shading = self.file.create_entity("IfcSurfaceStyleShading", SurfaceColour=colour, Transparency=0.)
            self.styles[group] = self.file.create_entity("IfcSurfaceStyle", Name=group,
                                                        Side="BOTH", Styles=[shading])
        self.file.create_entity("IfcStyledItem", Item=item, Styles=[self.styles[group]])

    def mesh(self, vertices, faces, closed=True):
        vertices = np.asarray(vertices, dtype=float)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
            raise ValueError("Asset geometry contains invalid coordinates")
        if len(vertices) < 3 or not faces:
            raise ValueError("Asset has no usable source geometry")
        coordinates = self.file.create_entity("IfcCartesianPointList3D", CoordList=vertices.tolist())
        return self.file.create_entity("IfcTriangulatedFaceSet", Coordinates=coordinates,
                                       Closed=closed, CoordIndex=[[int(i) + 1 for i in face] for face in faces])

    def profile(self, coordinates, name):
        coordinates = np.asarray(coordinates, dtype=float)
        if not np.isfinite(coordinates).all():
            raise ValueError("Asset profile contains invalid coordinates")
        curve = self.file.create_entity("IfcPolyline", Points=[self.point(p) for p in
                                                               [*coordinates, coordinates[0]]])
        return self.file.create_entity("IfcArbitraryClosedProfileDef", ProfileType="AREA",
                                       ProfileName=name, OuterCurve=curve)

    def extrusion(self, profile, position, depth):
        return self.file.create_entity("IfcExtrudedAreaSolid", SweptArea=profile, Position=position,
                                       ExtrudedDirection=self.direction((0., 0., 1.)), Depth=float(depth))

    def sleeper(self, parameters, spec):
        tracks = self.contract.get("tracks")
        if tracks:
            track = next(track for track in tracks if track["id"] == str(parameters["track"]))
            rail = next(rail for rail in spec["rails"] if rail["id"] == track["rail_ids"][0])
        else:
            rail = spec["rails"][(parameters["track"] - 1) * 2]
        rotation = math.atan(float(np.polyval(np.polyder(rail["v_coefficients"]), parameters["u"])))
        return self.box(parameters, rotation=rotation, slope=parameters["crossfall"])

    def box(self, parameters, rotation=0., slope=0.):
        width = parameters["width_u"]
        length = parameters.get("length_v", parameters.get("width_v"))
        depth = parameters["depth"]
        profile = self.profile([[-length / 2, -depth - slope * length / 2],
                                [length / 2, -depth + slope * length / 2],
                                [length / 2, slope * length / 2],
                                [-length / 2, -slope * length / 2]], parameters["id"])
        u_dir = geometry.world([math.cos(rotation), math.sin(rotation), 0.], self.alignment)
        v_dir = geometry.world([-math.sin(rotation), math.cos(rotation), 0.], self.alignment)
        centre = geometry.world([parameters["u"], parameters["v"], parameters["z_top"]], self.alignment)
        position = self.axis(centre - u_dir * width / 2, u_dir, v_dir)
        return self.extrusion(profile, position, width)

    def mast(self, parameters):
        width_u, width_v = parameters["shaft_section_m"]
        thickness = .016
        profile = self.profile([
            [-width_u / 2, -width_v / 2], [width_u / 2, -width_v / 2],
            [width_u / 2, -width_v / 2 + thickness], [thickness / 2, -width_v / 2 + thickness],
            [thickness / 2, width_v / 2 - thickness], [width_u / 2, width_v / 2 - thickness],
            [width_u / 2, width_v / 2], [-width_u / 2, width_v / 2],
            [-width_u / 2, width_v / 2 - thickness], [-thickness / 2, width_v / 2 - thickness],
            [-thickness / 2, -width_v / 2 + thickness], [-width_u / 2, -width_v / 2 + thickness],
        ], "Source fitted mast H envelope")
        rotation = parameters.get("section_rotation_rad", 0.)
        reference = geometry.world([math.cos(rotation), math.sin(rotation), 0.], self.alignment)
        bottom = geometry.world([parameters["u"], parameters["v"], parameters["z_bottom"]], self.alignment)
        return self.extrusion(profile, self.axis(bottom, ref=reference),
                              parameters["z_top"] - parameters["z_bottom"])

    def straight_tube(self, parameters):
        start, end = geometry.world([parameters["start_uvz"], parameters["end_uvz"]], self.alignment)
        direction = end - start
        length = np.linalg.norm(direction)
        if length <= 1e-12:
            raise ValueError("Source member has zero length")
        direction /= length
        normal = np.cross(direction, [0., 0., 1.])
        if np.linalg.norm(normal) < .01:
            normal = np.cross(direction, [0., 1., 0.])
        normal /= np.linalg.norm(normal)
        profile = self.file.create_entity("IfcCircleProfileDef", ProfileType="AREA",
                                          ProfileName="Assumed source member radius",
                                          Radius=float(parameters["radius_m"]))
        return self.extrusion(profile, self.axis(start, direction, normal), length)

    def representation(self, record, spec, terrain=None):
        group, parameters = record["group"], record["parameters"]
        representation_type = "SweptSolid"
        if group == "rails":
            item = self.mesh(*geometry.rail_mesh(parameters, self.alignment,
                             self.contract.get("sampling", {}).get("rail_sample_count")))
            representation_type = "Tessellation"
        elif group == "sleepers":
            item = self.sleeper(parameters, spec)
        elif group == "masts":
            item = self.mast(parameters)
        elif group == "counterweights":
            item = self.box(parameters)
        elif group in ("supports", "droppers"):
            item = self.straight_tube(parameters)
        elif group == "wires":
            path = np.asarray(parameters["path_uvz"], dtype=float).copy()
            path[:, 1:] += np.asarray(record.get("strand_offset_vz_m", [0., 0.]))
            item = self.mesh(*geometry.tube_mesh(path, parameters["radius_m"], self.alignment))
            representation_type = "Tessellation"
        elif group == "troughs":
            item = self.mesh(*geometry.trough_mesh(parameters, self.alignment))
            representation_type = "Tessellation"
        elif group == "terrain":
            item = self.mesh(*terrain[record["asset_id"]], closed=False)
            representation_type = "Tessellation"
        else:
            raise ValueError("No geometry author for group " + group)
        self.style(item, group)
        shape = self.file.create_entity("IfcShapeRepresentation", ContextOfItems=self.body_context,
                                        RepresentationIdentifier="Body", RepresentationType=representation_type,
                                        Items=[item])
        return self.file.create_entity("IfcProductDefinitionShape", Representations=[shape])

    def asset_properties(self, record):
        return {
            "AssetId": record["asset_id"], "SourceComponentId": record["source_component_id"],
            "SourceSHA256": self.sha, "SourceFile": self.header["source"],
            "SourcePointCount": self.header["point_count"],
            "ReconstructionGroup": record["group"], "ParametersJSON": _json(record["parameters"]),
            "ParameterStatusJSON": _json(record["parameter_status"]),
            "StrandOffsetVZJSON": _json(record.get("strand_offset_vz_m", [0., 0.])),
            "AssumptionsJSON": _json(record["assumptions"]), "Revision": record["revision"],
            "EditedFieldsJSON": _json(record["edited_fields"]),
            "HorizontalCRSStatus": self.reference["horizontal"]["status"],
            "VerticalDatumStatus": self.reference["vertical"]["status"],
        }


def _records(spec, identity_seed, contract):
    statuses = {
        "rails": {"v_coefficients": "DERIVED", "z_coefficients": "DERIVED",
                  "head_width_m": "MEASURED", "hidden_web_foot_profile": "ASSUMED"},
        "sleepers": {"u": "MEASURED", "v": "DERIVED", "z_top": "MEASURED",
                     "crossfall": "DERIVED", "width_u": "ASSUMED", "length_v": "ASSUMED", "depth": "ASSUMED"},
        "masts": {"u": "MEASURED", "v": "MEASURED", "z_top": "MEASURED", "z_bottom": "MEASURED",
                  "shaft_section_m": "DERIVED", "section_rotation_rad": "DERIVED", "flange_thickness_m": "ASSUMED"},
        "wires": {"path_uvz": "DERIVED", "radius_m": "ASSUMED", "role": "SOURCE_CLASSIFICATION"},
        "supports": {"start_uvz": "DERIVED", "end_uvz": "DERIVED", "radius_m": "ASSUMED"},
        "droppers": {"start_uvz": "MEASURED", "end_uvz": "MEASURED", "radius_m": "ASSUMED"},
        "troughs": {"path_uvz": "DERIVED", "width_m": "MEASURED", "depth_m": "ASSUMED"},
        "counterweights": {"u": "MEASURED", "v": "MEASURED", "z_top": "MEASURED",
                           "width_u": "MEASURED", "width_v": "MEASURED", "depth": "MEASURED"},
        "terrain": {"grid_step_m": "ASSUMED", "surface": "DERIVED", "gap_interpolation": "DERIVED"},
    }
    assumption_names = {"rails": "Rail profile", "sleepers": "Sleepers", "masts": "Masts",
                        "wires": "Wires", "supports": "Overhead support members",
                        "droppers": "Overhead support members", "troughs": "Cable troughs",
                        "counterweights": "Tension counterweights", "terrain": "Terrain"}
    records = []
    rail_tracks = {rail: track["id"] for track in contract["tracks"] for rail in track["rail_ids"]}
    for group in ("rails", "sleepers", "masts", "wires", "supports", "droppers", "troughs", "counterweights"):
        for index, parameters in enumerate(spec.get(group, [])):
            asset_id = parameters["id"]
            if group == "rails":
                ifc_class, predefined, role, part = "IfcRail", "RAIL", "RUNNING_RAIL", "track-" + rail_tracks[asset_id]
            elif group == "sleepers":
                ifc_class, predefined, role, part = "IfcTrackElement", "SLEEPER", "SLEEPER", "track-" + str(parameters["track"])
            elif group == "masts":
                ifc_class, predefined, role, part = "IfcColumn", "USERDEFINED", "OVERHEAD_LINE_MAST", "lineside"
            elif group == "wires":
                role = parameters["role"]
                predefined = ("CONTACTWIRESEGMENT" if role == "contact" else
                              "CONDUCTORSEGMENT" if role == "upper feeder" else "USERDEFINED")
                ifc_class, part = "IfcCableSegment", "overhead"
            elif group == "supports":
                role = parameters.get("role", "support bracket")
                ifc_class, predefined = (("IfcCableSegment", "CONDUCTORSEGMENT") if role == "conductor continuation"
                                         else ("IfcMember", "MEMBER"))
                part = "overhead"
            elif group == "droppers":
                ifc_class, predefined, role, part = "IfcCableCarrierSegment", "DROPPER", "OVERHEAD_DROPPER", "overhead"
            elif group == "troughs":
                ifc_class, predefined, role, part = "IfcCableCarrierSegment", "CABLETRUNKINGSEGMENT", "CONCRETE_CABLE_TROUGH_ENVELOPE", "lineside"
            else:
                ifc_class, predefined, role, part = "IfcBuildingElementProxy", "USERDEFINED", "TENSION_COUNTERWEIGHT_STACK", "lineside"
            offsets = parameters.get("strand_offsets_vz_m", [[0., 0.]]) if group == "wires" else [[0., 0.]]
            for strand, offset in enumerate(offsets):
                identity = asset_id + " strand " + str(strand + 1) if len(offsets) > 1 else asset_id
                record = {"asset_id": identity, "ifc_guid": _guid(identity_seed, "asset:" + identity),
                          "ifc_class": ifc_class, "predefined_type": predefined, "object_type": role,
                          "group": group, "spatial_part_id": part, "source_component_id": asset_id,
                          "parameters": copy.deepcopy(parameters), "original_parameters": copy.deepcopy(parameters),
                          "parameter_status": copy.deepcopy(statuses[group]), "revision": 0, "edited_fields": [],
                          "assumptions": [copy.deepcopy(a) for a in spec.get("assumptions", [])
                                          if a["component"] == assumption_names[group]]}
                if group == "wires":
                    record["strand_offset_vz_m"] = list(offset)
                records.append(record)
    for region in contract.get("terrain_regions", []):
        asset_id = region["id"]
        records.append({"asset_id": asset_id, "ifc_guid": _guid(identity_seed, "asset:" + asset_id),
                        "ifc_class": "IfcGeographicElement", "predefined_type": "TERRAIN",
                        "object_type": region.get("object_type", "FITTED_TERRAIN_SURFACE"),
                        "group": "terrain", "spatial_part_id": "substructure", "source_component_id": asset_id,
                        "parameters": copy.deepcopy(spec["terrain"]), "original_parameters": copy.deepcopy(spec["terrain"]),
                        "parameter_status": copy.deepcopy(statuses["terrain"]), "revision": 0, "edited_fields": [],
                        "assumptions": [copy.deepcopy(a) for a in spec.get("assumptions", []) if a["component"] == "Terrain"]})
    if len({r["asset_id"] for r in records}) != len(records):
        raise ValueError("Source asset IDs must be unique")
    return records


def _write_bundle(file, ifc_path, project, register):
    """Stage all three files, then restore the previous bundle on write failure."""
    project_path, register_path = _sidecars(ifc_path)
    targets = [Path(ifc_path), project_path, register_path]
    if any(path.is_symlink() for path in targets):
        raise ValueError("Output files must not be symbolic links")
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    staged, backups, replaced = [], {}, []
    try:
        for path in targets:
            descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
            os.close(descriptor)
            staged.append(Path(temporary))
        file.write(str(staged[0]))
        staged[1].write_text(json.dumps(project, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        staged[2].write_text(json.dumps(register, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        for path in targets:
            if path.exists():
                descriptor, backup = tempfile.mkstemp(prefix="." + path.name + ".backup.", dir=path.parent)
                os.close(descriptor)
                backup_path = Path(backup)
                backup_path.write_bytes(path.read_bytes())
                backups[path] = backup_path
        for target, stage in zip(targets, staged):
            os.replace(stage, target)
            replaced.append(target)
    except Exception:
        for path in reversed(replaced):
            if path in backups:
                os.replace(backups.pop(path), path)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
        for path in backups.values():
            path.unlink(missing_ok=True)


def build_ifc(reconstruction_dir, output_ifc, *, overwrite=False, project_contract=None):
    """Create an IFC delivery using explicit project identity and track associations."""
    root, work, spec, alignment, header, contract = _load_inputs(reconstruction_dir, project_contract)
    output = Path(output_ifc).expanduser().resolve()
    if output.suffix.lower() != ".ifc":
        raise ValueError("The output path must end in .ifc")
    project_path, register_path = _sidecars(output)
    if not overwrite and any(path.exists() for path in (output, project_path, register_path)):
        raise FileExistsError("Railway BIM output already exists. Choose another path or explicitly enable overwrite.")
    source_path = Path(header["source"]).expanduser()
    if not source_path.is_absolute():
        source_path = work / source_path
    header = copy.deepcopy(header)
    header["source"] = os.path.relpath(source_path.resolve(), output.parent)
    reference = contract["reference"]
    horizontal, vertical = reference["horizontal"], reference["vertical"]
    records = _records(spec, contract["project_id"], contract)
    alignment_ids = {track["id"]: track.get("alignment_asset_id", "Track " + track["id"] + " measured alignment")
                     for track in contract["tracks"]}
    if any(not isinstance(asset_id, str) or not asset_id for asset_id in alignment_ids.values()):
        raise ValueError("Alignment asset IDs must be nonempty strings")
    asset_ids = [record["asset_id"] for record in records] + list(alignment_ids.values())
    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("Physical and alignment asset IDs must be unique")
    file = ifcopenshell.file(schema=IFC_SCHEMA)
    file.header.file_name.name = output.name
    file.header.file_name.preprocessor_version = "Railway BIM pilot 0.1"
    file.header.file_name.originating_system = "Railway BIM for Blender/Bonsai"
    author = _Author(file, header, alignment, contract=contract)
    project = author.root("IfcProject", "project", Name=contract.get("name", "Railway as-built project"),
                          Description="Measured source reconstruction with recorded reference evidence.")
    metre = file.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    square = file.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
    cubic = file.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE")
    radians = file.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN")
    project.UnitsInContext = file.create_entity("IfcUnitAssignment", Units=[metre, square, cubic, radians])
    context = file.create_entity("IfcGeometricRepresentationContext", ContextIdentifier="Model", ContextType="Model",
                                 CoordinateSpaceDimension=3, Precision=1e-6, WorldCoordinateSystem=author.axis())
    project.RepresentationContexts = [context]
    author.body_context = file.create_entity("IfcGeometricRepresentationSubContext", ContextIdentifier="Body",
                                             ContextType="Model", ParentContext=context, TargetView="MODEL_VIEW")
    author.axis_context = file.create_entity("IfcGeometricRepresentationSubContext", ContextIdentifier="Axis",
                                             ContextType="Model", ParentContext=context, TargetView="GRAPH_VIEW")
    crs = file.create_entity("IfcProjectedCRS", Name=horizontal.get("crs", "UNKNOWN"),
                             Description=horizontal.get("description", horizontal.get("evidence")),
                             GeodeticDatum=horizontal.get("geodetic_datum"),
                             VerticalDatum=vertical.get("datum") if vertical["status"] != "UNKNOWN" else None,
                             MapProjection=horizontal.get("map_projection"), MapUnit=metre)
    origin = header["origin"]
    file.create_entity("IfcMapConversion", SourceCRS=context, TargetCRS=crs,
                       Eastings=float(origin[0]), Northings=float(origin[1]), OrthogonalHeight=float(origin[2]),
                       XAxisAbscissa=1., XAxisOrdinate=0., Scale=1.)
    site = author.root("IfcSite", "site", Name="Source railway crop", CompositionType="ELEMENT",
                       ObjectPlacement=author.placement())
    railway = author.root("IfcRailway", "railway", Name="Railway as-built crop", CompositionType="ELEMENT",
                          PredefinedType="NOTDEFINED", ObjectPlacement=author.placement(site.ObjectPlacement))
    parts = {}
    track_parts = [("track-" + track["id"], track.get("name", "Track " + track["id"]),
                    "TRACK", "LONGITUDINAL") for track in contract["tracks"]]
    for key, name, predefined, usage in track_parts + [
        ("lineside", "Lineside equipment", "LINESIDE", "LATERAL"),
        ("overhead", "Overhead equipment", "ABOVETRACK", "VERTICAL"),
        ("substructure", "Terrain and formation", "SUBSTRUCTURE", "VERTICAL"),
    ]:
        parts[key] = author.root("IfcRailwayPart", "part:" + key, Name=name, CompositionType="PARTIAL",
                                 PredefinedType=predefined, UsageType=usage,
                                 ObjectPlacement=author.placement(railway.ObjectPlacement))
    author.aggregate(project, [site])
    author.aggregate(site, [railway])
    author.aggregate(railway, list(parts.values()))
    assumptions = copy.deepcopy(spec.get("assumptions", [])) + [
        {"component": "Survey coordinates", "note": "Survey facts and their evidence are recorded in the project reference contract."},
        {"component": "Measured alignments", "note": "Each path averages its source rail pair over their common fitted interval. Distances along these paths are local; official chainage, design alignment and gauge are unknown."},
        {"component": "IFC mappings", "note": "Railway product roles follow the source labels. Messenger or auxiliary wire roles remain unresolved. Terrain and trough geometry are fitted outer surfaces and envelopes."},
    ]
    project_path, register_path = _sidecars(output)
    author.pset(project, "Pset_RailwayBIMProject", {
        "ProjectSchema": PROJECT_SCHEMA, "ProjectId": contract["project_id"],
        "SourceSHA256": header["sha256"], "SourceFile": header["source"],
        "SourcePointCount": header["point_count"], "LocalOriginJSON": _json(origin),
        "CoordinateRule": "Global XYZ = source-local XYZ + recorded origin. UVZ fitting basis is rotated into source-local XYZ.",
        "HorizontalCRS": horizontal.get("crs", "UNKNOWN"), "HorizontalCRSStatus": horizontal["status"],
        "VerticalDatumStatus": vertical["status"], "ReferenceJSON": _json(reference),
        "OfficialChainageStatus": reference["official_chainage"]["status"],
        "DesignAlignmentStatus": reference.get("design_alignment", {}).get("status", "UNKNOWN"),
        "GaugeStatus": reference.get("gauge", {}).get("status", "UNKNOWN"),
        "AssumptionsJSON": _json(assumptions), "ProjectFile": project_path.name, "AssetRegisterFile": register_path.name,
    })
    terrain = (geometry.terrain_meshes(work / "terrain.npz", spec, alignment, contract["terrain_regions"])
               if spec.get("terrain") else {})
    contained = {key: [] for key in parts}
    products = {}
    for record in records:
        product = author.root(record["ifc_class"], "asset:" + record["asset_id"],
                              Name=record["asset_id"], Tag=record["asset_id"], ObjectType=record["object_type"],
                              PredefinedType=record["predefined_type"],
                              ObjectPlacement=author.placement(parts[record["spatial_part_id"]].ObjectPlacement),
                              Representation=author.representation(record, spec, terrain))
        author.pset(product, ASSET_PSET, author.asset_properties(record))
        products[record["asset_id"]] = product
        contained[record["spatial_part_id"]].append(product)
    sleepers = [products[r["asset_id"]] for r in records if r["group"] == "sleepers"]
    if sleepers:
        sleeper_type = author.root("IfcTrackElementType", "type:sleeper", Name="Source fitted concrete sleeper",
                                   PredefinedType="SLEEPER", Description="Repeated source sleeper. Buried dimensions may be assumptions.")
        author.root("IfcRelDefinesByType", "type-rel:sleepers", RelatingType=sleeper_type, RelatedObjects=sleepers)
    alignment_records = []
    rail_index = {rail["id"]: rail for rail in spec["rails"]}
    for track in contract["tracks"]:
        track_id = track["id"]
        asset_id = alignment_ids[track_id]
        parameters = {"track": track_id, "rails": [copy.deepcopy(rail_index[rail_id]) for rail_id in track["rail_ids"]],
                      "method": "Mean fitted rail-head path on the pair's common observed interval"}
        points = geometry.measured_alignment(parameters, alignment,
                    contract.get("sampling", {}).get("alignment_sample_count"))
        curve = file.create_entity("IfcPolyline", Points=[author.point(point) for point in points])
        representation = file.create_entity("IfcShapeRepresentation", ContextOfItems=author.axis_context,
                                             RepresentationIdentifier="Axis", RepresentationType="Curve3D", Items=[curve])
        alignment_product = author.root("IfcAlignment", "asset:" + asset_id, Name=asset_id,
                                         ObjectType="MEASURED_AS_BUILT_RAIL_PAIR_PATH", PredefinedType="USERDEFINED",
                                         ObjectPlacement=author.placement(parts["track-" + track_id].ObjectPlacement),
                                         Representation=file.create_entity("IfcProductDefinitionShape", Representations=[representation]))
        record = {"asset_id": asset_id, "ifc_guid": alignment_product.GlobalId, "ifc_class": "IfcAlignment",
                  "predefined_type": "USERDEFINED", "object_type": "MEASURED_AS_BUILT_RAIL_PAIR_PATH",
                  "group": "alignments", "spatial_part_id": "track-" + track_id, "source_component_id": asset_id,
                  "parameters": parameters, "original_parameters": copy.deepcopy(parameters),
                  "parameter_status": {"path": "DERIVED", "official_chainage": "UNKNOWN", "design_alignment": "UNKNOWN"},
                  "revision": 0, "edited_fields": [], "assumptions": [assumptions[-2]]}
        author.pset(alignment_product, ASSET_PSET, author.asset_properties(record))
        alignment_records.append(record)
        contained["track-" + track_id].append(alignment_product)
    for key, elements in contained.items():
        author.contain(parts[key], elements)
    register = {"schema": REGISTER_SCHEMA, "project_id": contract["project_id"],
                "ifc_schema": IFC_SCHEMA, "ifc_path": output.name,
                "source_sha256": header["sha256"], "asset_count": len(records), "alignment_count": len(alignment_records),
                "assets": records, "alignments": alignment_records}
    project_data = {"schema": PROJECT_SCHEMA, "contract": contract,
                    "ifc_schema": IFC_SCHEMA, "ifc_path": output.name,
                    "reconstruction_dir": os.path.relpath(root, output.parent),
                    "work_dir": os.path.relpath(work, output.parent), "asset_register_path": register_path.name,
                    "source_header": header, "source_component_spec": spec, "alignment": alignment,
                    "terrain_path": os.path.relpath(work / "terrain.npz", output.parent) if spec.get("terrain") else None,
                    "assumptions": assumptions,
                    "horizontal_crs_status": horizontal["status"], "vertical_datum_status": vertical["status"],
                    "assets": copy.deepcopy(records), "alignments": copy.deepcopy(alignment_records),
                    "spatial_parts": {key: {"ifc_guid": part.GlobalId, "name": part.Name,
                                            "predefined_type": part.PredefinedType} for key, part in parts.items()}}
    _write_bundle(file, output, project_data, register)
    counts = {}
    for record in records:
        counts[record["ifc_class"]] = counts.get(record["ifc_class"], 0) + 1
    return {"status": "created", "output_ifc": str(output), "ifc_path": str(output),
            "project_path": str(project_path), "asset_register_path": str(register_path),
            "asset_count": len(records), "alignment_count": len(alignment_records), "counts": counts,
            "horizontal_crs_status": horizontal["status"], "vertical_datum_status": vertical["status"],
            "review_items": ["Review source fit, survey reference evidence, and hidden geometry before delivery."]}


def _update_pset(file, product, values):
    psets = []
    for relationship in product.IsDefinedBy:
        definition = relationship.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == ASSET_PSET:
            psets.append(definition)
    if len(psets) != 1:
        raise ValueError("Target needs exactly one Railway BIM asset property set")
    properties = {prop.Name: prop for prop in psets[0].HasProperties}
    for key, value in values.items():
        if key not in properties:
            raise ValueError("Target is missing property " + key)
        nominal = (file.create_entity("IfcInteger", value) if isinstance(value, int)
                   else file.create_entity("IfcText", str(value)))
        properties[key].NominalValue = nominal


def validate_asset_changes(asset_record, changes):
    """Validate edit input without writing files and return float values in metres."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Provide at least one approved dimension change")
    group = asset_record["group"]
    limits = EDIT_LIMITS.get(group, {})
    if set(changes) - set(limits):
        raise ValueError("Unsupported changes for " + group + ": " + ", ".join(sorted(set(changes) - set(limits))))
    for key, value in changes.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + " must be a finite number in metres")
        lower, upper = limits[key]
        if not lower <= value <= upper:
            raise ValueError(key + " must be between " + str(lower) + " and " + str(upper) + " metres")
    return {key: float(value) for key, value in changes.items()}


def rebuild_asset(ifc_path, asset_id, changes):
    """Change one asset's approved dimensions while preserving its IFC identity.

    Sleepers accept width_u, length_v and depth. Masts accept height_m. Rails
    accept head_width_m. Numeric bounds reject implausible accidental input.
    Unrelated IFC products and manually authored property sets remain in place.
    """
    output = Path(ifc_path).expanduser().resolve()
    project_path, register_path = _sidecars(output)
    project = json.loads(project_path.read_text())
    register = json.loads(register_path.read_text())
    if (project.get("schema"), register.get("schema")) not in {
            (PROJECT_SCHEMA, REGISTER_SCHEMA), (LEGACY_PROJECT_SCHEMA, LEGACY_REGISTER_SCHEMA)}:
        raise ValueError("Unsupported Railway BIM reconstruction sidecar version")
    matches = [record for record in register["assets"] if record["asset_id"] == asset_id]
    if len(matches) != 1:
        raise ValueError("Asset ID is missing or ambiguous: " + str(asset_id))
    record = matches[0]
    project_matches = [item for item in project["assets"] if item["asset_id"] == asset_id]
    if len(project_matches) != 1 or _json(project_matches[0]) != _json(record):
        raise ValueError("Project and asset register disagree about the target")
    changes = validate_asset_changes(record, changes)
    file = ifcopenshell.open(str(output))
    if file.schema_identifier != IFC_SCHEMA:
        raise ValueError("Asset editing requires " + IFC_SCHEMA)
    product = file.by_guid(record["ifc_guid"])
    if not product or not product.is_a(record["ifc_class"]):
        raise ValueError("The asset register does not match the IFC target")
    pset = ifcopenshell.util.element.get_psets(product).get(ASSET_PSET, {})
    if pset.get("AssetId") != asset_id or pset.get("SourceSHA256") != register["source_sha256"]:
        raise ValueError("The IFC target's source identity does not match its register")
    if pset.get("ParametersJSON") != _json(record["parameters"]) or pset.get("Revision") != record["revision"]:
        raise ValueError("The target's reconstruction parameters changed outside this project")
    changed = copy.deepcopy(record)
    for key, value in changes.items():
        if key == "height_m":
            changed["parameters"]["z_top"] = changed["parameters"]["z_bottom"] + float(value)
            changed["parameter_status"]["z_top"] = "USER_EDITED"
            changed["parameter_status"]["height_m"] = "USER_EDITED"
        else:
            changed["parameters"][key] = float(value)
            changed["parameter_status"][key] = "USER_EDITED"
    changed["revision"] += 1
    changed["edited_fields"] = sorted(set(changed["edited_fields"]) | set(changes))
    contexts = [context for context in file.by_type("IfcGeometricRepresentationSubContext")
                if context.ContextIdentifier == "Body" and context.ContextType == "Model"]
    if len(contexts) != 1:
        raise ValueError("The project needs one Body representation context")
    author = _Author(file, project["source_header"], project["alignment"], body_context=contexts[0],
                     contract=project.get("contract"))
    old_representation = product.Representation
    product.Representation = author.representation(changed, project["source_component_spec"])
    _update_pset(file, product, {
        "ParametersJSON": _json(changed["parameters"]), "ParameterStatusJSON": _json(changed["parameter_status"]),
        "Revision": changed["revision"], "EditedFieldsJSON": _json(changed["edited_fields"]),
    })
    if old_representation:
        ifcopenshell.util.element.remove_deep2(file, old_representation)
    record.clear()
    record.update(changed)
    project_matches[0].clear()
    project_matches[0].update(copy.deepcopy(changed))
    _write_bundle(file, output, project, register)
    return {"status": "updated", "asset_id": asset_id, "ifc_guid": product.GlobalId,
            "output_ifc": str(output), "ifc_path": str(output), "project_path": str(project_path),
            "asset_register_path": str(register_path), "parameters": changed["parameters"],
            "changed_fields": sorted(changes), "revision": changed["revision"]}
