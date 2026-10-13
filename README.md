# Railway scan to BIM

This repository contains three Blender extensions. Scan Viewer processes and displays point clouds, Swiss GIS Local aligns local Swiss survey and raster data, and Railway BIM authors editable IFC 4.3 assets in Bonsai.

The plugin folders and IDs remain stable. The first [railway IFC](railway_bim/output/railway_crop.ifc) and [Bonsai workspace](railway_bim/output/railway_crop.blend) remain at their original local paths. These generated files are excluded from Git. The workspace retains the complete 5,500,234-point crop.

| Plugin | Processing code | Blender entry point |
|---|---|---|
| [Scan Viewer](scan_viewer/README.md) | Rust library in `scan_viewer/src` | `scan_viewer/addon` |
| [Swiss GIS Local](swiss_gis/README.md) | Python package in `swiss_gis/src/swiss_gis` | `swiss_gis/addon` |
| [Railway BIM](railway_bim/README.md) | Python package in `railway_bim/src/railway_bim` | `railway_bim/addon` |

Shared wire and project contracts live in [contracts](contracts/). The [architecture notes](docs/architecture.md) explain memory limits, source evidence, package boundaries, and migration. Existing dataset descriptions and attribution are in [the dataset index](docs/datasets.md).

The [skill alignment audit](docs/skill-alignment.md) records the installed review skills, repairs and verification results.

The [automated review follow-up](docs/automated-review.md) records the CLI repair and fixes for project UUIDs, alignment identity and measured-path validation.

Install the portable development dependencies in an isolated Python 3.12 environment, then run the same checks as CI:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip check
.venv/bin/python scripts/check.py
```

Rust uses `rust-toolchain.toml` and the committed Cargo lockfile. Install Rust through rustup if the configured toolchain is unavailable. The portable suite exercises real worker processes, generated GIS inputs, IFC export and persisted edits, and extension package imports. It does not require our local scans or Blender.

Build the three supported macOS arm64 release ZIPs with:

```sh
for plugin in scan_viewer swiss_gis railway_bim
do
  .venv/bin/python scripts/build_plugin.py "$plugin" --blender /Applications/Blender.app/Contents/MacOS/Blender
done
```

Install the ZIPs through Blender's extension preferences. Railway BIM uses the IfcOpenShell provided by Bonsai; Swiss GIS bundles its separately locked CPython 3.13 wheels. The release builder stages each archive in a clean temporary directory.

Run installed-extension integration checks in background Blender:

```sh
.venv/bin/python scripts/check.py --suite blender --blender /Applications/Blender.app/Contents/MacOS/Blender
```

These checks need Blender with all three extensions and Bonsai enabled, plus the local pilot IFC. GitHub runs the portable suite on pushes and pull requests. The Blender workflow runs only when dispatched on a configured self-hosted runner.

Finish installation before starting the suite. Run checks serially against one Blender user profile; extension startup and registration can change its shared Python dependencies. The shared check command already runs Blender processes in sequence.

The original Scan Viewer Git history is retained in this repository. Local metadata backups for the former repositories are stored in `.repository-history/`. Scans, caches, local backups, generated models, and audit artifacts stay outside source history.
