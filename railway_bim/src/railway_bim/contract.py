"""Validate project identity, track associations, and recorded survey facts."""

from __future__ import annotations

import copy
import math
from pathlib import Path
import uuid


SCHEMA = "railway-project-contract/1"
STATUSES = {"UNKNOWN", "ASSUMED", "DECLARED", "VERIFIED"}


def validate_contract(value, spec, header):
    """Return a checked copy. Verification requires a caller-supplied evidence record."""
    if not isinstance(value, dict):
        raise ValueError("The project contract must be a JSON object")
    contract = copy.deepcopy(value)
    if contract.get("schema") != SCHEMA:
        raise ValueError("Unsupported railway project contract version")
    try:
        contract["project_id"] = str(uuid.UUID(contract["project_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The project needs a persistent UUID independent of scan observations") from exc
    if contract.get("units") != "metre":
        raise ValueError("Railway project geometry must use metre units")
    reference = contract.get("reference", {})
    if not isinstance(reference, dict):
        raise ValueError("The project reference must be a JSON object")
    origin = reference.get("local_origin_m")
    if not isinstance(origin, list) or len(origin) != 3 or not all(isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v) for v in origin):
        raise ValueError("The project reference needs three finite local-origin coordinates")
    if origin != header.get("origin"):
        raise ValueError("Project and fitted observation origins disagree")
    for key, field in (("horizontal", "crs"), ("vertical", "datum")):
        fact = reference.get(key, {})
        if not isinstance(fact, dict):
            raise ValueError("The " + key + " reference must be a JSON object")
        if fact.get("status") not in STATUSES:
            raise ValueError("Invalid " + key + " reference status")
        if fact["status"] != "UNKNOWN" and not fact.get(field):
            raise ValueError("The " + key + " reference needs a recorded value")
        if fact["status"] in {"DECLARED", "VERIFIED"} and not fact.get("evidence"):
            raise ValueError("The " + key + " reference status needs an evidence record")
    extent = reference.get("max_local_extent_m", 1000.0)
    if isinstance(extent, bool) or not isinstance(extent, (int, float)) or not math.isfinite(extent) or extent <= 0:
        raise ValueError("The local coordinate extent must be a positive finite distance")
    reference["max_local_extent_m"] = float(extent)
    reference.setdefault("official_chainage", {"status": "UNKNOWN"})
    for key in ("official_chainage", "design_alignment", "gauge"):
        if key not in reference:
            continue
        fact = reference[key]
        if fact.get("status") not in STATUSES:
            raise ValueError("Invalid " + key + " status")
        if fact["status"] in {"DECLARED", "VERIFIED"} and not fact.get("evidence"):
            raise ValueError(key + " needs an evidence record")
    rails = {rail["id"] for rail in spec.get("rails", [])}
    tracks, seen_ids, used_rails = contract.get("tracks", []), set(), []
    if not tracks:
        raise ValueError("Declare the project's track and rail associations")
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("Each track must be a JSON object")
        identity = track.get("id")
        pair = track.get("rail_ids", [])
        if not isinstance(identity, str) or not identity or identity in seen_ids:
            raise ValueError("Track IDs must be unique nonempty strings")
        if len(pair) != 2 or len(set(pair)) != 2 or not set(pair) <= rails:
            raise ValueError("Each measured track needs two distinct fitted rail IDs")
        seen_ids.add(identity)
        used_rails.extend(pair)
    if len(set(used_rails)) != len(used_rails) or set(used_rails) != rails:
        raise ValueError("Assign each fitted running rail to exactly one track")
    if any(str(sleeper.get("track")) not in seen_ids for sleeper in spec.get("sleepers", [])):
        raise ValueError("A sleeper refers to an undeclared track")
    regions = contract.get("terrain_regions", [])
    if bool(spec.get("terrain")) != bool(regions):
        raise ValueError("Declare terrain regions when the observation includes terrain")
    if len({region.get("id") for region in regions}) != len(regions) or any(not region.get("id") for region in regions):
        raise ValueError("Terrain region IDs must be unique and nonempty")
    contract["reference"] = reference
    return contract


def legacy_reference(header):
    """Read version-one deliveries without claiming new survey knowledge."""
    return {"local_origin_m": header["origin"],
            "horizontal": {"crs": "EPSG:2056", "status": "ASSUMED",
                           "evidence": "Source filename and coordinate values; no scan CRS record"},
            "vertical": {"datum": "UNKNOWN", "status": "UNKNOWN"},
            "official_chainage": {"status": "UNKNOWN"}, "max_local_extent_m": 1000.0}


def resolve_reference(base, value):
    """Resolve a local source locator relative to the delivered project files."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(base) / path
