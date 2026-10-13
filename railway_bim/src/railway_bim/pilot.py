"""Import the recorded Swiss crop's topology into an explicit project contract."""

import json
from pathlib import Path

from .contract import legacy_reference, validate_contract


SOURCE_SHA256 = "daa6966bf94f3f2f2db40cdb7e478eddb665f09b4f06161d36267440229ec069"


def import_swiss_crop(spec, header):
    """Adapt only the known pilot. Other observations need their own project.json."""
    if header.get("sha256") != SOURCE_SHA256:
        raise ValueError("Add project.json with a persistent project ID, tracks, and survey reference")
    value = json.loads(Path(__file__).with_name("pilot_contract.json").read_text())
    value["reference"] = legacy_reference(header)
    value["reference"]["horizontal"]["description"] = (
        "LV95 is inferred from the source filename and coordinate values. No CRS record exists in the scan.")
    value["reference"]["horizontal"]["geodetic_datum"] = "CH1903+"
    value["reference"]["horizontal"]["map_projection"] = "Swiss Oblique Mercator"
    return validate_contract(value, spec, header)
