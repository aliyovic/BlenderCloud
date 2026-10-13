# Skill alignment audit

The 2026-10-12 review applied the three requested skills to the stabilized repository. It found four defects and repaired them. The repository keeps its three extension IDs, one Rust processing library, separate Python domain packages and the first IFC/Bonsai delivery.

## Installed sources

The installations use pinned source commits. `test-audit` replaces the older local copy; its backup is in `/Users/ali/.codex/skill-backups/test-audit-20261012-6a6fd124`. No upstream skill instructions were changed.

| Skill | Source snapshot | Installed folder |
|---|---|---|
| test-audit | [OpenClaw, 5cf2615](https://github.com/openclaw/openclaw/tree/5cf2615e65ec2e8344c836a5572c523783981e60/.agents/skills/test-audit) | `/Users/ali/.codex/skills/test-audit` |
| rust-best-practices | [Apollo GraphQL, 5f02fcf](https://github.com/apollographql/skills/tree/5f02fcfcebec678e2e55777b639e32346622a963/skills/rust-best-practices) | `/Users/ali/.codex/skills/rust-best-practices` |
| rust-async-patterns | [wshobson/agents, 46891e7](https://github.com/wshobson/agents/tree/46891e7e60da0e52baf1050b7b6391b64e84c6d9/plugins/systems-programming/skills/rust-async-patterns) | `/Users/ali/.codex/skills/rust-async-patterns` |

The Rust skills came from the entries on [skills.sh for Apollo](https://www.skills.sh/apollographql/skills/rust-best-practices) and [skills.sh for async Rust patterns](https://www.skills.sh/wshobson/agents/rust-async-patterns). Full commits and installed `SKILL.md` hashes are recorded locally in `artifacts/skill-alignment/installations.json`. Repository instructions now require these reviews for applicable changes.

## Repairs and their proof owners

| Defect | Repair | Primary regression boundary |
|---|---|---|
| A manifest node path could make compaction copy and delete a file outside the cache. | Validate every node's lexical path and resolved location before compaction or indexing starts. Node reads use the same validation. | Real cache CLI and worker requests in `scan_viewer/tests/test_worker_protocol.py` |
| Projective or overflowing crop transforms could retain fewer points than source IDs. | Require finite affine transforms, reject overflowing local coordinates, and write an ID only after retaining its point. | Real crop RPC in `scan_viewer/tests/test_worker_protocol.py` |
| Disabling Scan Viewer could retain import processes, jobs and timers after removing properties. | Stop all owned work and remove partial caches before unregistering. Detail toggling keeps overview imports running. | Installed Blender extension lifecycle in `scan_viewer/tests/blender_portable.py` |
| Crop and pick requests had an unbounded queue on the viewport planner. | Admit at most two tool jobs, use a separate tool executor, and count queue time toward the request deadline. | Process transport and cancellation in `scan_viewer/tests/test_transport.py` |

Pre-fix worker controls failed for nine intended cases. The repaired worker passes rejection controls, valid compaction/indexing and a rotated crop spanning two sources. Independent expected coordinates and IDs protect crop delivery.

Pre-fix concurrency controls failed because a third tool was accepted and a blocked crop prevented viewport planning. The repaired code rejects excess work, restores capacity after cancellation and completion, and plans while the tool remains blocked. The pre-fix Blender lifecycle control retained import work after disable. Installed Scan Viewer 0.4.3 now passes disable, re-enable and the following import.

No production export, dependency injection hook or flag was added for tests. The new IFC integration wrapper runs the existing acceptance suite with Blender's IfcOpenShell and the selected delivery's reconstruction. It writes temporary model copies.

## Rust decisions

The Rust review covered all nine best-practice chapters and the async skill's concurrency guidance. Strict Clippy passes, including focused performance, clone, collection and enum checks. Rustdoc treats warnings as errors; the library denies missing public documentation and forbids unsafe code.

The current library exposes the application's command entry point. Processing modules remain private, so `anyhow` fits this application boundary. A public reusable geometry API would need its own typed error contract. Checked fixed-width conversions and initialized-state `unwrap`/`expect` calls satisfy the error chapter's impossible-failure exception; replacing them mechanically would add noise.

Rust reads local files synchronously. Python controls subprocess concurrency, deadlines, cancellation and Blender callbacks. This workload does not justify Tokio, async traits, additional crates or a type-state rewrite. Singular affine crop transforms remain valid because cropping only maps points forward.

## Test audit and cost

The authoring gate identified each observable contract, its credible regression and the gap in existing proof before additions. No tests were removed. Package, protocol, coordinate, SDK, persisted identity and lifecycle checks protect distinct failure boundaries. Historical GUI and performance scripts remain available for their original checks.

The focused Rust repairs add 72 production lines and remove 28; worker tests and support add 118 and remove 6. The concurrency batch adds 57 production lines and removes 17, including the version change, and adds 92 test/support lines. The full worker suite takes about 0.4 seconds. The complete transport suite took 22.65 seconds; it includes real deadline and memory-release checks. These costs do not justify weakening their contracts.

The portable run passes Rust formatting, strict Clippy, strict rustdoc, Cargo tests, ten worker tests and 33 Python tests. Its raster fixture warnings and Rasterio deprecation warning remain visible. The Blender lane checks the full pilot export, a persisted mast edit, invalid-input file preservation and deliberately misplaced rail geometry, then checks the installed extensions. Source checks alone cannot establish Bonsai operator registration or refreshed authoring meshes.

The stable full-pilot run passed all four acceptance cases in 104.36 seconds with Blender 5.1.1 and Bonsai's IfcOpenShell 0.8.6 alpha. Installed Railway BIM also passed its saved-edit, live-mesh refresh, invalid-input and job lifecycle checks in a separate serial run.

Final serial checks also passed for installed Scan Viewer 0.4.3 and Swiss GIS Local 0.1.2. Scan Viewer delivered all 2,000 authored source points and passed active import disable/re-enable. Swiss GIS passed mesh coordinate and terrain UV checks, packed-image reload, reference export and registration. All six protected delivery and crop hashes remain unchanged.

## Blender environment and CI

An early pilot validation reported an EXPRESS engine exception without an internal stack. Subsequent parallel Blender checks exposed shared dependency mutation: extension refresh removes and reinstalls wheel directories without a process lock. One parallel run reported a missing `ifccityjson` wheel record, and the IfcOpenShell directory was temporarily absent. Isolated schema and EXPRESS validation passed on both the first delivery and a newly rebuilt model. The first exception's precise cause remains unproven; the final pilot acceptance run uses a stable, serial environment.

Build and installation must finish before Blender checks. The shared runner executes checks serially. The manual Blender workflow now queues concurrent dispatches without cancelling an install, builds and installs the current three archives, and runs the full pilot acceptance suite before extension checks. It uses a locked Python environment and pinned Rust toolchain. The portable GitHub workflow runs the same portable command. Neither workflow has been dispatched remotely during this local audit.

The first IFC, Blender workspace, preview, both evidence sidecars and source crop have recorded SHA-256 hashes. Validation and edit experiments use copies. The model's assumed horizontal CRS, unknown height datum and unknown official chainage remain review items, because the skills do not supply missing survey evidence.

Local receipts, negative controls and final verification are in `artifacts/skill-alignment/`. These files and generated deliveries remain outside Git. The changes remain local until a separate commit or push request.

The test-audit proof policy also calls for autoreview. The [installed autoreview skill](/Users/ali/.codex/skills/autoreview/SKILL.md) requires, "Run the bundled structured review helper as a closeout check." At the first closeout, the command below exited 1 before producing a review because CLI 0.155.1 rejected its configured `gpt-6.1-sol` model with this ChatGPT account. No model or engine override was applied. The [2026-10-13 follow-up](automated-review.md) records the CLI upgrade, confirmed findings and repairs.

```sh
/Users/ali/.codex/skills/autoreview/scripts/autoreview --mode local \
  --output artifacts/skill-alignment/autoreview.md \
  --json-output artifacts/skill-alignment/autoreview.json
```
