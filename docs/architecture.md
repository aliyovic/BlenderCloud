# Architecture and stabilization

The existing workspace is the integration repository. Keeping the three plugin folders at their original paths avoids breaking saved Blender source references and existing commands. The repository owns releases and shared checks; each extension keeps its own manifest and installation ID.

Rust point processing is one library with a thin executable. `point` defines point fields and intensity conversion, `formats` handles source formats, `cache` and `octree` persist point data, `operations` owns planning, node reads, crops and picking, and `protocol` defines typed requests and payload limits. Create another crate only when a separate production caller needs that boundary.

Python domain packages live under each plugin's `src` directory. Blender adapters import them through compatibility modules. Release archives include the packages, so an installed extension needs no checkout or editable install. IfcOpenShell remains the IFC authoring library.

The node protocol transfers positions, RGB, intensity and classification in separate columns. A frame holds at most 64 MiB. Protocol version 2 adds offsets and continuation fields; the Python client assembles each column across pages and retains every point. It terminates workers after malformed framing or a transport deadline, allowing the runtime to start a fresh worker.

The Blender runtime shares a 512 MiB reservation pool across worker requests. Reservations cover the requested Rust decode budget and twice the expected node payload for assembly. Runtime requests use a 64 MiB Rust working budget unless the request supplies another value. One assembled node may contain up to 128 MiB; larger nodes return an explicit repartition error. Completed node buffers retain their reservation until Blender consumes them. These limits govern working buffers, separate from retained CPU/GPU caches and process overhead. They do not silently reduce source data.

Viewport planning and interactive tools use separate executors. Crop and pick requests admit at most two jobs, including the running job, and reject further submissions with a busy message. Their deadline includes queue time. Disabling Scan Viewer stops streaming, import processes, queued work and import timers before removing Blender properties. Turning off detail streaming keeps overview imports running.

Native node readers use bounded reads instead of memory mapping mutable cache files. Cache generations must match current source evidence before reuse. Cropping writes selected data to temporary output and publishes successful results; point-limit failures discard temporary output. Picking finds a hit and estimates its local neighborhood without retaining the entire ray cylinder. Source caches have an eviction limit.

Native cache operations validate node paths inside the cache's real `nodes` directory before reading, compacting or indexing files. Crop transforms must be finite affine matrices, and each retained point has exactly one source ID. Overflowing local coordinates reject the crop before publication.

The railway project contract records explicit tracks, project identity, observation identity, coordinate references and evidence statuses. The Swiss crop importer supplies the historical two-track assumptions. New asset GUIDs depend on project and asset identity rather than the source scan hash. Editing the first delivery retains its existing GUIDs and legacy sidecars. A verified reference must include evidence; the importer continues to mark the original crop's horizontal CRS as assumed and its vertical datum as unknown.

Portable tests own observable contracts at process, file and package boundaries. They use small authored fixtures rather than external datasets. The full source comparison and Blender/Bonsai suites remain separate integration checks. The first model, workspace, sidecars and original crop have recorded hashes in local stabilization artifacts.

The Blender suite rebuilds the full pilot into a temporary directory and checks export, persisted edits, invalid-input rejection and deliberately misplaced geometry with Bonsai's IfcOpenShell. It then checks the three installed extensions. Installation finishes before these serial checks because Blender manages shared extension wheels in the user profile.

One root Git repository replaces the three nested checkouts. The original Scan Viewer commit remains an ancestor and named history reference. `.repository-history/` retains all former Git metadata and the migration inventory. Local sources and generated assets were not moved. CI configuration is checked in with source, while `.gitignore` excludes datasets, caches, generated deliveries, releases and audit output.

The corridor planner writes overlapping extraction sections from supplied bounds or a polyline. Each section has its own local origin, coordinate limit and project-derived identity. Reconciliation compares project-wide asset observations and writes explicit conflicts for divergent revisions or parameters. Planning uses supplied path distances and does not establish official chainage or perform raw scan fitting.
