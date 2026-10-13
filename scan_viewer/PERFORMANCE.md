# Scan Viewer performance, 26 September 2026

## Dataset and test machine

Tests ran on an Apple M5 with 32 GB RAM, Blender 5.1.1, and the Metal backend. The GUI tests used the normal Blender profile with Bonsai enabled unless a test specifies factory startup.

The 13 SwissSurface3D COPC tiles contain 683,993,847 points. One LAS file contains 10,437,327 points and one XYZ file contains 4,000,000 rows. The 15 Desktop sources total 698,431,174 points. A separate CRAS terrestrial ZIP contains 584,701,977 points in a 26.7 GiB text member. All COPC tiles carry EPSG:2056 WKT. The LAS has no CRS record; the importer does not infer one.

## Streaming design

The planner culls nodes against the view and active modeling box, ranks them by projected point spacing, and observes both a point budget and available GPU cache capacity. It loads full shuffled COPC nodes. A draw prefix samples a node, and the planner adjusts spacing by the square root of full count divided by drawn count. The old 65,536-point cap is removed. The 13-tile hierarchy has 237.7 million points in nodes that exceeded that cap, so the change makes those points addressable.

Six persistent decode workers pull from a shared priority queue. They keep completed reads in the CPU cache even when the camera has moved. The draw handler schedules uploads until a four millisecond elapsed-time target, then tags another redraw if buffers remain. A single upload can take longer than the target. Root overviews stop drawing once the streamed roots fade in.

For LAS formats 6 through 8, selective LAZ decompression reads coordinates, RGB where present, intensity, and classification. Other formats use the regular LAS reader. CPU node payloads use 18 bytes per point; GPU buffers use 24. The shader changes color and intensity without buffer re-upload, draws round points, supports class and box filters, and uses viewport clip planes. A 150 ms draw-range ramp fades in new detail.

For a class filter, COPC first builds and saves a per-node histogram in its overview cache. On the 79 million point test tile this took 3.35 s with a warm file cache, and the index occupies 48 KiB. Subsequent plans use exact filtered counts and decode only the requested class before shuffling. Native octree manifests store their own class counts.

## COPC measurements

[The GUI run](tests/gui_streaming.json) opened all 15 scans, zoomed to a building, moved the camera, cropped the building, and made a snap mesh.

| Measurement | Result |
| --- | ---: |
| Overview, streamed roots | 13 nodes, 959,046 planned points |
| Building view | 26 nodes through level 5, 1,123,002 planned points |
| Building refinement | 0.636 s |
| Camera motion, median / 95th percentile frame interval | 8.83 / 10.68 ms |
| Draw submission, median | 0.46 ms |
| Modeling crop | 8,357 points in 0.218 s |
| Snap mesh | 8,357 vertices |

The camera interval includes the display cadence; it is not a GPU timing. The crop and its source IDs are embedded in the saved [example .blend](examples/copc_modeling_crop.blend). Six parallel COPC readers reduced the independent 8,361-point crop from 0.29 s to 0.11 s on a warm cache. [Overview](tests/gui_stream_overview.png), [building](tests/gui_stream_building.png), and [crop](tests/gui_modeling_crop.png) captures come from the GUI run.

[The stress run](tests/gui_stream_stress.json) made 30 rapid camera changes, then 14 visits across the 13 tiles, with 64 MiB limits for each point cache and a six million requested point budget. Settled visit time was 0.807 s median and 0.903 s maximum. It read 350 nodes and recorded 324 CPU and 322 GPU evictions. Maximum CPU payload was 66,466,872 bytes; maximum GPU payload including overviews was 67,108,656 bytes, under the 67,108,864-byte limit. These limits exclude Blender, decoder working memory, and driver allocations. Worker processes stopped after the run. The changing color modes did not re-upload buffers solely for color.

[The source-data check](tests/streaming_data.json) compares selective node decoding, axis-aligned and rotated crops, and cross-tile crops with laspy. The cross-tile query matched 46,537 source points from two tiles. A filtered COPC root matched 4,931 building-class points from laspy. The 10-point crop limit rejected and removed an incomplete output. Picking found a source point within 0.01 mm of a test ray and fitted a plane from seven neighbors. The GUI crop differs by four points from the independent Rust crop because Blender's box transform uses float precision.

[The class-filter GUI check](tests/gui_class_filter.json) inspected 26 decoded buffers and found only the selected class. [The viewport clipping probe](tests/gui_clip_probe.py) drew two points on opposite sides of a clip plane and observed only the retained point.

## Metal draw measurement

[The offscreen benchmark](tests/gpu_benchmark.json) rendered two-pixel round points at 1920 × 1080 on Metal.

| Points | Median elapsed time |
| ---: | ---: |
| 250,000 | 2.47 ms |
| 500,000 | 4.75 ms |
| 750,000 | 7.07 ms |
| 1,000,000 | 9.80 ms |
| 2,000,000 | 20.95 ms |

The loop measures GPU draw and synchronization time. It excludes Blender's full viewport frame. The result did not justify increasing the two million default ceiling on this machine. Motion density now responds to measured frame intervals. [The Metal point-size probe](tests/gpu_probe.py) also checked that programmable size and round fragments render correctly.

## Terrestrial scan

The Rust builder indexed the full CRAS ZIP directly, retaining all 584,701,977 points in a 9.8 GiB native octree at ~/Library/Caches/scan_viewer/octree-7384d2c1cdd7f425cd761286. It did not unpack the 26.7 GiB member. Small nodes were compacted into 9,408 files; the manifest includes per-node class counts. Native node reads are memory mapped. The builder took about 180 s on this machine.

[The full CRAS GUI test](tests/gui_cras_full.json) drew about two million points: 3.16 s to settle the first overview from a cold worker state, then 0.98 s for a nearby camera view. The first plan took 1.75 ms, and the nearby plan 1.53 ms. A smaller [native octree GUI check](tests/gui_native_octree.py) also built a cache, streamed it, and embedded a crop. The full CRAS source was not converted to COPC because the volume had about 9 GiB free after the native cache build. The native octree is the measured full-resolution terrestrial path.

## Scope and limits

The native octree stores 32-bit point offsets relative to a 64-bit origin. Its pick coordinates and IDs refer to the cache's precision and node order. COPC crop IDs encode the tile, node, and source point index, and COPC picking reads original source coordinates. Crops remain available in a saved .blend even if the temporary crop cache is cleared. Original scans and octree caches are still needed for new streaming, crops, and picking.

Only the largest 3D viewport requests streamed detail; other viewports draw the overview. Automatic IFC reconstruction, registration, and eye-dome lighting are not part of this release. The GPU benchmark and local timings should be repeated on other hardware before changing the default budget.
