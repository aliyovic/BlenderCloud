"""Save the authorized railway surroundings from direct GeoTIFF file URLs."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import ssl
import urllib.request

import certifi
import rasterio
from rasterio.windows import Window, from_bounds


ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTION = "Federal Office of Topography swisstopo"


def main():
    parser = argparse.ArgumentParser(description="Retain the selected railway area from direct public GeoTIFF files")
    parser.add_argument("--project", type=Path, default=ROOT / "data/railway_local/project.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/swisstopo_area")
    parser.add_argument("--label", default="railway")
    args = parser.parse_args()
    if not args.label.replace("_", "").replace("-", "").isalnum():
        parser.error("Use letters, digits, underscores or hyphens for the crop label")
    project = json.loads(args.project.read_text())
    area = project["area"]
    if len(area) != 4 or not all(math.isfinite(v) for v in area) or area[2] <= area[0] or area[3] <= area[1]:
        parser.error("The project needs valid LV95 area bounds")
    if max(area[2]-area[0], area[3]-area[1]) > 5000:
        parser.error("Keep the selected area within five kilometres per side")
    tiles = [f"{east}-{north}"
             for east in range(math.floor(area[0]/1000), math.ceil(area[2]/1000))
             for north in range(math.floor(area[1]/1000), math.ceil(area[3]/1000))]
    output = args.output.resolve()
    destinations = [output/kind/f"{product}_{year}_{tile}_{suffix}_{args.label}_crop.tif"
                    for product, year, suffix, kind in (
                        ("swissimage-dop10", 2025, "0.1_2056", "imagery"),
                        ("swissalti3d", 2026, "2_2056_5728", "terrain")) for tile in tiles]
    if any(path.exists() for path in destinations) or (output/"sources.json").exists():
        parser.error("Choose a new output folder. Existing crops and receipts are preserved")
    output.mkdir(parents=True, exist_ok=True)
    sources = []
    tls = ssl.create_default_context(cafile=certifi.where())
    for product, year, suffix, kind in (
        ("swissimage-dop10", 2025, "0.1_2056", "imagery"),
        ("swissalti3d", 2026, "2_2056_5728", "terrain"),
    ):
        folder = output / kind
        folder.mkdir(exist_ok=True)
        for tile in tiles:
            stem = f"{product}_{year}_{tile}"
            filename = f"{stem}_{suffix}.tif"
            url = f"https://data.geo.admin.ch/ch.swisstopo.{product}/{stem}/{filename}"
            destination = folder / (Path(filename).stem + f"_{args.label}_crop.tif")
            if destination.exists():
                raise SystemExit(f"Preserving existing crop: {destination}. No new transfer started.")
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), context=tls, timeout=30) as response:
                headers = {key.lower():value for key,value in response.headers.items()}
            print(f"Read selected window from {filename}", flush=True)
            with rasterio.Env(GDAL_HTTP_CAINFO=certifi.where(), GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                              CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_TIMEOUT="30"):
                with rasterio.open(url) as src:
                    if src.crs.to_epsg() != 2056:
                        raise RuntimeError(f"Unexpected source CRS: {src.crs}")
                    bounds = src.bounds
                    clipped = (max(bounds.left, area[0]), max(bounds.bottom, area[1]),
                               min(bounds.right, area[2]), min(bounds.top, area[3]))
                    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
                        raise RuntimeError("The source tile does not cover the selected area")
                    request = from_bounds(*clipped, src.transform)
                    left = max(0, math.floor(request.col_off))
                    top = max(0, math.floor(request.row_off))
                    right = min(src.width, math.ceil(request.col_off + request.width))
                    bottom = min(src.height, math.ceil(request.row_off + request.height))
                    window = Window(left, top, right-left, bottom-top)
                    data = src.read(window=window)
                    profile = src.profile.copy()
                    profile.update(driver="GTiff", width=data.shape[2], height=data.shape[1],
                                   transform=src.window_transform(window), compress="deflate",
                                   predictor=3 if data.dtype.kind == "f" else 2,
                                   tiled=True, blockxsize=256, blockysize=256)
                    if kind == "imagery":
                        profile["photometric"] = "RGB"
                    source_info = {"url":url, "tile":tile, "product":product, "asset_year":year,
                                   "source_file_bytes":int(headers.get("content-length", 0)),
                                   "source_etag":headers.get("etag"), "source_last_modified":headers.get("last-modified"),
                                   "source_sha256":headers.get("x-amz-meta-sha256"),
                                   "source_bounds":list(bounds), "source_resolution_m":list(src.res),
                                   "window":[left,top,right-left,bottom-top], "local_file":str(destination),
                                   "source_reference":ATTRIBUTION}
                    temporary = destination.with_suffix(".partial.tif")
                    try:
                        with rasterio.open(temporary, "w", **profile) as dst:
                            dst.write(data)
                            dst.update_tags(**src.tags(), SOURCE_URL=url, SOURCE_REFERENCE=ATTRIBUTION,
                                            SOURCE_ASSET_YEAR=str(year))
                            if kind == "terrain":
                                dst.update_tags(VERTICAL_DATUM="LN02")
                        temporary.rename(destination)
                    finally:
                        if temporary.exists():
                            temporary.unlink()
            source_info["local_bytes"] = destination.stat().st_size
            source_info["local_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
            with rasterio.open(destination) as cropped:
                source_info["local_bounds"] = list(cropped.bounds)
                source_info["local_crs"] = str(cropped.crs)
                source_info["local_shape"] = list(cropped.shape)
            sources.append(source_info)
            print(f"Saved {destination.name}: {source_info['local_bytes']:,} bytes", flush=True)
    receipt = {"date":"2026-10-12", "area_lv95":area, "origin":project["origin"],
               "download_method":"Selected byte ranges from direct public GeoTIFF URLs, no geospatial API",
               "source_reference":ATTRIBUTION,
               "terms":"https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices",
               "local_bytes_total":sum(item["local_bytes"] for item in sources), "sources":sources}
    (output / "sources.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Retained {receipt['local_bytes_total']:,} local bytes for this area", flush=True)


if __name__ == "__main__":
    main()
