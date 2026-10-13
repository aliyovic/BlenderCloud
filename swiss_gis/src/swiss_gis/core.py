"""Read local LV95 datasets and prepare bounded geometry without Blender."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import math
import shutil
import tempfile
import uuid

import numpy as np


class DataError(ValueError):
    pass


@dataclass
class Project:
    scan: str
    terrain_folder: str = ""
    imagery_folder: str = ""
    margin: float = 50.0
    max_points: int = 750_000
    terrain_step: float = 2.0
    ground_step: float = 0.5
    vertical_datum: str = "UNKNOWN"
    assume_lv95: bool = True
    center: tuple | None = None
    origin: tuple | None = None
    project_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def local_file(value):
    if "://" in str(value):
        raise DataError("Choose a local file. Remote URLs are not supported.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise DataError(f"File does not exist: {path}")
    return path


def crs_parts(value):
    from pyproj import CRS
    crs = CRS.from_user_input(value)
    parts = crs.sub_crs_list if crs.is_compound else [crs]
    horizontal = next((part.to_epsg() for part in parts if part.is_projected), None)
    vertical = next((part.name for part in parts if part.is_vertical), None)
    return horizontal, vertical


def vertical_name(value):
    text = (value or "UNKNOWN").upper()
    if "LN02" in text:
        return "LN02"
    if "LHN95" in text:
        return "LHN95"
    return "UNKNOWN" if text == "UNKNOWN" else text


def inspect_scan(path, assume_lv95=True):
    import laspy
    path = local_file(path)
    if path.suffix.lower() not in {".las", ".laz"}:
        raise DataError("Choose a LAS or LAZ point cloud.")
    with laspy.open(path) as source:
        header = source.header
        if not header.point_count:
            raise DataError("The point cloud is empty.")
        low, high = header.mins.copy(), header.maxs.copy()
        if not np.isfinite([low, high]).all() or (high < low).any():
            raise DataError("The scan has invalid coordinate bounds.")
        crs = header.parse_crs()
        horizontal, vertical = crs_parts(crs) if crs else (None, None)
        if horizontal is not None and horizontal != 2056:
            raise DataError(f"Scan CRS is {crs}. Convert it to EPSG:2056 first.")
        if crs is not None and horizontal is None:
            raise DataError("The scan CRS does not define projected LV95 coordinates.")
        if crs is None and not assume_lv95:
            raise DataError("The scan has no CRS. Confirm the LV95 assumption to import it.")
        if not (2_400_000 <= low[0] <= high[0] <= 2_900_000 and
                1_000_000 <= low[1] <= high[1] <= 1_400_000):
            raise DataError("The scan bounds are outside the supported Swiss LV95 range.")
        return {
            "path": str(path), "point_count": int(header.point_count),
            "bounds": [low.tolist(), high.tolist()], "epsg": 2056,
            "crs_assumed": crs is None,
            "vertical_datum": vertical_name(vertical),
            "scale": header.scales.tolist(),
        }


def project_area(project, header):
    for name in ("margin", "terrain_step", "ground_step"):
        value = float(getattr(project, name))
        if not math.isfinite(value) or value <= 0:
            raise DataError(f"{name} must be a positive finite number.")
    if not 1 <= project.max_points <= 5_000_000:
        raise DataError("Point budget must be between 1 and 5,000,000.")
    low, high = np.array(header["bounds"], dtype=np.float64)
    center = (low + high) / 2
    if project.center is None:
        area = [low[0] - project.margin, low[1] - project.margin,
                high[0] + project.margin, high[1] + project.margin]
    else:
        if len(project.center) != 2 or not np.isfinite(project.center).all():
            raise DataError("Enter finite LV95 east and north coordinates.")
        area = [project.center[0] - project.margin, project.center[1] - project.margin,
                project.center[0] + project.margin, project.center[1] + project.margin]
    if max(area[2] - area[0], area[3] - area[1]) > 5_000:
        raise DataError("Keep the selected area within 5 km per side.")
    origin = np.round(center) if project.origin is None else np.array(project.origin, dtype=np.float64)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise DataError("The project origin needs three finite coordinates.")
    if max(abs(np.array(area) - origin[[0, 1, 0, 1]])) > 10_000:
        raise DataError("The area is over 10 km from the scene origin. Use a separate scene.")
    return area, origin


def grid_shape(area, step, limit=4_000_000):
    width = max(1, math.ceil((area[2] - area[0]) / step))
    height = max(1, math.ceil((area[3] - area[1]) / step))
    if width * height > limit:
        raise DataError("The grid exceeds four million cells. Increase the cell size.")
    return height, width


def grid_mesh(z, valid, area, step, origin):
    """Use cell centres. Never bridge a cell with missing elevation."""
    rows, cols = z.shape
    yy, xx = np.indices(z.shape)
    xyz = np.column_stack((area[0] + (xx.ravel() + .5) * step - origin[0],
                           area[3] - (yy.ravel() + .5) * step - origin[1],
                           z.ravel() - origin[2]))
    good = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    rr, cc = np.nonzero(good)
    a = rr * cols + cc
    faces = np.column_stack((a, a + cols, a + cols + 1, a + 1))
    used = np.unique(faces)
    remap = np.full(rows * cols, -1, dtype=np.int32)
    remap[used] = np.arange(len(used))
    return xyz[used].astype(np.float32), remap[faces], used


def read_scan(project, header, area, origin, progress=lambda message: None, cancel=lambda: False):
    import laspy
    height, width = grid_shape(area, project.ground_step)
    ground = np.full(height * width, np.inf)
    plan_z = np.full(height * width, -np.inf)
    plan_rgb = np.zeros((height * width, 3), dtype=np.float32)
    samples, colors, classes, indices = [], [], [], []
    stride = max(1, math.ceil(header["point_count"] / project.max_points))
    offset = 0
    ground_count = 0
    max_error = 0.0
    with laspy.open(header["path"]) as source:
        for chunk in source.chunk_iterator(500_000):
            if cancel():
                raise DataError("Preparation canceled.")
            xyz = np.column_stack((chunk.x, chunk.y, chunk.z))
            finite = np.isfinite(xyz).all(axis=1)
            inside = finite & (xyz[:, 0] >= area[0]) & (xyz[:, 0] < area[2]) & (xyz[:, 1] > area[1]) & (xyz[:, 1] <= area[3])
            ids = np.arange(offset, offset + len(chunk), dtype=np.int64)
            classification = np.asarray(chunk.classification, dtype=np.uint8)
            if "red" in chunk.point_format.dimension_names:
                rgb = np.column_stack((chunk.red, chunk.green, chunk.blue)).astype(np.float32) / 65535
            else:
                intensity = np.asarray(chunk.intensity, dtype=np.float32) / 65535
                rgb = np.repeat(intensity[:, None], 3, axis=1)
            selected = inside & (ids % stride == 0)
            local64 = xyz[selected] - origin
            local32 = local64.astype(np.float32)
            if len(local64):
                max_error = max(max_error, float(np.max(np.abs(local32.astype(np.float64) - local64))))
                samples.append(local32)
                colors.append(rgb[selected])
                classes.append(classification[selected])
                indices.append(ids[selected])
            kept = np.flatnonzero(inside & (classification != 7) & (classification != 18))
            if len(kept):
                col = np.floor((xyz[kept, 0] - area[0]) / project.ground_step).astype(int)
                row = np.floor((area[3] - xyz[kept, 1]) / project.ground_step).astype(int)
                cell = row * width + col
                order = np.argsort(xyz[kept, 2], kind="stable")
                ordered_cells = cell[order]
                # Keep one topmost point per cell before comparing it with earlier chunks.
                _, reversed_indices = np.unique(ordered_cells[::-1], return_index=True)
                winners = order[len(order) - 1 - reversed_indices]
                target = cell[winners]
                top = xyz[kept[winners], 2]
                update = top > plan_z[target]
                plan_z[target[update]] = top[update]
                plan_rgb[target[update]] = rgb[kept[winners[update]]]
                is_ground = classification[kept] == 2
                ground_count += int(is_ground.sum())
                np.minimum.at(ground, cell[is_ground], xyz[kept[is_ground], 2])
            offset += len(chunk)
            progress(f"Read {offset:,} of {header['point_count']:,} scan points")
    if not samples:
        raise DataError("The selected area contains no sampled scan points. Move the area or raise the point budget.")
    points = np.concatenate(samples)
    rgb = np.concatenate(colors)
    classification = np.concatenate(classes)
    source_ids = np.concatenate(indices)
    valid = np.isfinite(ground).reshape(height, width)
    z = ground.reshape(height, width)
    vertices, faces, used = grid_mesh(z, valid, area, project.ground_step, origin)
    return {
        "points": points, "rgb": rgb, "classification": classification,
        "source_ids": source_ids, "ground_vertices": vertices, "ground_faces": faces,
        "ground_rgb": plan_rgb[used], "ground_point_count": ground_count,
        "plan_rgb": plan_rgb.reshape(height, width, 3),
        "plan_valid": np.isfinite(plan_z).reshape(height, width),
        "roundtrip_error_m": max_error, "stride": stride,
    }


def raster_files(folder):
    if not folder:
        return []
    if "://" in folder:
        raise DataError("Raster folders must be local.")
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise DataError(f"Raster folder does not exist: {path}")
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".tif", ".tiff"})


def raster_mosaic(folder, kind, area, step, datum, progress, cancel):
    """Read only intersecting GeoTIFF windows, into a bounded LV95 grid."""
    paths = raster_files(folder)
    channels = 1 if kind == "terrain" else 3
    if not paths:
        return np.empty((channels, 0, 0), dtype=np.float32), [], [], []
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import from_bounds, Window
    from rasterio.warp import reproject
    height, width = grid_shape(area, step)
    result = np.full((channels, height, width), np.nan, dtype=np.float32)
    transform = from_origin(area[0], area[3], step, step)
    accepted, skipped = [], []
    warnings = []
    for path in paths:
        if cancel():
            raise DataError("Preparation canceled.")
        progress(f"Inspect {path.name}")
        with rasterio.open(path) as src:
            if not src.crs:
                raise DataError(f"{path.name} has no CRS. Supply a georeferenced EPSG:2056 GeoTIFF.")
            horizontal, vertical = crs_parts(src.crs)
            if horizontal != 2056:
                raise DataError(f"{path.name} is not EPSG:2056. Convert it locally first.")
            b = src.bounds
            if b.right <= area[0] or b.left >= area[2] or b.top <= area[1] or b.bottom >= area[3]:
                skipped.append(str(path))
                continue
            if src.transform.b or src.transform.d or src.transform.a <= 0 or src.transform.e >= 0:
                raise DataError(f"{path.name} must have a north-up raster transform.")
            raster_datum = vertical_name(vertical or src.tags().get("VERTICAL_DATUM"))
            if kind == "terrain" and raster_datum != "UNKNOWN" and datum != "UNKNOWN" and raster_datum != datum:
                raise DataError(f"Vertical datum mismatch: scan {datum}, {path.name} {raster_datum}.")
            if kind == "terrain" and (raster_datum == "UNKNOWN" or datum == "UNKNOWN"):
                warnings.append(f"Height alignment is unverified for {path.name}; one or both vertical datums are unknown.")
            if src.count < channels:
                raise DataError(f"{path.name} needs {channels} raster bands for {kind}.")
            clipped = (max(b.left, area[0]), max(b.bottom, area[1]), min(b.right, area[2]), min(b.top, area[3]))
            requested = from_bounds(*clipped, src.transform)
            col0, row0 = math.floor(requested.col_off), math.floor(requested.row_off)
            col1 = min(src.width, math.ceil(requested.col_off + requested.width))
            row1 = min(src.height, math.ceil(requested.row_off + requested.height))
            window = Window(col0, row0, col1 - col0, row1 - row0)
            if window.width < 1 or window.height < 1:
                continue
            # Reduce the read size before allocating an array for fine imagery.
            out_h = max(1, math.ceil(window.height * abs(src.transform.e) / min(step, abs(src.transform.e))))
            out_w = max(1, math.ceil(window.width * src.transform.a / min(step, src.transform.a)))
            if out_h * out_w > 16_000_000:
                out_h = max(1, math.ceil(window.height * abs(src.transform.e) / step))
                out_w = max(1, math.ceil(window.width * src.transform.a / step))
            values = src.read(list(range(1, channels + 1)), window=window, out_shape=(channels, out_h, out_w), masked=True)
            source_transform = src.window_transform(window) * src.transform.scale(window.width / out_w, window.height / out_h)
            source_values = values.astype(np.float32).filled(np.nan)
            if kind == "imagery":
                divisor = 65535 if np.issubdtype(values.dtype, np.integer) and values.dtype.itemsize > 1 else 255 if np.issubdtype(values.dtype, np.integer) else 1
                source_values /= divisor
                if src.count >= 4:
                    alpha = src.read(4, window=window, out_shape=(out_h, out_w), resampling=Resampling.nearest)
                    source_values[:, alpha == 0] = np.nan
            tile = np.full_like(result, np.nan)
            for band in range(channels):
                reproject(source_values[band], tile[band], src_transform=source_transform,
                          src_crs="EPSG:2056", dst_transform=transform, dst_crs="EPSG:2056",
                          src_nodata=np.nan, dst_nodata=np.nan,
                          resampling=Resampling.bilinear if kind == "terrain" else Resampling.nearest)
            mask = np.isfinite(tile).all(axis=0)
            result[:, mask] = tile[:, mask]
            accepted.append({"path": str(path), "vertical_datum": raster_datum,
                             "source_reference": src.tags().get("SOURCE_REFERENCE", ""),
                             "source_url": src.tags().get("SOURCE_URL", ""),
                             "source_asset_year": src.tags().get("SOURCE_ASSET_YEAR", ""),
                             "window": [float(window.col_off), float(window.row_off), float(window.width), float(window.height)],
                             "read_shape": [out_h, out_w]})
    return result, accepted, skipped, warnings


def save_rgba(path, rgb, valid):
    import rasterio
    rgba = np.concatenate((np.clip(rgb, 0, 1), valid[..., None].astype(np.float32)), axis=2)
    rgba[~np.isfinite(rgba)] = 0
    data = np.moveaxis(np.round(rgba * 255).astype(np.uint8), 2, 0)
    with rasterio.open(path, "w", driver="PNG", width=rgb.shape[1], height=rgb.shape[0], count=4, dtype="uint8") as dst:
        dst.write(data)


def prepare(project, output, progress=lambda message: None, cancel=lambda: False):
    """Publish a complete local bundle only after all inputs have passed validation."""
    header = inspect_scan(project.scan, project.assume_lv95)
    area, origin = project_area(project, header)
    declared_datum = vertical_name(project.vertical_datum)
    source_datum = header["vertical_datum"]
    if source_datum != "UNKNOWN" and declared_datum != "UNKNOWN" and source_datum != declared_datum:
        raise DataError("The selected vertical datum conflicts with the scan CRS.")
    datum = source_datum if source_datum != "UNKNOWN" else declared_datum
    try:
        project_id = str(uuid.UUID(project.project_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DataError("The project identity must be a persistent UUID.") from exc
    warnings = []
    if header["crs_assumed"]:
        warnings.append("The LAS header has no CRS. EPSG:2056 is an explicit project assumption.")
    if datum == "UNKNOWN":
        warnings.append("The scan vertical datum is unknown. Heights retain the source values.")
    if declared_datum != "UNKNOWN" and source_datum == "UNKNOWN":
        warnings.append(f"The scan vertical datum {declared_datum} is a user declaration, not source metadata.")
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise DataError("Choose a new bundle directory. Existing bundles are preserved.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=output.name + ".partial-", dir=output.parent))
    try:
        scan = read_scan(project, header, area, origin, progress, cancel)
        np.savez(temp / "scan.npz", points=scan["points"], rgb=scan["rgb"],
                 classification=scan["classification"], source_ids=scan["source_ids"])
        np.savez(temp / "ground.npz", vertices=scan["ground_vertices"], faces=scan["ground_faces"], rgb=scan["ground_rgb"])
        save_rgba(temp / "scan_plan.png", scan["plan_rgb"], scan["plan_valid"])
        terrain, terrain_sources, terrain_skipped, terrain_warnings = raster_mosaic(
            project.terrain_folder, "terrain", area, project.terrain_step, datum, progress, cancel)
        terrain_valid = np.isfinite(terrain[0])
        terrain_vertices, terrain_faces, _ = grid_mesh(terrain[0], terrain_valid, area, project.terrain_step, origin)
        np.savez(temp / "terrain.npz", vertices=terrain_vertices, faces=terrain_faces)
        imagery, imagery_sources, imagery_skipped, _ = raster_mosaic(
            project.imagery_folder, "imagery", area, .25, datum, progress, cancel)
        image_valid = np.isfinite(imagery).all(axis=0)
        if image_valid.any():
            save_rgba(temp / "aerial.png", np.moveaxis(imagery, 0, 2), image_valid)
        if not terrain_sources:
            warnings.append("No local terrain tile intersects this area. Scan ground is limited to observed class 2 cells.")
        if not imagery_sources:
            warnings.append("No local aerial tile intersects this area. The plan image comes from scan RGB and is not aerial imagery.")
        if terrain_sources and not len(terrain_faces):
            warnings.append("Terrain tiles contain no complete valid cells in the selected area.")
        if imagery_sources and not image_valid.any():
            warnings.append("Imagery tiles contain no valid pixels in the selected area.")
        manifest = {
            "version": 1, "source": header, "origin": origin.tolist(), "epsg": 2056,
            "vertical_datum": datum, "vertical_datum_declared": declared_datum != "UNKNOWN" and source_datum == "UNKNOWN",
            "area": area, "units": "metres", "axes": "X east, Y north, Z up",
            "coordinate_rule": "LV95 XYZ = Blender XYZ + origin",
            "preview_points": len(scan["points"]), "preview_stride": scan["stride"],
            "roundtrip_error_m": scan["roundtrip_error_m"],
            "scan_ground_points": scan["ground_point_count"], "scan_ground_faces": len(scan["ground_faces"]),
            "scan_plan_step_m": project.ground_step,
            "scan_plan_area": [area[0], area[3] - scan["plan_valid"].shape[0] * project.ground_step,
                               area[0] + scan["plan_valid"].shape[1] * project.ground_step, area[3]],
            "aerial_area": [area[0], area[3] - image_valid.shape[0] * .25,
                            area[0] + image_valid.shape[1] * .25, area[3]],
            "terrain_faces": len(terrain_faces), "terrain_step_m": project.terrain_step,
            "terrain_sources": terrain_sources, "imagery_sources": imagery_sources,
            "skipped_terrain": terrain_skipped, "skipped_imagery": imagery_skipped,
            "warnings": warnings + terrain_warnings,
            "project_contract": {
                "schema": "railway-project-contract/1", "project_id": project_id,
                "name": "Local Swiss GIS project", "units": "metre", "tracks": [], "terrain_regions": [],
                "reference": {
                    "local_origin_m": origin.tolist(), "max_local_extent_m": 10_000.0,
                    "horizontal": {"crs": "EPSG:2056",
                                   "status": "ASSUMED" if header["crs_assumed"] else "VERIFIED",
                                   "evidence": "Explicit LV95 project assumption" if header["crs_assumed"] else "LAS CRS metadata"},
                    "vertical": {"datum": datum,
                                 "status": "VERIFIED" if source_datum != "UNKNOWN" else "DECLARED" if datum != "UNKNOWN" else "UNKNOWN",
                                 "evidence": "LAS vertical CRS metadata" if source_datum != "UNKNOWN" else "User project declaration" if datum != "UNKNOWN" else ""},
                    "official_chainage": {"status": "UNKNOWN"},
                },
            },
        }
        (temp / "project.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
        if cancel():
            raise DataError("Preparation canceled.")
        temp.rename(output)
        progress("Local scene data ready")
        return manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def load_manifest(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "project.json").read_text())
    if manifest.get("version") != 1 or manifest.get("epsg") != 2056:
        raise DataError("This is not a supported LV95 project bundle.")
    origin = np.array(manifest.get("origin"), dtype=np.float64)
    area = np.array(manifest.get("area"), dtype=np.float64)
    if origin.shape != (3,) or area.shape != (4,) or not np.isfinite(origin).all() or not np.isfinite(area).all():
        raise DataError("Project georeferencing is incomplete.")
    contract = manifest.get("project_contract")
    if contract is not None:
        reference = contract.get("reference", {})
        if (contract.get("schema") != "railway-project-contract/1" or contract.get("units") != "metre"
                or reference.get("local_origin_m") != manifest["origin"]
                or reference.get("horizontal", {}).get("crs") != "EPSG:2056"):
            raise DataError("The shared project reference conflicts with the GIS bundle.")
    for name in ("scan.npz", "ground.npz", "terrain.npz", "scan_plan.png"):
        local_file(folder / name)
    return manifest
