"""Plan local extraction sections and reconcile project-wide asset observations."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import uuid


PLAN_SCHEMA = "railway-corridor-plan/1"
OBSERVATION_SCHEMA = "railway-segment-observation/1"
RECONCILIATION_SCHEMA = "railway-corridor-reconciliation/1"


def _point(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        raise ValueError("Corridor coordinates need three finite numbers in project-local metres")
    return [float(v) for v in value]


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be a positive finite distance")
    return float(value)


def read_project(path):
    """Read a contract or an existing Railway BIM reconstruction sidecar."""
    document = json.loads(Path(path).read_text())
    if document.get("schema") == "railway-bim-project/1":
        from .pilot import import_swiss_crop

        return import_swiss_crop(document["source_component_spec"], document["source_header"])
    return document.get("contract", document)


def _path_distance(points):
    cumulative = [0.0]
    for first, last in zip(points, points[1:]):
        length = math.dist(first, last)
        if length <= 1e-9:
            raise ValueError("The supplied corridor path has a zero-length segment")
        cumulative.append(cumulative[-1] + length)
    return cumulative


def _interpolate(points, distances, station):
    for index in range(len(distances) - 1):
        if station <= distances[index + 1]:
            fraction = (station - distances[index]) / (distances[index + 1] - distances[index])
            return [a + fraction * (b - a) for a, b in zip(points[index], points[index + 1])]
    return list(points[-1])


def _section_path(points, distances, start, end):
    return [_interpolate(points, distances, start),
            *[list(point) for point, station in zip(points, distances) if start < station < end],
            _interpolate(points, distances, end)]


def plan_corridor(project, geometry, *, section_length_m=500.0, overlap_m=20.0,
                  max_local_extent_m=1000.0):
    """Plan sections from supplied bounds or a path, without reconstructing assets.

    Route distance follows this supplied polyline. It is not official chainage.
    Bounds provide a fixed padding envelope around each clipped path section.
    Adjacent sections share the requested overlap. Short terminal sections shift
    that shared interval inward to keep the full overlap inside the corridor.
    """
    if project.get("schema") != "railway-project-contract/1" or project.get("units") != "metre":
        raise ValueError("Corridor planning needs a supported project contract in metre units")
    try:
        project_uuid = uuid.UUID(project["project_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The corridor needs a persistent project UUID") from exc
    origin_global = _point(project.get("reference", {}).get("local_origin_m"))
    corridor_id = geometry.get("corridor_id", "main")
    if not isinstance(corridor_id, str) or not corridor_id:
        raise ValueError("The corridor ID must be a stable nonempty project-wide name")
    length = _positive(section_length_m, "Section length")
    limit = _positive(max_local_extent_m, "Local coordinate limit")
    if (isinstance(overlap_m, bool) or not isinstance(overlap_m, (int, float))
            or not math.isfinite(overlap_m) or not 0 <= overlap_m < length):
        raise ValueError("Corridor overlap must be finite and between zero and the section length")
    bounds = geometry.get("bounds_local_m")
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise ValueError("Supply minimum and maximum corridor bounds in project-local metres")
    lower, upper = map(_point, bounds)
    if any(high < low for low, high in zip(lower, upper)):
        raise ValueError("The corridor bounds are reversed")
    supplied_path = geometry.get("path_local_m")
    if supplied_path is None:
        axis = max(range(3), key=lambda index: upper[index] - lower[index])
        centre = [(low + high) / 2 for low, high in zip(lower, upper)]
        start, end = list(centre), list(centre)
        start[axis], end[axis] = lower[axis], upper[axis]
        points = [start, end]
        path_kind = "BOUNDS_AXIS"
    else:
        if not isinstance(supplied_path, list) or len(supplied_path) < 2:
            raise ValueError("A supplied corridor path needs at least two points")
        points = [_point(point) for point in supplied_path]
        path_kind = "SUPPLIED_POLYLINE"
    if any(any(value < low or value > high for value, low, high in zip(point, lower, upper)) for point in points):
        raise ValueError("The supplied corridor path lies outside its project-local bounds")
    distances = _path_distance(points)
    total = distances[-1]
    path_min = [min(point[axis] for point in points) for axis in range(3)]
    path_max = [max(point[axis] for point in points) for axis in range(3)]
    padding_low = [value - low for value, low in zip(path_min, lower)]
    padding_high = [high - value for value, high in zip(path_max, upper)]
    segment_count = math.ceil(total / length)
    if segment_count > 100_000:
        raise ValueError("The corridor plan exceeds 100,000 sections; increase section length")

    def shared_interval(boundary):
        shared_start = min(boundary - overlap_m / 2, total - overlap_m)
        return shared_start, shared_start + overlap_m

    segments = []
    for number in range(segment_count):
        start, end = number * length, min((number + 1) * length, total)
        observed_start = 0. if number == 0 else shared_interval(start)[0]
        observed_end = total if number == segment_count - 1 else shared_interval(end)[1]
        section = _section_path(points, distances, observed_start, observed_end)
        low = [min(point[axis] for point in section) - padding_low[axis] for axis in range(3)]
        high = [max(point[axis] for point in section) + padding_high[axis] for axis in range(3)]
        origin = [(first + last) / 2 for first, last in zip(low, high)]
        local_bounds = [[value - offset for value, offset in zip(corner, origin)] for corner in (low, high)]
        if max(abs(value) for corner in local_bounds for value in corner) >= limit:
            raise ValueError("A corridor section exceeds its local coordinate limit; shorten it or narrow its supplied bounds")
        identity = str(uuid.uuid5(project_uuid, f"corridor-section:{corridor_id}:{start:.9f}:{end:.9f}"))
        segments.append({
            "segment_id": identity, "nominal_route_interval_m": [start, end],
            "observed_route_interval_m": [observed_start, observed_end],
            "local_origin_in_project_m": origin,
            "map_origin_m": [value + offset for value, offset in zip(origin, origin_global)],
            "bounds_in_project_m": [low, high], "bounds_local_m": local_bounds,
            "path_local_m": [[value - offset for value, offset in zip(point, origin)] for point in section],
        })
    return {
        "schema": PLAN_SCHEMA, "project_id": str(project_uuid), "corridor_id": corridor_id, "units": "metre",
        "project_reference": copy.deepcopy(project["reference"]), "path_kind": path_kind,
        "geometry": {"corridor_id": corridor_id, "bounds_local_m": [lower, upper], "path_local_m": points},
        "route_length_m": total, "section_length_m": length, "overlap_m": float(overlap_m),
        "max_local_extent_m": limit, "route_distance_status": "SUPPLIED_PATH_DISTANCE",
        "segments": segments,
    }


def reconcile_observations(plan, observations):
    """Keep one project-wide asset only when all supplied observations agree.

    Parameters must already use the project-local frame. Normalizing fitted
    coordinate bases belongs to extraction; this function never guesses a
    transform or chooses a newer conflicting revision.
    """
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("Unsupported corridor plan version")
    if not observations:
        raise ValueError("Supply at least one segment observation for reconciliation")
    segment_ids = {segment["segment_id"] for segment in plan["segments"]}
    grouped = {}
    for observation in observations:
        if observation.get("schema") != OBSERVATION_SCHEMA or observation.get("project_id") != plan["project_id"]:
            raise ValueError("The segment observation belongs to another project or schema")
        if observation.get("segment_id") not in segment_ids:
            raise ValueError("The observation refers to a segment absent from the corridor plan")
        if observation.get("parameter_frame") != "project-local" or observation.get("units") != "metre":
            raise ValueError("Normalize segment parameters to project-local metre coordinates before reconciliation")
        fingerprint = observation.get("source_sha256", "")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("The segment observation needs its recorded source SHA-256 fingerprint")
        seen = set()
        for asset in observation.get("assets", []):
            identity, revision, parameters = asset.get("asset_id"), asset.get("revision"), asset.get("parameters")
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("Asset IDs must be unique within each observation and remain project-wide")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or not isinstance(parameters, dict):
                raise ValueError("Each observed asset needs a nonnegative revision and fitted parameters")
            json.dumps(parameters, allow_nan=False)
            seen.add(identity)
            grouped.setdefault(identity, []).append({
                "segment_id": observation["segment_id"], "source_sha256": fingerprint,
                "revision": revision, "parameters": copy.deepcopy(parameters),
                **({"ifc_guid": asset["ifc_guid"]} if "ifc_guid" in asset else {}),
            })
    assets, conflicts = [], []
    for identity, evidence in sorted(grouped.items()):
        reasons = [key for key in ("revision", "parameters", "ifc_guid")
                   if any(item.get(key) != evidence[0].get(key) for item in evidence[1:])]
        if reasons:
            conflicts.append({"asset_id": identity, "reasons": reasons, "observations": evidence})
        else:
            assets.append({"asset_id": identity, "revision": evidence[0]["revision"],
                           "parameters": evidence[0]["parameters"], "observations": evidence,
                           **({"ifc_guid": evidence[0]["ifc_guid"]} if "ifc_guid" in evidence[0] else {})})
    return {"schema": RECONCILIATION_SCHEMA, "project_id": plan["project_id"],
            "status": "conflict" if conflicts else "reconciled", "parameter_frame": "project-local",
            "units": "metre", "assets": assets, "conflicts": conflicts}


def write_document(document, path, *, references=None, overwrite=False):
    """Publish a complete plan or reconciliation with relative input locators."""
    output = Path(path).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError("Corridor output already exists; choose another path or enable overwrite")
    payload = copy.deepcopy(document)
    for key, value in (references or {}).items():
        payload[key] = ([os.path.relpath(Path(item).resolve(), output.parent) for item in value]
                        if isinstance(value, list) else os.path.relpath(Path(value).resolve(), output.parent))
    text = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".partial", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(output)
