"""Exercise the worker process against generated points and published byte layouts."""

import json
import os
from pathlib import Path
import select
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('SCAN_CACHE_BINARY', ROOT / 'target/debug/scan-cache'))
COPC = ROOT / 'tests/fixtures/points.copc.laz'


class Worker:
    def __init__(self):
        self.process = subprocess.Popen([str(BINARY), 'serve'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def request(self, value):
        deadline = time.monotonic() + 10
        request = json.dumps(value).encode() + b'\n'
        written = 0
        while written < len(request):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise AssertionError('Worker request write timed out')
            written += os.write(self.process.stdin.fileno(), request[written:written+4096])
        header = bytearray()
        while not header.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise AssertionError('Worker response header timed out')
            byte = self.process.stdout.read(1)
            if not byte:
                error = self.process.stderr.read().decode() if self.process.poll() is not None else ''
                raise AssertionError('Worker closed stdout without a JSON response: ' + error)
            header.extend(byte)
        response = json.loads(header)
        data = bytearray()
        while len(data) < response['payload_bytes']:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise AssertionError('Worker response payload timed out')
            chunk = self.process.stdout.read(response['payload_bytes'] - len(data))
            if not chunk:
                raise AssertionError('Worker exited inside a point payload')
            data.extend(chunk)
        return response, bytes(data)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()


def stamp(path):
    info = path.stat()
    return f'{info.st_size}:{info.st_mtime_ns}'


def native_fixture(directory, records):
    source = directory / 'source.xyz'
    source.write_text('source fixture\n')
    cache = directory / 'cache'
    (cache / 'nodes').mkdir(parents=True)
    data = b''.join(struct.pack('<3f3BHB', *xyz, *rgb, intensity, classification)
                    for xyz, rgb, intensity, classification in records)
    (cache / 'nodes/fixture.bin').write_bytes(data)
    classes = {}
    for _, _, _, classification in records:
        classes[classification] = classes.get(classification, 0) + 1
    manifest = dict(version=1, source=str(source), stamp=stamp(source), source_points=len(records),
                    retained_points=len(records), origin=[0, 0, 0], bounds_min=[-1]*3,
                    bounds_max=[1]*3, has_rgb=True, has_intensity=True, has_classification=True,
                    nodes=[dict(key=[0]*4, count=len(records), bounds=[[-1]*3, [1]*3], spacing=1,
                                file='nodes/fixture.bin', class_counts=list(classes.items()))])
    (cache / 'manifest.json').write_text(json.dumps(manifest))
    return source, cache, manifest


def node_request(source, cache, manifest, **extra):
    return dict(op='node', source=str(source), cache=str(cache), stamp=manifest['stamp'], node=0,
                origin=[0]*3, **extra)


def rows(packet, data):
    count = packet['count']
    xyz = struct.unpack_from(f'<{count*3}f', data)
    intensity = struct.unpack_from(f'<{count}H', data, count*15)
    return [(tuple(xyz[i*3:i*3+3]), tuple(data[count*12+i*3:count*12+i*3+3]),
             intensity[i], data[count*17+i]) for i in range(count)]


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='scan-worker-')
        self.root = Path(self.directory.name)
        self.worker = Worker()
        self.records = [((i/4, 0, 0), (255, i, 0), i*1000, 6) for i in range(4)]
        self.source, self.cache, self.manifest = native_fixture(self.root, self.records)

    def tearDown(self):
        self.worker.close()
        self.directory.cleanup()

    def test_malformed_native_count_returns_error_and_worker_survives(self):
        response, data = self.worker.request(node_request(
            self.source, self.cache, self.manifest, file='nodes/fixture.bin', source_count=1,
            node_total_count=4, cache_origin=[0]*3, class_filter=6, has_rgb=True, bounds=[[-1]*3,[1]*3]))
        self.assertFalse(response['ok'])
        self.assertIn('count', response['error'].lower())
        self.assertEqual(data, b'')
        response, data = self.worker.request(node_request(self.source, self.cache, self.manifest))
        self.assertTrue(response['ok'], response)
        self.assertEqual(rows(response, data), self.records)

    def test_cached_native_source_revalidates_after_source_changes(self):
        view = dict(source=str(self.source), cache=str(self.cache), object='fixture', origin=[0]*3,
                    matrix=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], width=100, height=100)
        request = dict(op='plan', views=[view], budget=4, gpu_budget=4, pixel_error=1)
        response, _ = self.worker.request(request)
        self.assertTrue(response['ok'], response)
        self.assertTrue(response['nodes'])
        self.source.write_text('changed source with a different size\n')
        response, _ = self.worker.request(request)
        self.assertFalse(response['ok'])
        self.assertIn('stale', response['error'].lower())

    def test_cache_commands_reject_external_node_paths_before_mutation(self):
        for operation in ('compact-octree', 'index-octree-classes', 'node'):
            for path_kind in ('traversal', 'absolute', 'symlink'):
                with self.subTest(operation=operation, path_kind=path_kind):
                    directory = self.root / f'{operation}-{path_kind}'
                    directory.mkdir()
                    source, cache, manifest = native_fixture(directory, self.records)
                    original = (cache / 'nodes/fixture.bin').read_bytes()
                    outside = directory / 'outside.bin'
                    outside.write_bytes(original)
                    unsafe_path = '../outside.bin' if path_kind == 'traversal' else str(outside)
                    if path_kind == 'symlink':
                        (cache / 'nodes/escape.bin').symlink_to(outside)
                        unsafe_path = 'nodes/escape.bin'
                    unsafe = dict(manifest['nodes'][0], file=unsafe_path, class_counts=[])
                    safe = dict(manifest['nodes'][0], key=[0, 1, 0, 0], class_counts=[])
                    manifest['nodes'] = [unsafe, safe]
                    manifest['retained_points'] = 8
                    manifest_path = cache / 'manifest.json'
                    manifest_path.write_text(json.dumps(manifest))
                    before = manifest_path.read_bytes()
                    if operation == 'node':
                        response, payload = self.worker.request(node_request(source, cache, manifest))
                        self.assertFalse(response['ok'], response)
                        self.assertIn('node path', response['error'].lower())
                        self.assertEqual(payload, b'')
                    else:
                        result = subprocess.run([str(BINARY), operation, str(cache)],
                                                capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('node path', result.stderr.lower())
                    self.assertEqual(outside.read_bytes(), original)
                    self.assertEqual((cache / 'nodes/fixture.bin').read_bytes(), original)
                    self.assertEqual(manifest_path.read_bytes(), before)
                    self.assertFalse((cache / 'compact-tmp').exists())

    def test_crop_rejects_invalid_output_transforms_without_partial_delivery(self):
        transforms = {
            'projective': [[1,0,0,0],[0,1,0,0],[0,0,1,0],[1,0,0,0]],
            'overflowing': [[1e308,0,0,1.5e308],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
        }
        for name, matrix in transforms.items():
            with self.subTest(transform=name):
                destination = self.root / f'crop-{name}'
                response, payload = self.worker.request(dict(
                    op='crop', source=str(self.source), output=str(destination), origin=[0]*3,
                    bounds=[[-1]*3, [1]*3], point_limit=4, source_specs=[dict(
                        source=str(self.source), cache=str(self.cache), origin=[0]*3,
                        bounds=[[-1]*3, [1]*3], stamp=self.manifest['stamp'], to_output=matrix)]))
                self.assertFalse(response['ok'], response)
                self.assertIn('transform', response['error'].lower())
                self.assertEqual(payload, b'')
                self.assertFalse(destination.exists())
        response, data = self.worker.request(node_request(self.source, self.cache, self.manifest))
        self.assertTrue(response['ok'], response)
        self.assertEqual(rows(response, data), self.records)

    def test_compacted_native_nodes_remain_readable_with_indexed_classes(self):
        node = self.manifest['nodes'][0]
        duplicate = dict(node, key=[0,1,0,0], file='nodes/second.bin')
        (self.cache / duplicate['file']).write_bytes((self.cache / node['file']).read_bytes())
        self.manifest['nodes'] = [dict(node, class_counts=[]), dict(duplicate, class_counts=[])]
        self.manifest['retained_points'] = 8
        (self.cache / 'manifest.json').write_text(json.dumps(self.manifest))
        for operation in ('compact-octree', 'index-octree-classes'):
            result = subprocess.run([str(BINARY), operation, str(self.cache)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        compacted = json.loads((self.cache / 'manifest.json').read_text())
        self.assertEqual(len(compacted['nodes']), 1)
        self.assertEqual(compacted['nodes'][0]['class_counts'], [[6,8]])
        response, data = self.worker.request(node_request(self.source, self.cache, compacted))
        self.assertTrue(response['ok'], response)
        self.assertEqual(sorted(rows(response, data)), sorted(self.records * 2))

    def test_affine_cross_source_crop_preserves_local_points_and_source_ids(self):
        directory = self.root / 'second-source'
        directory.mkdir()
        source, cache, manifest = native_fixture(directory, self.records)
        destination = self.root / 'affine-crop'
        origins = [2689000, 1254000, 400]
        specs = []
        for observation, native, evidence, shift in (
            (self.source, self.cache, self.manifest, 1), (source, cache, manifest, 2)
        ):
            specs.append(dict(source=str(observation), cache=str(native), origin=[0]*3,
                              bounds=[[-1]*3, [1]*3], stamp=evidence['stamp'],
                              to_output=[[0,-1,0,shift],[1,0,0,0],[0,0,1,0],[0,0,0,1]]))
        response, _ = self.worker.request(dict(op='crop', source=str(self.source),
            output=str(destination), origin=origins, bounds=[[-1]*3, [1]*3],
            point_limit=8, source_specs=specs))
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['manifest']['retained_points'], 8)
        positions = struct.unpack('<24f', (destination / 'positions.f32').read_bytes())
        expected = [(shift, index/4, 0) for shift in (1, 2) for index in range(4)]
        self.assertEqual([tuple(positions[index:index+3]) for index in range(0,24,3)], expected)
        self.assertEqual(struct.unpack('<8Q', (destination / 'source_ids.u64').read_bytes()),
                         (0,1,2,3,1 << 56,(1 << 56)+1,(1 << 56)+2,(1 << 56)+3))
        self.assertEqual((destination / 'colors.u8').read_bytes(),
                         b''.join(bytes(record[1]) for record in self.records) * 2)

    def test_native_pages_preserve_every_column_and_finish_on_same_worker(self):
        offset = 0
        received = []
        while True:
            response, data = self.worker.request(node_request(
                self.source, self.cache, self.manifest, point_offset=offset, max_payload_bytes=36))
            self.assertTrue(response['ok'], response)
            self.assertLessEqual(len(data), 36)
            self.assertEqual(response['total_count'], 4)
            self.assertEqual(response['point_offset'], offset)
            received.extend(rows(response, data))
            offset = response['next_offset']
            if offset is None:
                break
        self.assertEqual(received, self.records)
        response, _ = self.worker.request(dict(op='unknown'))
        self.assertFalse(response['ok'])
        self.assertIn('Unknown', response['error'])

    def test_copc_preview_intensity_is_normalized(self):
        output = self.root / 'preview'
        result = subprocess.run([str(BINARY), str(COPC), str(output), '--max-points', '4'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = struct.unpack('<4f', (output / 'intensity.f32').read_bytes())
        for value, integer in zip(actual, [0, 1, 32768, 65535]):
            self.assertAlmostEqual(value, integer/65535, places=6)

    def test_copc_paging_and_crop_preserve_coordinates_classes_and_source_ids(self):
        request = dict(op='node', source=str(COPC), stamp=stamp(COPC), node=0,
                       origin=[2689000,1254000,400], max_payload_bytes=36)
        output_rows = []
        offset = 0
        while True:
            response, data = self.worker.request(dict(request, point_offset=offset))
            self.assertTrue(response['ok'], response)
            self.assertLessEqual(len(data), 36)
            output_rows.extend(rows(response, data))
            offset = response['next_offset']
            if offset is None:
                break
        expected = [((160.125,972.25,52.125),(255,0,0),0,2),
                    ((161.25,972.25,52.25),(128,255,0),1,6),
                    ((162.375,972.25,52.375),(0,128,255),32768,6),
                    ((163.5,972.25,52.5),(1,1,1),65535,2)]
        self.assertEqual(sorted(output_rows), expected)
        filtered = []
        offset = 0
        while True:
            response, data = self.worker.request(dict(request, point_offset=offset,
                class_filter=6, source_count=2, max_payload_bytes=18))
            self.assertTrue(response['ok'], response)
            self.assertEqual(response['total_count'], 2)
            self.assertEqual(response['count'], 1)
            filtered.extend(rows(response, data))
            offset = response['next_offset']
            if offset is None:
                break
        self.assertEqual(sorted(filtered), expected[1:3])
        destination = self.root / 'crop'
        response, _ = self.worker.request(dict(op='crop', source=str(COPC),
            output=str(destination), origin=request['origin'],
            bounds=[[2689160,1254972,452],[2689164,1254973,453]], point_limit=4))
        self.assertTrue(response['ok'], response)
        ids = struct.unpack('<4Q', (destination/'source_ids.u64').read_bytes())
        self.assertEqual(ids, (0,1,2,3))
        self.assertEqual((destination/'classification.u8').read_bytes(), bytes([2,6,6,2]))
        xyz = struct.unpack('<12f', (destination/'positions.f32').read_bytes())
        self.assertEqual([tuple(xyz[i:i+3]) for i in range(0,12,3)], [item[0] for item in expected])
        overflow = self.root / 'overflow'
        response, _ = self.worker.request(dict(op='crop', source=str(COPC), output=str(overflow),
            origin=request['origin'], bounds=[[2689160,1254972,452],[2689164,1254973,453]], point_limit=3))
        self.assertFalse(response['ok'])
        self.assertIn('exceeds', response['error'])
        self.assertFalse(overflow.exists())


    def test_pick_returns_nearest_source_id_and_local_plane_normal(self):
        directory = self.root / 'plane'
        directory.mkdir()
        points = [((0,0,0),(0,0,0),0,6), ((0.5,0,0),(0,0,0),0,6),
                  ((0,0.5,0),(0,0,0),0,6), ((0.5,0.5,0),(0,0,0),0,6)]
        source, cache, _ = native_fixture(directory, points)
        response, _ = self.worker.request(dict(op='pick', pick_radius=0.05, plane_radius=1,
            pick_sources=[dict(source=str(source), cache=str(cache), ray_origin=[0,0,1],
                               ray_direction=[0,0,-1], max_distance=2)]))
        self.assertTrue(response['ok'], response)
        self.assertTrue(response['found'])
        self.assertEqual(response['source_id'], 0)
        self.assertEqual(response['xyz'], [0,0,0])
        self.assertEqual(response['plane_points'], 4)
        for value, expected in zip(response['normal'], [0,0,1]):
            self.assertAlmostEqual(value, expected, places=9)


if __name__ == '__main__':
    unittest.main()
