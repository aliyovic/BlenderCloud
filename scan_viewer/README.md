# Scan Viewer

Scan Viewer 0.4.4 is a Blender add-on with a Rust point-cloud reader. It draws large scans beside Blender and Bonsai geometry, streams detail as you navigate, and creates modeling crops that survive in the saved .blend file.

[Download the macOS Apple Silicon extension](dist/scan_viewer-0.4.4-macos-arm64.zip). It was built for Blender 5.1.1 and tested on an Apple M5. If your development installation points to the source directory, restart Blender to load changed Python code.

## Use it

1. In the 3D View, press N and open the **Scan** tab. Choose **Import scan folder**.
2. Zoom to stream detail. COPC files stream directly. Other large scans prepare a full-detail cache automatically while the overview remains visible. One automatic build runs at a time. Canceling stops automatic preparation for that scan; **Build full detail cache** starts it again. Later visits reuse the cache.
3. Use **Class (-1 = all)** to show one classification, or choose RGB, intensity, or class colors. The shader changes color without uploading points again. The first COPC class selection builds a small per-node class index in the overview cache.
4. Choose **Add modeling box** and place it around your work area. The box immediately clips the scan and focuses the detail plan. Choose **Load full detail in box** to make a source crop. The crop can cross COPC tile boundaries when the neighboring scans are in the scene. The default limit is one million points.
5. Use **Pick scan point** to place and orient the 3D cursor from a source point and a local fitted plane. For mesh vertex snapping, select a crop and choose **Create modeling snap sample**.

A crop stores its points, colors, intensity, classes, and source IDs as mesh attributes inside the .blend file. The source scan and box are hidden after the crop; unhide them in the Outliner to keep browsing. COPC IDs identify a source tile, node, and point within that node. Native octree IDs identify a cached node and point. The original source files remain necessary for new crops and source picking. Missing overview caches are rebuilt from their source files.

[The building crop example](examples/copc_modeling_crop.blend) contains 8,357 points and a snap mesh. [The all-scans scene](examples/all_desktop_scans.blend) contains the 15 Desktop scans. These example scenes refer to source paths on this Mac.

## Measured here

The 13 SwissSurface3D COPC tiles contain 683,993,847 points. The Desktop LAS and XYZ bring the total to 698,431,174 points. The separate CRAS terrestrial ZIP contains 584,701,977 points.

| Task | Result |
| --- | ---: |
| Building view refinement, 26 COPC nodes through level 5 | 0.64 s |
| Camera motion, median / 95th percentile frame interval | 8.83 / 10.68 ms |
| 14 stress visits through the COPC tiles, median / maximum | 0.81 / 0.90 s |
| Cross-tile crop checked against laspy | 46,537 matching points |
| 8,357-point modeling crop in Blender | 0.22 s |
| Full CRAS native octree, overview / nearby view | 3.16 / 0.98 s |

The CRAS octree occupies 9.8 GiB in the scan viewer cache and streams at the two million point budget. It was built directly from the ZIP without unpacking the 26.7 GiB text scan. A full CRAS COPC conversion was not run because the volume had about 9 GiB free after the octree build. [Performance details](PERFORMANCE.md) include the Metal offscreen measurements and test limits.

## How it works

The COPC reader loads whole shuffled nodes, including points previously lost to a 65,536-point cap. The planner chooses nodes under the GPU memory budget, then distributes the draw budget proportionally across those nodes. This keeps neighboring regions at the same sampling fraction instead of dropping entire regions when the point budget runs out. For LAS point formats 6 through 8, the worker decompresses only the layers needed for drawing. Six persistent decode workers take the next queued node as soon as one finishes. Late results stay in the CPU cache for a return visit.

The custom shader preserves source RGB in both linear and sRGB framebuffer targets, draws round points, applies class and intensity controls, and clips against the modeling box and Blender's viewport clip planes. Points default to one pixel and keep the selected size during streaming. The renderer no longer enlarges sparse samples to fill gaps. New detail fades in over 150 ms. The root overview sample stops drawing once its streamed node is ready. Uploads are limited by a four millisecond scheduling target rather than a fixed node count. GPU buffers use 24 bytes per point; CPU payloads use 18 bytes per point.

The default budget is five million points, with 512 MiB each for the CPU and GPU caches. The sidebar reports submitted draw counts separately from the overview point limit. The following GPU measurements used the earlier two-pixel display settings. In a 1920 × 1080 Metal offscreen loop with two-pixel points, one million points took 9.80 ms median and two million took 20.95 ms. The add-on reduces density during motion when measured draw intervals miss the display cadence. The offscreen result is a separate workload from the Blender viewport.

Caches live in ~/Library/Caches/scan_viewer on macOS, %LOCALAPPDATA%/scan_viewer on Windows, and $XDG_CACHE_HOME/scan_viewer or ~/.cache/scan_viewer on Linux. Streaming serves the largest 3D viewport; other viewports show the overview. Native octree coordinates are stored as 32-bit offsets from a 64-bit origin, so native picking reflects cache precision. COPC picking reads the original source coordinates.

Supported imports: ASCII and binary PLY, LAS, LAZ, E57, XYZ, PTS, ASC, TXT, CSV, and a ZIP containing one text scan. Text input accepts XYZ followed by optional RGB, intensity, and classification. Automatic BIM reconstruction and scan registration are outside this add-on.

## Open scans from Finder

On macOS, install the Finder opener with Blender in `/Applications/Blender.app`:

    python3 scripts/install_macos_launcher.py

The installer creates `/Applications/Open Point Cloud in Blender.app` and makes it the default opener for LAS, LAZ, E57, PLY, XYZ, PTS, and ASC files. Double-click a scan to open a fresh Blender window, import it, and show the Scan sidebar. COPC LAZ files stream detail automatically. Other scans reuse an existing full-detail cache when available; choose **Build full detail cache** to create one.

The opener bundles Scan Viewer and its reader, so it also works without a development installation. Select several files and open them together to load them in one window. TXT, CSV, and scan ZIP files can be dropped onto the opener or selected by opening the app itself. Their general-purpose file associations stay as they are.

Launcher output goes to `~/Library/Logs/Scan Viewer/launcher.log`. The installer saves the previous point-cloud associations in `~/Library/Application Support/Scan Viewer/finder-associations-before.json` on its first run.

## Build and verify

Clone the repository and build the macOS Apple Silicon extension with Rust and Blender installed:

    git clone https://github.com/aliyovic/BlenderCloud.git
    cd BlenderCloud
    sh scripts/build_extension.sh

The script expects Blender at `/Applications/Blender.app/Contents/MacOS/Blender` and writes the extension ZIP to `dist/`.

The integration tests below use source scans and caches from the development Mac. Those datasets and caches are not included in this repository. Some tests use absolute paths under `/Users/ali/Desktop/pointclouds`; CRAS and synthetic fixtures live beside the repository in `CRAS_Lab/` and `Synthetic_Building/`. Update those paths and provide the fixtures before running the tests on another machine. The JSON results and linked screenshots record the runs described in [PERFORMANCE.md](PERFORMANCE.md).

    cargo build --release
    python3 tests/streaming_data.py
    /Applications/Blender.app/Contents/MacOS/Blender examples/all_desktop_scans.blend --python tests/gui_streaming.py
    /Applications/Blender.app/Contents/MacOS/Blender examples/all_desktop_scans.blend --python tests/gui_stream_stress.py
    /Applications/Blender.app/Contents/MacOS/Blender examples/all_desktop_scans.blend --python tests/gui_class_filter.py
    /Applications/Blender.app/Contents/MacOS/Blender --factory-startup --python tests/gui_clip_probe.py
    /Applications/Blender.app/Contents/MacOS/Blender --factory-startup --python tests/gui_cras_full.py
    sh scripts/build_extension.sh
    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tests/package_smoke.py

The source-data check uses Python with numpy, laspy, and lazrs. The package script builds macOS Apple Silicon. For another platform, build Rust there and update the extension manifest before packaging.
