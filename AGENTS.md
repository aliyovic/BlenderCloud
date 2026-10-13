# Repository work

Use the unslop skill for natural-language writing and run its self-audit before final prose. Use the test-audit authoring gate before adding or changing tests.

Use rust-best-practices when reviewing or changing Rust. Apply rust-async-patterns to concurrency, admission limits, deadlines, cancellation and shutdown. The current Rust worker processes files synchronously in subprocesses; adding an async runtime requires a concrete workload need.

Keep Scan Viewer, Swiss GIS Local and Railway BIM as separate Blender extensions. Keep point processing in Rust and IFC/Bonsai authoring in Python. Package domain code with extensions; installed plugins must not require the source checkout.

Run `python scripts/check.py` for portable checks. Use the locked Python dependencies and pinned Rust toolchain. Run background Blender integration checks after changes to installed extension loading, operators, source-coordinate delivery, or IFC edits.

Finish extension installation before running Blender checks. Run Blender checks serially against one user profile because extension startup, file loads and registration can update its shared Python wheels.

Preserve the first railway delivery and the source crop. Write validation and edit experiments to temporary folders or ignored artifacts. Keep scan data, caches, generated IFC/Blender files, releases and repository metadata backups out of commits.

Do not delegate to remote Codex on Spark unless Ali explicitly requests that delegation. Use SSH directly for work on a named remote machine.
