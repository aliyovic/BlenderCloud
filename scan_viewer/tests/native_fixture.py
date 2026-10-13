"""Independently authored native records spanning the 64 MiB frame boundary."""

import json
import struct


def large_native_fixture(tmp_path):
    count = 3_728_271  # Eighteen-byte records exceed the old 64 MiB frame by fourteen bytes.
    source = tmp_path / "source.xyz"
    source.write_text("source observation\n")
    stamp = f"{source.stat().st_size}:{source.stat().st_mtime_ns}"
    cache = tmp_path / "cache"
    (cache / "nodes").mkdir(parents=True)
    record = struct.pack('<fffBBBHB', 1.0, 2.0, 3.0, 7, 8, 9, 128, 6)
    with (cache / "nodes/fixture.bin").open('wb') as stream:
        block = record * 8192
        for _ in range(count // 8192):
            stream.write(block)
        stream.write(record * (count % 8192))
    (cache / "manifest.json").write_text(json.dumps(dict(version=1, source=str(source), stamp=stamp,
        source_points=count, retained_points=count, origin=[0, 0, 0], bounds_min=[0, 0, 0], bounds_max=[4, 4, 4],
        has_rgb=True, has_intensity=True, has_classification=True,
        nodes=[dict(key=[0, 0, 0, 0], count=count, bounds=[[0, 0, 0], [4, 4, 4]], spacing=1,
                    file="nodes/fixture.bin", class_counts=[[6, count]])])))
    return source, cache, stamp, count
