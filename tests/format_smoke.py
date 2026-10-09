import json
from pathlib import Path
import struct
import subprocess

import numpy as np

root = Path(__file__).resolve().parents[1]
source = root.parent / "CRAS_Lab/craslab_sample_1in1000.ply"
with source.open() as stream:
    for line in stream:
        if line.strip() == "end_header":
            break
    rows = [stream.readline().split() for _ in range(1000)]

text = [" ".join(row) for row in rows]
fixtures = {
    "xyz": "\n".join(" ".join(row[:3]) for row in rows) + "\n",
    "pts": "1000\n" + "\n".join(text) + "\n",
    "asc": "//X Y Z R G B Intensity Classification\n1000\n" + "\n".join(text) + "\n",
    "txt": "\n".join(text) + "\n",
    "csv": "x,y,z,red,green,blue,intensity,classification\n" +
           "\n".join(",".join(row) for row in rows) + "\n",
}

for extension, content in fixtures.items():
    path = root / "tests" / f"cras_1000.{extension}"
    path.write_text(content)

big = root / "tests/cras_big_endian_1000.ply"
with big.open("wb") as stream:
    stream.write(b"ply\nformat binary_big_endian 1.0\nelement vertex 1000\n"
                 b"property float x\nproperty float y\nproperty float z\n"
                 b"property uchar red\nproperty uchar green\nproperty uchar blue\n"
                 b"property float intensity\nproperty uchar classification\nend_header\n")
    for row in rows:
        stream.write(struct.pack(">fffBBBfB", *[float(x) for x in row[:3]],
                                 *[int(x) for x in row[3:6]], float(row[6]), int(row[7])))

for path in [root / "tests" / f"cras_1000.{ext}" for ext in fixtures] + [big]:
    cache = root / "cache" / ("format_" + path.suffix[1:] + ("_big" if path == big else ""))
    subprocess.run([str(root / "target/release/scan-cache"), str(path), str(cache),
                    "--max-points", "1000"], check=True, capture_output=True)
    manifest = json.loads((cache / "manifest.json").read_text())
    assert manifest["retained_points"] == 1000, path
    points = np.memmap(cache / "positions.f32", dtype="<f4", shape=(1000, 3))
    np.testing.assert_allclose(points[0], [float(x) for x in rows[0][:3]], atol=1e-5)
    print("FORMAT_OK", path.suffix, manifest["retained_points"])
