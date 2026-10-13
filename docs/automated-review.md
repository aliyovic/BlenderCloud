# Automated review and repairs

The 2026-10-13 follow-up repaired the local CLI and addressed twenty confirmed findings in the railway stabilization changes. The final structured review is clean. It used the Codex engine and the existing `gpt-6.1-sol` model.

## CLI repair

The Homebrew CLI was 0.155.1. Updating it to 0.162.0 allowed the configured model to complete the review request. The shared configuration file, model, reasoning setting and authentication were unchanged. The old executable is backed up in ignored local artifacts.

[Official OpenAI documentation](https://learn.chatgpt.com/docs/models) lists `gpt-6.1-sol` for the CLI and explains that availability depends on the client and sign-in method. Local receipts record both binary hashes and identical configuration hashes before and after the upgrade.

The first model call after upgrade completed, but the review helper rejected absolute filenames in the JSON report. The next invocation specified repository-relative `code_location.file_path` values and produced a valid report with three findings. No review engine or model substitution was needed.

## Confirmed findings

| Finding | Repair | Primary regression |
|---|---|---|
| Shared track labels or explicit alignment IDs could produce duplicate identities and overwrite an invalid delivery. | Default alignment IDs now use stable track IDs. The writer rejects collisions across physical assets and alignments before authoring or publication. | Shared labels, rename stability, explicit IDs and unchanged files after rejected overwrite in `test_project_contract.py` |
| Preparing another GIS observation could replace the scene's persistent project UUID. | The Build operator validates and reuses the existing scene UUID. A scene without a project identity gets a fresh UUID. | Real Blender Build operations for two observations, an independent scene and invalid saved identity in `check_project_identity.py` |
| IFC validation counted alignments but accepted changed paths or GUIDs. | Validation checks both alignment sidecars, IFC identities, rail associations, placement and the full measured path against fitted rail evidence. | Saved IFC mutations for translation, an interior bend and a changed GUID in `test_project_contract.py` |
| The first path repair still accepted a straight chord after removing a curved alignment's interior vertices. | Validation also requires the recorded sampling count and fitted station positions. | A curved rail pair whose saved alignment retains only its original endpoints |
| Nodes above 64 MiB could be evicted before upload and decoded repeatedly at the old minimum cache setting. | The minimum CPU cache is 128 MiB, enough for the largest accepted node. Streaming also migrates smaller values saved in older scenes. | Real-worker large-node delivery at the lowest supported scene setting |
| Supported dimension edits disabled later geometry checks. | Checks use the current recorded parameters and retain the scan-fit review flag. | Valid mast, sleeper and rail edits followed by saved placement corruption |
| Rail endpoint checks missed interior section movement. | Independent polynomial evaluation checks every recorded cross-section and sampling station. | An interior rail section moved while both end sections and metadata remain unchanged |
| Opening a prepared GIS bundle could replace the scene UUID at an unchanged origin. | Build and Open share the saved UUID reader. Bundle imports reject a conflicting project identity before changing the scene. | Real Open operations reject a different UUID and accept another observation with the existing UUID |
| A saved rail profile could disagree with a recorded head-width edit while keeping its centre and top height. | Validation measures the saved head flanks. Recorded dimension checks also cover mast height and sleeper width, length and depth. | Restored old head width, centred mast height and centred sleeper depth after valid edits |
| IFC Open retained scan objects with a different geographic anchor. | The importer checks the established scene origin, project identity, metre units and translation-only map frame before Bonsai loads. It records a checked anchor and optional supplied project contract after successful import. | Real Scan Viewer and Bonsai operators reproduce the 160/972/452 m displacement, reject mismatches without mutation, accept matching imports and share the reference with later GIS preparation |
| GIS preparation kept the UUID but erased saved tracks, sampling and reference evidence. | Scene publication retains the saved contract and merges compatible supplied facts. It rejects conflicting topology and known coordinate references before changing the scene. | Actual pilot IFC import followed by GIS Build and prepared Open, with independently authored survey coordinates |
| IFC Open accepted conflicting known height datums at matching numeric origins. | The importer compares known IFC, sidecar and scene height declarations before loading. Unknown references remain compatible. | Valid domain-built height-reference deliveries, rejected conflicts without scene mutation, and matching or unknown reference imports |
| Saved IFC validation missed a project UUID that disagreed with both sidecars. | Validation compares the actual IFC project property with the normalized contract and asset-register UUID. Legacy deliveries without the property remain supported. | A changed IFC UUID fails while asset inventory still passes; equivalent UUID spellings pass |
| Importing IFC into a scene with a contract could discard its known height reference. | The importer retains the checked IFC height reference separately after a successful load. Later GIS and IFC preflights compare that fact. | An UNKNOWN project opens LHN95 IFC, then rejects a later LN02 GIS observation without mutation |
| Later GIS imports overlooked the datum of retained terrain layers. | Terrain objects retain their source datum independently of the survey datum. Publication checks retained layers, incoming sources and the imported IFC reference. | Successive real terrain bundles remain checked after a terrain-free observation replaces the active manifest |
| Missing optional imagery still triggered the image grid limit. | Raster preparation discovers files before sizing or allocating their grid. Absent raster inputs return empty arrays. | A 1,000 by 400 m scan area prepares with unselected or empty imagery, while selecting imagery still enforces its limit |
| A crop deadline could leave orphaned partial buffers after the worker stopped. | Crop submission uses private staging and joins the worker before removing failed output. Successful publication reserves a new destination and preserves existing directories. | A real subprocess writes partial buffers and misses the deadline; actual Rust crops also publish correctly and retain pre-existing output and source data |
| Opening IFC after GIS retained an incomplete project contract and missed conflicting topology. | The importer fills compatible missing model fields and merges reference facts while preserving established project fields. It rejects contradictory nonempty topology before loading. | Real GIS-first import fills rail pairs, terrain selectors and sampling; conflicting topology leaves the scene untouched |
| A sidecar-free IFC lost its known project UUID before the next GIS Build. | Successful IFC import retains the checked UUID separately. GIS Build and prepared Open validate and reuse that identity independently of a full contract. | A version-two bare IFC retains its UUID through GIS Build; prepared Open accepts the same project and rejects conflicting or malformed identity |
| A sidecar-free IFC also lost its known horizontal CRS. | Successful import retains the checked CRS name separately. Later IFC and GIS preflights compare it before loading geometry. | A bare IFC with another known CRS rejects a later LV95 GIS observation; matching and unknown references stay compatible |

The identity and IFC findings were reproduced before repair. The alignment identity controls failed on duplicate GUIDs and missed collision rejection. The saved-alignment controls failed because invalid exports had no validation errors. The installed GIS Build control failed because a later observation changed the UUID. A subsequent review found the shortcut gap, and its curved-path control failed before the sampling repair. The physical controls also reproduce missed shifts after valid dimension edits and an interior rail bend. Further controls restore old geometry after valid rail, mast and sleeper dimension edits while retaining matching metadata and centres. The GIS import control confirms that the Open operator previously accepted another project UUID at the same origin. Additional controls showed that a retained rail foot could hide a lateral shift of its head or crown. Both running-head positions now receive independent fitted-path checks. The importer also rejects unspecified project length units rather than treating the library's default scale as a metre declaration. Valid conversion-based metre units and bare IFC files with a sufficient map frame remain supported. The repaired railway owner checks pass.

The alignment geometry comparison reads the actual IFC polyline and composes its local placement. It converts coordinates back to the fitted UV frame, checks both ends of the common rail interval, verifies recorded sampling stations, and evaluates the paired rail functions at every path vertex. This calculation is independent of the exporter. Unknown official chainage, height datum and assumed horizontal reference remain review items.

## Verification and delivery

The first repaired portable gate passed formatting, strict Clippy, strict rustdoc, Cargo tests, ten worker tests and 40 Python tests. The gate after the project UUID repair passed 51 Python tests. The final gate adds optional-raster and crop-lifecycle controls for 55 Python tests. Raster fixture warnings and the existing Rasterio deprecation warning remain visible. The railway owner file contains twenty-two tests, including the new identity and path controls. The real GIS scenarios run without a production test hook.

The repaired releases are Swiss GIS Local 0.1.3 and Railway BIM 0.1.2. Scan Viewer is 0.4.4 after the cache repair. Builds validate their Blender manifests and bundle their runtime code. The pinned Swiss dependency wheels are unchanged and rechecked against their lockfile. The large-node controls fail on the old minimum and on a candidate that changes only the minimum. The repaired controller delivers the whole node in both cases. The legacy scene fixture uses the old RNA declaration only while the extension is disabled and verifies its saved 64 MiB value after reload.

Installation finished before the full serial Blender suite. That suite passed four pilot acceptance cases in 109.548 seconds and exercised all three installed extensions. It rebuilt a temporary pilot IFC and checked persisted edits and invalid geometry. After the final horizontal CRS repair, Swiss GIS Local and Railway BIM were rebuilt, installed and checked again, serially. Both passed. The Swiss integration owner imports the original pilot and checks preservation of its full contract through later observations. Railway BIM checks real Bonsai imports, saved dimension edits, refreshed meshes and job completion across file loads and extension re-enabling.

Eight installed runtime files match the reviewed source. A separate check validates the original version-one IFC with its embedded evidence. All 33 checks pass with zero errors; the known survey review items remain visible. The first IFC, workspace, preview, sidecars and source crop still match all six recorded hashes and sizes after the final installed checks.

Local receipts and review results are in `artifacts/automated-review/`. Model and edit experiments use generated fixtures or temporary copies. The audit finished before a commit or push was requested.

## Review command

Each pass uses the structured helper against the staged and unstaged local changes:

```sh
/Users/ali/.codex/skills/autoreview/scripts/autoreview --mode local \
  --prompt-file artifacts/automated-review/review-context.md \
  --output artifacts/automated-review/review-11.md \
  --json-output artifacts/automated-review/review-11.json
```

Review 11 exited 0 with an empty findings list. All twenty findings from the earlier valid reports were accepted after checking their real callers and reproducing the failures. None were rejected. The review inspected the frozen local changes with read-only tools; the test runs supply the execution evidence. No additional review was run after the clean result.

The portable gate passed strict Rust checks, one Cargo test, ten worker tests and 55 Python tests in 131.34 seconds. Installed checks passed for Scan Viewer 0.4.4, Swiss GIS Local 0.1.3 and Railway BIM 0.1.2. The final affected checks are recorded in `swiss-installed-final.log` and `railway-installed-final.log`. The review JSON, verification receipt and protected hashes are in `artifacts/automated-review/`. At the audit's close, changes were local and uncommitted. The later delivery request authorizes this verified snapshot on `main`. Restart Blender to load the updated extensions in an existing session.
