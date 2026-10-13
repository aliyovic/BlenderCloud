# Local project data

`railway_local/` is the original scan-only bundle. `railway_swisstopo/` adds independent aerial and terrain context for the same origin and selected area.

`swisstopo_area/imagery/` contains two 0.1-metre SWISSIMAGE 2025 crops. `swisstopo_area/terrain/` contains two 2-metre swissALTI3D 2026 crops in LN02. They occupy 5,462,984 bytes altogether. The selected area is E 2,689,076.323 to 2,689,244.336 m and N 1,254,895.946 to 1,255,047.115 m. Crops include the source pixel edges enclosing that rectangle.

The one-time download used direct GeoTIFF byte ranges. The Blender extension reads only local files. `swisstopo_area/sources.json` records the source URLs, source versions, hashes, and retained bounds.

Source: Federal Office of Topography swisstopo. [Terms of use](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices).

The original LAZ remains in `../../infrastructure/`. Place local EPSG:2056 terrain and aerial GeoTIFFs in separate folders, then choose those folders in the Swiss GIS panel or preparation script. Only intersecting raster windows enter the bundle.

Generated bundles are excluded from source control.
