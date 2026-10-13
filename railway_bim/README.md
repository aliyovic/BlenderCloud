# Railway BIM

Railway BIM builds an editable IFC 4.3 model from the existing fitted railway crop. It keeps persistent asset IDs, reconstruction dimensions, source fingerprints, and assumptions. Blender and Bonsai provide the authoring workspace; Scan Viewer and Swiss GIS remain separate extensions.

Version 0.1.2 uses the fitted evidence in `../infrastructure/reconstruction/work/`. It creates the railway model from those measurements. Automatic detection from an unfamiliar raw scan is a later stage.

The processing code lives in `src/railway_bim/`. The `addon/` folder contains
Blender operators and compatibility imports. Extension builds bundle the domain
package, so Blender and command-line tools use the same implementation.

## Open the pilot

- [Bonsai workspace](output/railway_crop.blend)
- [IFC 4.3 model](output/railway_crop.ifc)
- [Extension ZIP](dist/railway_bim-0.1.2-macos-arm64.zip)
- [Saved IFC validation](output/railway_crop.validation.json)
- [Asset register](output/railway_crop.asset-register.json)
- [Reconstruction project](output/railway_crop.railway-project.json)

The workspace has view layers named BIM model, Scan overlay, and Scan reference. The source scan retains all 5,500,234 points. Viewport sampling changes only the display.

Install the ZIP through Blender Preferences > Extensions > Install from Disk. Enable Bonsai first. The packaged extension targets Blender 5.1 on macOS Apple Silicon and uses Bonsai's installed IfcOpenShell. The processing library also runs in ordinary Python with the dependencies in `requirements.txt`.

## Build and edit

In the 3D View sidebar, open Railway BIM. Choose the fitted crop folder and an output IFC file, then choose Build railway IFC. Existing files are preserved unless Replace existing output is enabled.

Open IFC in Bonsai imports the generated model into a scene without an active IFC project and retains non-IFC scan objects. When another IFC is already open, open the generated workspace in a separate Blender window.

Select a generated sleeper, rail, or mast. Choose its reconstruction dimension, use Read selected dimension, enter a value in metres, and choose Apply dimension and save IFC. The operator saves other current IFC edits, changes only the selected asset, preserves its GUID, and refreshes its geometry. Dimension changes are marked as edited and require a new comparison with the scan. Sleepers and masts also have native extruded IFC representations that Bonsai can edit directly.

Check saved IFC reopens the file on disk. It checks IFC structure, EXPRESS rules, asset inventory, source references, and exported coordinates. Save Bonsai edits before running it. Save the Blender workspace separately after editing.

## Coordinates and evidence

Geometry uses local metres in the scan's original orientation. Add the source origin to recover the source coordinates:

```text
E 2689160.000 m
N 1254972.000 m
H     452.000 m
```

The IFC stores this offset in `IfcMapConversion`. EPSG:2056 is an assumption inferred from the scan filename. The scan has no CRS record and its height datum remains unknown. Property sets and validation reports retain those statuses. The offset transformation does not establish survey accuracy or transform height datums.

Each track has a measured polyline alignment. It represents the fitted rail-pair centreline, with sampled rail-top and cant evidence. Official chainage and design alignment parameters remain unknown. The model does not claim reconstructed clothoid parameters or an operator-approved gauge measurement.

`Pset_RailwayBIMAsset` stores each asset's ID, source component, source scan hash, reconstruction parameters, parameter statuses, and assumptions. Sidecars keep the complete project and asset register. Keep the IFC and its two sidecars together for dimension regeneration. The original fitting did not retain per-asset point memberships, so this version traces dimensions to the fitted component and original scan rather than individual point indices.

New reconstructions use `project.json` beside their `work` folder. Declare a
persistent project UUID, explicit rail pairs, terrain selectors, and reference
evidence using `../contracts/project.schema.json`. New IFC identities depend on
this project UUID and stable asset IDs. A new scan observation changes the
source hash while preserving those identities. The named Swiss crop importer
supplies the original crop's topology without putting it in the general writer.

New sidecars store relative paths and embed their fitted evidence. Validation
still works after the bundle moves and external inputs become unavailable. It
reports the missing original scan instead of claiming its hash was checked.
The original version-one IFC delivery remains readable, and its edits preserve
the original GUIDs. Its stored survey assumptions remain unchanged.

Hidden rail profile dimensions, sleeper buried depths, wire radii, and mast flange thickness retain their assumptions. Validation compares exported geometry with the fitted specification. Survey control, fresh point-to-model fitting checks, completeness, and clearance acceptance need separate evidence.

## Command-line tools

From the dataset folder:

```sh
python3 railway_bim/scripts/railway_bim.py build infrastructure/reconstruction railway_bim/output/railway_crop.ifc
python3 railway_bim/scripts/railway_bim.py validate railway_bim/output/railway_crop.ifc --reconstruction-dir infrastructure/reconstruction --report railway_bim/output/railway_crop.validation.json
python3 railway_bim/scripts/railway_bim.py edit railway_bim/output/railway_crop.ifc 'Mast 01' --changes '{"height_m": 12.8}'
```

The edit command writes the selected change to the IFC and sidecars. Reload the affected asset or reopen the model in Bonsai afterward. Use a copy when trying dimensions.

To build the IFC, validate it, and save the Blender workspace:

```sh
python3 railway_bim/scripts/run_pipeline.py --out railway_bim/output
```

Use a new output directory for another run. `--overwrite` explicitly replaces outputs. `--skip-blender` runs only IFC generation and validation. `--blender` selects another Blender executable.

To package the extension:

```sh
python3 railway_bim/scripts/build_extension.py
```

The ZIP includes the extension code and manifest. It excludes scans, generated models, and external dependency packages.

## Corridor extraction planning

Plan overlapping sections before extracting a longer corridor. Supply a JSON
geometry file with `bounds_local_m` as minimum and maximum XYZ corners, and
optionally `path_local_m` as a polyline in project-local metres. Without a path,
the planner follows the longest bounds axis. This fallback describes an
extraction envelope, not a measured track alignment.

```sh
python3 railway_bim/scripts/railway_bim.py plan-corridor project.json corridor-geometry.json corridor-plan.json --section-length 500 --overlap 20 --local-limit 1000
```

The saved plan contains clipped paths, section origins, global map origins,
local bounds, and relative input locators. Adjacent sections share the requested
overlap. Sections that exceed the local coordinate limit are rejected before
publishing a file. Route distances follow the supplied path and do not establish
official chainage. Segment IDs use the project UUID, a stable `corridor_id`, and
nominal route intervals. The default corridor ID is `main`; supply a different
ID in the geometry file for another corridor in the same project. Changing a
scan fingerprint does not change segment IDs.

The next extraction stage can publish `railway-segment-observation/1` files.
Each file records `project_id`, `segment_id`, `source_sha256`, `units: "metre"`,
`parameter_frame: "project-local"`, and an `assets` array. Each asset needs its
project-wide `asset_id`, integer `revision`, and fitted `parameters`. Normalize
segment coordinate bases before reconciliation. Keep the same asset ID in
overlapping sections.

```sh
python3 railway_bim/scripts/railway_bim.py reconcile-corridor corridor-plan.json reconciled-assets.json segment-01.json segment-02.json
```

Matching observations produce one asset with all its observation evidence.
Different revisions, fitted parameters, or IFC GUIDs produce a conflict report
and a failing exit status. Conflicting assets are withheld from the reconciled
asset list. The command does not select a revision or merge divergent fits.
These commands plan extraction and compare its supplied results. They do not
fit railway geometry from a raw scan.

## Verification

The focused tests exercise IFC delivery, persisted dimension edits, rejection of invalid changes, and a deliberately shifted rail that validation must detect. Blender checks reopen the saved workspace, verify complete source coordinates and colours, and test a real Bonsai geometry edit through IFC export and a separate-process import.

```sh
python3 -m pytest -q railway_bim/tests/test_ifc_delivery.py
```

Portable generated fixtures exercise one-track and three-track exports,
identity across scan observations, recorded reference facts, and relocated
delivery edits without the dataset:

```sh
python3 -m pytest -q railway_bim/tests/test_project_contract.py
```

The proof files beside the pilot record completed runs. Inspect their status before treating a check as passed. Unknown CRS, height datum, official chainage, and assumed dimensions produce review items even when export checks pass.

Source code is GPL-3.0-or-later. Bonsai and IfcOpenShell retain their own licences.
