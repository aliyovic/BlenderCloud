"""Regenerate the four authored COPC points with laspy and lazrs, outside CI."""
from pathlib import Path
import io
import struct

import laspy
import lazrs
import numpy as np
from laspy.vlrs.known import LasZipVlr

header = laspy.LasHeader(version='1.4', point_format=7)
header.scales = np.array([0.001]*3)
header.offsets = np.array([2689000.0, 1254000.0, 400.0])
header.are_points_compressed = True
points = laspy.ScaleAwarePointRecord.zeros(4, header=header)
points.x = [2689160.125, 2689161.25, 2689162.375, 2689163.5]
points.y = [1254972.25]*4
points.z = [452.125, 452.25, 452.375, 452.5]
points.intensity = [0, 1, 32768, 65535]
points.classification = [2, 6, 6, 2]
points.red = [65535, 32768, 0, 256]
points.green = [0, 65535, 32768, 256]
points.blue = [0, 0, 65535, 256]
points.return_number = [1]*4
points.number_of_returns = [1]*4
header.update(points)
vlr = lazrs.LazVlr.new_for_compression(7, 0)
compressed = bytearray(lazrs.compress_points(vlr, points.array.tobytes(), False))
table_offset = struct.unpack_from('<q', compressed)[0]


def info(hierarchy_offset):
    return struct.pack('<3dddQQdd88x', 2689162.0, 1254972.0, 452.5, 1000.0,
                       1.0, hierarchy_offset, 32, 0.0, 0.0)


header.vlrs = [laspy.VLR('copc', 1, 'COPC info VLR', info(0)), LasZipVlr(vlr.record_data())]
buffer = io.BytesIO()
header.write_to(buffer)
point_offset = len(buffer.getvalue())
evlr_offset = point_offset + len(compressed)
header.vlrs[0] = laspy.VLR('copc', 1, 'COPC info VLR', info(evlr_offset + 60))
header.start_of_first_evlr = evlr_offset
header.number_of_evlrs = 1
buffer = io.BytesIO()
header.write_to(buffer)
struct.pack_into('<q', compressed, 0, point_offset + table_offset)
hierarchy = struct.pack('<4iQii', 0, 0, 0, 0, point_offset + 8, table_offset - 8, 4)
evlr = struct.pack('<H16sHQ32s', 0, b'copc', 1000, len(hierarchy), b'COPC hierarchy')
path = Path(__file__).with_name('points.copc.laz')
path.write_bytes(buffer.getvalue() + compressed + evlr + hierarchy)
with laspy.CopcReader.open(path) as reader:
    decoded = reader.query()
    assert decoded.intensity.tolist() == [0, 1, 32768, 65535]
    np.testing.assert_array_equal(decoded.x, points.x)
print(f'Wrote and independently decoded {path.name}, {path.stat().st_size} bytes')
