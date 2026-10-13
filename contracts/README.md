# Shared project contract

`project.schema.json` describes the reference exchanged by the three plugins.
It separates a persistent project UUID from a scan observation fingerprint.
Railway BIM requires explicit running-rail pairs. Swiss GIS can prepare a
project before any tracks have been identified, so its track list starts empty.

Geometry uses local metres. `reference.local_origin_m` records the translation
from source-local XYZ to project coordinates. The current IFC writer supports
this translation and a right-handed fitted UV basis. It does not transform
between CRS definitions or height datums.

Every survey fact records `UNKNOWN`, `ASSUMED`, `DECLARED`, or `VERIFIED`.
Declarations and verified facts need an evidence description. A status is a
record of supplied evidence, not a survey certification performed by this code.
The Swiss crop remains `ASSUMED` for EPSG:2056 and `UNKNOWN` for its height datum.

Railway BIM stores the checked contract in version-two reconstruction
sidecars, together with fitted component evidence. Paths are relative to the
IFC delivery directory. Validation can compare the IFC with this embedded
evidence when original inputs are unavailable. It reports that it cannot check
the unavailable scan's fingerprint. Supplying a reconstruction directory adds
an independent comparison with those external files.

For a rescan, reuse `project_id` and the stable asset IDs for the same physical
assets. Update the observation hash and fitted parameters. These hashes describe
observations and do not determine new-project IFC GUIDs. Existing version-one
deliveries retain their original GUIDs during edits and remain readable.

Swiss GIS stores the contract in `project.json` and writes
`railway_project_contract_json` to the Blender scene. Existing origin fields
remain compatible, and contradictory origins are rejected.

Corridor plans use `railway-corridor-plan/1`. They embed the project reference,
the supplied geometry, and each section's coordinate envelope. Segment IDs are
scoped by project UUID and corridor ID, while physical asset IDs remain
project-wide. Overlap observations use `railway-segment-observation/1` and
declare their parameter frame. Reconciliation accepts project-local metre
parameters and records disagreements as conflicts instead of choosing a fit.
