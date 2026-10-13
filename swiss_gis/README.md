# Swiss GIS Local

Local data preparation lives in `src/swiss_gis/`; `addon/` owns Blender scene
creation and operators. The extension bundles the same domain package used by
the command-line tool. Existing addon imports remain supported.

New bundles include the shared `railway-project-contract/1` reference alongside
the existing manifest fields. A persistent project UUID identifies the project;
the reference records its origin, CRS and height-datum evidence. Supply
`--project-id` to `scripts/prepare_local.py` when another observation belongs to
the same project. Blender stores this contract and checks its origin against
the existing Scan Viewer and GIS origin fields.

Swiss GIS Local is a separate Blender extension for local Swiss LV95 datasets. It imports a bounded LAS or LAZ preview, reads nearby terrain and aerial GeoTIFFs, and places them using one geographic origin. It has no map service, API client, download button, or network permission. It does not depend on BlenderGIS or Scan Viewer.

The first example uses `../infrastructure/BRUT_LV95_000017_000018-Cloud_Cropped_agent.laz`. Its 5,500,234 measured points cover 68.013 by 51.169 metres. The selected context area adds 50 metres on each side, making an area of 168.013 by 151.169 metres.

[Open the railway with aerial and terrain context](examples/railway_swisstopo.blend). [Install the extension ZIP](dist/swiss_gis_local-0.1.3-macos-arm64.zip).

## Full scan with separate layers

The current [single combined view](examples/full_scan_combined.blend) shows the full scan over the local aerial photograph, with a coarse terrain wireframe in the same viewport. The original terrain mesh remains intact; a display modifier reduces wireframe density. All layers retain their geographic coordinates and separate collections.

[Open the full scan comparison](examples/full_scan_local_layers.blend). It uses the original OneDrive `BRUT_LV95_000017_000018-Cloud_Cropped.laz`, with 21,862,802 source points and a 211 by 133 metre extent. The context adds a 50 metre margin. Eight local GeoTIFF crops cover the resulting 311 by 233 metre area and occupy 15,471,784 bytes. Their receipt is in `data/swisstopo_full_scan/sources.json`.

The saved Blender layout has three independent top views and named collections. The left view shows measured RGB points, the middle view shows the SWISSIMAGE photograph, and the right view shows the swissALTI3D terrain mesh with elevation colours. These views keep identical geographic coordinates; the layout separates their visibility without translating geometry. The flat aerial map has no surveyed elevation.

The embedded overview contains 1,987,528 sampled points across the full source extent. The scene named `04 Full scan detail on zoom` retains the Scan Viewer link to the local 21,862,802-point detail cache. Exit local view in the viewport when switching to that scene. The scan still has no CRS record or known height datum. LV95 remains an explicit assumption, and terrain LN02 heights retain their source values.

Preparation, scene creation and saved-file verification ran with OS network access denied. [Full scan verification](artifacts/full_scan_check.json) records the coordinate comparison, raster hashes, complete terrain coverage, packed image and separate view persistence.

![Separate top views of the full scan, aerial map and terrain](artifacts/full_scan_separate_top_views.png)

## What the example contains

The saved scene contains a 687,530-point preview, source point IDs, RGB, classification, a hidden ground mesh with 7,418 quads, and a packed scan RGB plan image. Preview stride is 8. The complete source file stays intact and remains necessary for a new preview at a higher point budget.

The ground mesh uses the minimum class 2 height in each observed 0.5-metre cell. It leaves cells without measured ground open. It is context derived from scan classification, not a surveyed terrain model. The plan image uses the topmost scan RGB per cell. It is not an aerial photograph.

The [scan-only scene](examples/railway_local.blend) retains the original local preview. The [swisstopo scene](examples/railway_swisstopo.blend) adds independent aerial and terrain coverage for the selected area.

## Aerial and terrain context for this scan

A one-time transfer retained four GeoTIFF crops, totalling 5,462,984 bytes. They cover the scan and its 50-metre margin. Two SWISSIMAGE 2025 source tiles supply 0.1-metre imagery. Two swissALTI3D 2026 source tiles supply a 2-metre elevation grid. Their shared north tile boundary crosses the selected area at N 1,255,000 m.

The separate `scripts/download_area_once.py` reads selected byte ranges from four direct public GeoTIFF URLs. It does not use a geospatial API. The Blender extension has no network code or permission and loads the saved crops from `data/swisstopo_area/imagery/` and `data/swisstopo_area/terrain/`. The receipt in `data/swisstopo_area/sources.json` records URLs, source versions, local bounds, sizes, and hashes. Existing crops are preserved.

The prepared `data/railway_swisstopo/` bundle produces a complete 6,460-vertex terrain mesh with 6,300 quads. The aerial texture uses the same LV95 coordinates as the scan. The comparison below overlays four rail paths fitted from the scan onto the independent aerial image, without applying an alignment shift. This provides a visual check of horizontal placement. It does not establish survey accuracy or confirm the scan's height datum. The terrain is LN02; the scan still has an unknown vertical datum.

The saved top view shows the measured scan over the flat aerial map. Use the Swiss GIS sidebar to switch between the flat map and 3D terrain. The flat map is a display plane below the scan and has no surveyed elevation.

Source: Federal Office of Topography swisstopo. Product descriptions are available for [SWISSIMAGE](https://www.swisstopo.admin.ch/en/orthoimage-swissimage-10) and [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d). Attribution follows [swisstopo's terms of use](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices).

![Scan and measured rail paths over the local aerial image](artifacts/swisstopo_alignment_top_view.png)

![Railway scan preview](artifacts/area_preview.png)

## Install and use

The packaged build targets Blender 5.1 with Python 3.13 on Apple Silicon, macOS 15 or newer. It was installed and tested with Blender 5.1.1 on this Mac. Other Blender versions and platforms need matching dependency wheels and a rebuilt manifest.

1. In Blender Preferences, open Extensions, choose Install from Disk, and select the ZIP. Its dependencies are included. Installation does not need internet access.
2. In the 3D View, press N and open Swiss GIS. Select a local LAS or LAZ file and choose Read scan location.
3. Set the height datum only if the supplier confirms it. The example retains Unknown. The source has no CRS record, so its LV95 setting is a project assumption indicated by the filename.
4. Optionally choose separate folders containing terrain and aerial GeoTIFFs in EPSG:2056. The importer examines headers and reads windows only from tiles that intersect the selected area.
5. Keep Use scan bounds enabled and choose the surroundings margin, or disable it to enter an LV95 centre. With a manual centre, the margin is the half-width of a square. Choose Build local scene.
6. Use Area view, Top view, and the layer buttons to inspect the result. Export geographic reference writes the origin, axes, units, datum, source, and assumptions to JSON.

Each successful build adds a collection. Existing objects stay in place. Remove an earlier context collection in the Outliner before replacing it. Saved scenes embed the preview geometry and pack their images, so those displays survive missing source files or preparation bundles. A new build still needs the local source files.

The point radius setting applies on import. To change a loaded scan's radius, select it and edit Radius metres in its geometry nodes modifier.

## Local context files

Put terrain GeoTIFFs in one folder and RGB or RGBA aerial GeoTIFFs in another. The folders can contain multiple tiles. Files need an explicit LV95 CRS, a north-up raster transform, and metre elevations for terrain. Raster nodata and transparent aerial pixels stay empty. JPEG compression inside GeoTIFF works through bundled GDAL. Plain JPEG, PNG with world files, rotated rasters, VRT files, and reprojection are not supported in this version.

Use a digital terrain model such as locally stored swissALTI3D for ground context. A surface model includes objects above the ground and should be labelled accordingly. The importer treats the selected terrain folder as an elevation grid; it does not infer whether a raster is a terrain or surface model.

Known vertical datums must match. For terrain files with horizontal CRS only, a `VERTICAL_DATUM` GeoTIFF tag may declare `LN02` or `LHN95`. Otherwise the importer reports that height alignment is unverified. It never converts heights. Dataset attribution and acquisition dates remain the responsibility of the local file supplier.

## Coordinate accuracy and other add-ons

The example uses this origin, stored as JSON numbers with double precision:

```text
E 2689160.000 m
N 1254972.000 m
H     452.000 m

LV95 XYZ = Blender XYZ + origin
Blender X = east, Y = north, Z = up
```

The reader subtracts the origin in float64 before it writes float32 display geometry. Validation compared all 687,530 preview vertices with their original LAS point IDs. Maximum coordinate error was 0.000001892 metres, below 0.002 mm. This measures display conversion only. It does not establish survey accuracy, confirm LV95, or identify the scan's vertical datum.

The extension reuses existing `scan_scene_origin` or `LV95_origin_m` scene metadata, including the origin used by the railway reconstruction. It rejects conflicting origins instead of moving existing geometry. An established project origin farther than 10 km from the selected area requires another scene. The selected area is limited to 5 km per side and each output grid to four million cells.

The reference JSON preserves the information needed by an IFC exporter. This version does not write IFC or set Bonsai georeferencing automatically.

## Build and verify

Create a Python 3.13 environment and install `requirements.txt`. Tests also need pytest. The extension build downloads software dependency wheels once. It does not download geographic data.

```sh
/Applications/Blender.app/Contents/Resources/5.1/python/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest
.venv/bin/python scripts/build_extension.py
.venv/bin/python -m pytest -q tests/test_local_project.py
```

Reuse existing wheels for an offline build with `scripts/build_extension.py --reuse-wheels`. The installed extension uses Blender's NumPy and does not bundle a second copy.

To prepare local files without Blender:

```sh
.venv/bin/python scripts/prepare_local.py \
  ../infrastructure/BRUT_LV95_000017_000018-Cloud_Cropped_agent.laz \
  data/another_project --margin 50 --max-points 750000 \
  --terrain-folder /absolute/path/to/terrain \
  --imagery-folder /absolute/path/to/aerial
```

Existing bundle directories are preserved. Choose a new output directory for each preparation. Open its `project.json` using Open prepared local project in Blender.

The integration scripts require the installed extension. These commands ran on this Mac with OS network access denied:

```sh
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  /Applications/Blender.app/Contents/MacOS/Blender -b \
  --python-exit-code 1 --python tests/blender_integration.py
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  /Applications/Blender.app/Contents/MacOS/Blender -b \
  --python-exit-code 1 --python scripts/build_example.py
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  /Applications/Blender.app/Contents/MacOS/Blender -b examples/railway_local.blend \
  --python-exit-code 1 --python tests/check_railway.py
```

The fixture tests cover bounded raster windows, north/south image orientation, nodata holes, LAZ source IDs, point budgets, missing context, invalid CRS, vertical datum conflicts, cancellation, and existing bundle preservation. The Blender integration covers the real sidebar operators, terrain UVs, layer visibility, save/reload with the bundle unavailable, packed images, reference export, origin conflicts, and registration lifecycle. The actual railway check verifies every preview coordinate against the source LAZ. `tests/check_swisstopo.py` verifies the real crop hashes, selected bounds, source attribution, complete terrain across the tile seam, geographic UVs, and packed aerial image in the saved scene.

See [verification results](artifacts/verification.json) for the final runs. Source code is GPL-3.0-or-later. Bundled libraries retain the licenses included in their wheels.
