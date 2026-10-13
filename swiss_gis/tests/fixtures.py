"""Create tiny local LAS and GeoTIFF files with known coordinates."""
import laspy
import numpy as np
from pyproj import CRS
import rasterio
from rasterio.transform import from_origin


def fixture_scan(path, crs=None):
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = [.001, .001, .001]
    header.offsets = [2689000, 1254000, 400]
    if crs:
        header.add_crs(CRS.from_user_input(crs))
    scan = laspy.LasData(header)
    xyz = np.array([[2689160.125+x, 1254970.875+y, 450.321+.1*x+.2*y]
                    for y in range(3) for x in range(3)])
    scan.x, scan.y, scan.z = xyz.T
    scan.classification = np.full(9, 2, dtype=np.uint8)
    scan.red = np.full(9, 65535, dtype=np.uint16)
    scan.green = np.zeros(9, dtype=np.uint16)
    scan.blue = np.zeros(9, dtype=np.uint16)
    scan.write(path)
    return xyz


def fixture_raster(path, data, x=2689158, y=1254975, datum=None):
    data = data[None] if data.ndim == 2 else data
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[2], height=data.shape[1],
                       count=data.shape[0], dtype=data.dtype, crs="EPSG:2056",
                       transform=from_origin(x, y, 1, 1), nodata=-9999 if data.dtype.kind == "f" else None) as dst:
        dst.write(data)
        if datum:
            dst.update_tags(VERTICAL_DATUM=datum)
