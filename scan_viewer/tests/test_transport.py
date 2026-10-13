"""Exercise pipe framing, worker recovery, paging, and shared memory at real RPC boundaries.

A real child process supplies malformed frames to protect worker termination and
replacement. The old client raised an error while leaving the child alive.
The paging case uses the production Rust binary and independently authored
point records; it detects joining complete pages as if they were AoS records.
The concurrent case protects the shared memory limit and released reservations.
Crop cancellation must join its writer before removing owned partial buffers.
A real child writes those buffers and a final shutdown write, exposing cleanup
order and orphaned output. A real Rust crop protects successful publication and
pre-existing destination/source preservation. Node transport does not own
output directories, so its framing and timeout cases miss this lifecycle.
"""

import importlib.util
import gc
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
import tracemalloc

import numpy as np
import pytest

from native_fixture import large_native_fixture

PLUGIN = Path(__file__).resolve().parents[1]
MODULE = Path(os.environ.get("SCAN_TRANSPORT_MODULE", PLUGIN / "addon/streaming.py"))
spec = importlib.util.spec_from_file_location("scan_transport", MODULE)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def executable(tmp_path, body):
    path = tmp_path / "worker"
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("frame", [
    '{"ok":true,"payload_bytes":67108865}\n',
    'invalid json\n',
])
def test_bad_frame_stops_worker_before_next_request(tmp_path, frame):
    worker = executable(tmp_path, "import sys\nsys.stdin.readline()\nsys.stdout.write(" + repr(frame)
                        + ")\nsys.stdout.flush()\nsys.stdin.readline()\n")
    client = transport.RustClient(worker)
    try:
        with pytest.raises((RuntimeError, ValueError)):
            client.request({"op": "plan", "views": []})
        assert client.process.poll() is not None, "A rejected frame left the worker blocked and reusable"
    finally:
        client.close()


def test_runtime_replaces_worker_after_framing_failure(tmp_path):
    worker = executable(tmp_path, '''import sys, pathlib
marker = pathlib.Path(__file__).with_suffix(".started")
first = not marker.exists()
marker.write_text("started")
for line in sys.stdin:
    sys.stdout.write('{"ok":true,"payload_bytes":67108865}\\n' if first else '{"ok":true,"payload_bytes":0,"nodes":[]}\\n')
    sys.stdout.flush()
''')
    runtime = transport.StreamRuntime(worker)
    try:
        with pytest.raises(RuntimeError, match="buffer size"):
            runtime._call({"op": "plan", "views": []})
        dead = runtime.local.client
        assert dead.process.poll() is not None, "Corrupted worker must stop before another RPC"
        response, payload = runtime._call({"op": "plan", "views": []})
        assert response["nodes"] == [] and not payload
        assert runtime.local.client.process.pid != dead.process.pid
    finally:
        runtime.close()


def test_stalled_worker_times_out_and_releases_shared_budget(tmp_path):
    worker = executable(tmp_path, "import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n")
    budget = transport.WorkingBudget(128 * 1024 * 1024)
    client = transport.RustClient(worker, timeout=0.2, working_budget=budget)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            client.request({"op": "plan", "views": []})
        assert client.process.poll() is not None and budget.used == 0
    finally:
        client.close()


def test_concurrent_workers_respect_shared_budget(tmp_path):
    worker = executable(tmp_path, '''import sys,time
for line in sys.stdin:
    time.sleep(0.1)
    sys.stdout.write('{"ok":true,"payload_bytes":0}\\n')
    sys.stdout.flush()
''')
    budget = transport.WorkingBudget(64 * 1024 * 1024)
    clients = [transport.RustClient(worker, timeout=3, working_budget=budget) for _ in range(3)]
    failures = []

    def call(client):
        try:
            client.request({"op": "plan", "views": []})
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=call, args=(client,)) for client in clients]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert not failures and all(not thread.is_alive() for thread in threads)
        assert budget.used == 0 and budget.peak == budget.limit
    finally:
        for client in clients:
            client.close()


def test_real_worker_pages_preserve_all_point_columns(tmp_path):
    binary = Path(os.environ.get("SCAN_CACHE_BINARY", PLUGIN / "target/debug/scan-cache"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"source identity")
    stamp = f"{source.stat().st_size}:{source.stat().st_mtime_ns}"
    cache = tmp_path / "cache"
    (cache / "nodes").mkdir(parents=True)
    positions = np.array([[1.25, 2.5, 3.75], [4.5, 5.25, 6.0], [7.5, 8.25, 9.0], [10.5, 11.25, 12.0]], dtype='<f4')
    colors = np.array([[1, 2, 3], [30, 40, 50], [60, 70, 80], [90, 100, 110]], dtype='u1')
    intensities = np.array([1, 1024, 32768, 65535], dtype='<u2')
    classes = np.array([1, 2, 6, 6], dtype='u1')
    data = b"".join(struct.pack('<fffBBBHB', *position, *color, int(intensity), int(classification))
                    for position, color, intensity, classification in zip(positions, colors, intensities, classes))
    (cache / "nodes/fixture.bin").write_bytes(data)
    (cache / "manifest.json").write_text(json.dumps(dict(version=1, source=str(source), stamp=stamp,
        source_points=4, retained_points=4, origin=[0, 0, 0], bounds_min=[0, 0, 0], bounds_max=[20, 20, 20],
        has_rgb=True, has_intensity=True, has_classification=True,
        nodes=[dict(key=[0, 0, 0, 0], count=4, bounds=[[0, 0, 0], [20, 20, 20]], spacing=1,
                    file="nodes/fixture.bin", class_counts=[[1, 1], [2, 1], [6, 2]])])))
    client = transport.RustClient(binary)
    try:
        packet, payload = client.request(dict(op="node", source=str(source), cache=str(cache),
            stamp=stamp, file="nodes/fixture.bin", node_total_count=4, source_count=4,
            cache_origin=[0, 0, 0], bounds=[[0, 0, 0], [20, 20, 20]], origin=[0, 0, 0],
            class_filter=-1, has_rgb=True, max_payload_bytes=36))
        assert packet["count"] == 4 and len(payload) == 72
        np.testing.assert_array_equal(np.frombuffer(payload, '<f4', count=12).reshape(-1, 3), positions)
        np.testing.assert_array_equal(np.frombuffer(payload, 'u1', count=12, offset=48).reshape(-1, 3), colors)
        np.testing.assert_array_equal(np.frombuffer(payload, '<u2', count=4, offset=60), intensities)
        np.testing.assert_array_equal(np.frombuffer(payload, 'u1', count=4, offset=68), classes)
    finally:
        client.close()


def test_default_frame_boundary_keeps_complete_node_and_worker_reusable(tmp_path):
    source, cache, stamp, count = large_native_fixture(tmp_path)
    binary = Path(os.environ.get("SCAN_CACHE_BINARY", PLUGIN / "target/debug/scan-cache"))
    client = transport.RustClient(binary)
    try:
        packet, payload = client.request(dict(op="node", source=str(source), cache=str(cache), stamp=stamp,
                                             node=0, origin=[0, 0, 0], source_count=count))
        assert packet["count"] == count and len(payload) == count * 18
        for index in (0, count - 1):
            assert struct.unpack_from('<fff', payload, index * 12) == (1.0, 2.0, 3.0)
            assert payload[count * 12 + index * 3:count * 12 + index * 3 + 3] == b'\x07\x08\x09'
            assert struct.unpack_from('<H', payload, count * 15 + index * 2) == (128,)
            assert payload[count * 17 + index] == 6
        idle, empty = client.request({"op": "plan", "views": []})
        assert idle["ok"] and idle["nodes"] == [] and not empty
    finally:
        client.close()


def test_completed_node_stays_charged_until_blender_consumes_it(tmp_path):
    from test_worker_protocol import native_fixture

    binary = Path(os.environ.get("SCAN_CACHE_BINARY", PLUGIN / "target/debug/scan-cache"))
    records = [((index / 4, 0, 0), (255, index, 0), index * 1000, 6) for index in range(4)]
    source, cache, _ = native_fixture(tmp_path, records)
    runtime = transport.StreamRuntime(binary)
    view = dict(source=str(source), cache=str(cache), object="fixture", origin=[0, 0, 0],
                matrix=np.eye(4).tolist(), width=1200, height=900)
    try:
        runtime.request_view([view], 4, 1.0, 4)
        deadline = time.monotonic() + 5
        while runtime.planned_generation != runtime.generation and time.monotonic() < deadline:
            runtime.tick()
            time.sleep(0.01)
        while runtime.completed_reads.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not runtime.completed_reads.empty(), "Real decoder did not deliver a node"
        assert runtime.working_budget.used == 72, "Queued point bytes escaped the working-memory budget"
        assert runtime.cpu_bytes == 0
        runtime.tick()
        assert runtime.working_budget.used == 0 and runtime.cpu_bytes == 72
    finally:
        runtime.close()
    assert runtime.working_budget.used == 0


def test_underreported_node_count_is_rejected_before_assembly(tmp_path):
    from test_worker_protocol import native_fixture

    binary = Path(os.environ.get("SCAN_CACHE_BINARY", PLUGIN / "target/debug/scan-cache"))
    records = [((index / 4, 0, 0), (255, index, 0), index * 1000, 6) for index in range(4)]
    source, cache, manifest = native_fixture(tmp_path, records)
    budget = transport.WorkingBudget(128 * 1024 * 1024)
    client = transport.RustClient(binary, working_budget=budget)
    try:
        with pytest.raises(RuntimeError, match="stale point count"):
            client.request(dict(op="node", source=str(source), cache=str(cache), stamp=manifest["stamp"],
                                node=0, origin=[0, 0, 0], source_count=1))
        assert client.process.poll() is not None and budget.used == 0
    finally:
        client.close()


def test_evicted_large_node_releases_python_memory_while_workers_are_idle(tmp_path):
    source, cache, _, count = large_native_fixture(tmp_path)
    binary = Path(os.environ.get("SCAN_CACHE_BINARY", PLUGIN / "target/debug/scan-cache"))
    runtime = transport.StreamRuntime(binary)
    runtime.cpu_limit = 64 * 1024 * 1024
    matrix = np.eye(4)
    matrix[:3, :3] *= 0.25
    matrix[:3, 3] = -0.5
    view = dict(source=str(source), cache=str(cache), object="fixture", origin=[0, 0, 0],
                matrix=matrix.tolist(), width=1200, height=900)
    tracemalloc.start()
    try:
        runtime.request_view([view], count, 1.0, count)
        deadline = time.monotonic() + 30
        while runtime.planned_generation != runtime.generation and time.monotonic() < deadline:
            runtime.tick()
            time.sleep(0.01)
        while runtime.completed_reads.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not runtime.completed_reads.empty(), "Real decoder did not deliver the large node"
        assert tracemalloc.get_traced_memory()[0] >= count * 18
        runtime.request_view([], 0, 1.0, 0)
        runtime.tick()
        gc.collect()
        assert runtime.cpu_bytes == 0 and runtime.working_budget.used == 0
        assert tracemalloc.get_traced_memory()[0] < count * 18 // 4, "Evicted point buffers remain alive outside the cache budgets"
    finally:
        runtime.close()
        tracemalloc.stop()


def blocked_tool_worker(tmp_path):
    return executable(tmp_path, """import json,pathlib,sys,time
root = pathlib.Path(__file__).parent
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] != 'plan':
        (root / 'tool-started').touch()
        while not (root / 'release-tool').exists():
            time.sleep(0.01)
    sys.stdout.write(json.dumps({'ok': True, 'payload_bytes': 0, 'nodes': [], 'elapsed_ms': 0}) + '\\n')
    sys.stdout.flush()
""")


def wait_for_tool(tmp_path):
    deadline = time.monotonic() + 3
    while not (tmp_path / 'tool-started').exists():
        assert time.monotonic() < deadline, "The real child process never received its tool request"
        time.sleep(0.01)


def test_tool_admission_is_bounded_and_recovers_after_completion(tmp_path):
    runtime = transport.StreamRuntime(blocked_tool_worker(tmp_path))
    try:
        first = runtime.submit_pick({'op': 'pick'})
        wait_for_tool(tmp_path)
        queued = runtime.submit_pick({'op': 'pick'})
        with pytest.raises(RuntimeError, match='busy'):
            runtime.submit_pick({'op': 'pick'})
        assert queued.cancel(), "A pending tool could not be canceled"
        queued = runtime.submit_pick({'op': 'pick'})
        (tmp_path / 'release-tool').touch()
        assert first.result(timeout=3)[0]['ok']
        assert queued.result(timeout=3)[0]['ok']
        assert runtime.submit_pick({'op': 'pick'}).result(timeout=3)[0]['ok']
    finally:
        runtime.close()


def test_viewport_plan_completes_while_a_tool_is_blocked(tmp_path):
    runtime = transport.StreamRuntime(blocked_tool_worker(tmp_path))
    try:
        tool = runtime.submit_pick({'op': 'pick'})
        wait_for_tool(tmp_path)
        runtime.request_view([], 1000, 1.0, 1000)
        deadline = time.monotonic() + 3
        while runtime.planned_generation != runtime.generation and time.monotonic() < deadline:
            runtime.tick()
            time.sleep(0.01)
        assert runtime.planned_generation == runtime.generation, "A pick blocked the viewport planner"
        assert not tool.done(), "The tool fixture did not hold its RPC while the viewport planned"
        assert runtime.stats()['plans'] == 1 and not runtime.error
    finally:
        runtime.close()


def test_timed_out_crop_joins_writer_and_removes_owned_partial_output(tmp_path, monkeypatch):
    source = tmp_path / 'source.scan'
    source.write_bytes(b'unchanged source')
    cache = tmp_path / 'source-cache'
    cache.mkdir()
    (cache / 'sentinel').write_bytes(b'unchanged source cache')
    worker = executable(tmp_path, """import json,os,pathlib,signal,sys,time
root = pathlib.Path(__file__).parent
request = json.loads(sys.stdin.readline())
output = pathlib.Path(request['output'])
output.mkdir(parents=True)
(output / 'positions.f32').write_bytes(b'partial point buffer')
state = {'pid': os.getpid(), 'output': str(output)}
def stop(signum, frame):
    state['output_existed_at_termination'] = output.is_dir()
    time.sleep(0.05)
    (output / 'last-buffer').write_bytes(b'writer finished before cleanup')
    state['writer_finished'] = True
    (root / 'worker-state.json').write_text(json.dumps(state))
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'worker-state.json').write_text(json.dumps(state))
while True:
    time.sleep(1)
""")
    monkeypatch.setattr(transport, 'REQUEST_TIMEOUT_SECONDS', 1.0)
    before = set(tmp_path.iterdir())
    runtime = transport.StreamRuntime(worker)
    destination = tmp_path / 'crop-output'
    try:
        future = runtime.submit_crop({'op': 'crop', 'source': str(source), 'origin': [0, 0, 0],
                                     'bounds': [[-1] * 3, [1] * 3], 'point_limit': 1, 'output': str(destination),
                                     'source_specs': [{'source': str(source), 'cache': str(cache), 'origin': [0, 0, 0],
                                                       'bounds': [[-1] * 3, [1] * 3]}]})
        with pytest.raises(RuntimeError, match='timed out'):
            future.result(timeout=4)
        state = json.loads((tmp_path / 'worker-state.json').read_text())
        assert state['output_existed_at_termination'] and state['writer_finished']
        with pytest.raises(ProcessLookupError):
            os.kill(state['pid'], 0)
        assert not Path(state['output']).exists(), 'Completed crop failure left its writer-owned buffers'
        assert not destination.exists()
        assert set(tmp_path.iterdir()) == before | {tmp_path / 'worker-state.json'}
        assert source.read_bytes() == b'unchanged source'
        assert (cache / 'sentinel').read_bytes() == b'unchanged source cache'
    finally:
        runtime.close()


def test_real_crop_publishes_complete_output_and_preserves_existing_destinations(tmp_path):
    from test_worker_protocol import native_fixture

    records = [((index / 4, 0, 0), (255, index, 0), index * 1000, 6) for index in range(4)]
    source, cache, manifest = native_fixture(tmp_path, records)
    before = {path: path.read_bytes() for path in cache.rglob('*') if path.is_file()}
    before[source] = source.read_bytes()
    destination = tmp_path / 'crop-output'
    request = dict(op='crop', source=str(source), origin=[0, 0, 0], bounds=[[-1] * 3, [1] * 3],
                   point_limit=4, output=str(destination), source_specs=[dict(source=str(source), cache=str(cache),
                   stamp=manifest['stamp'], origin=[0, 0, 0], bounds=[[-1] * 3, [1] * 3])])
    binary = Path(os.environ.get('SCAN_CACHE_BINARY', PLUGIN / 'target/debug/scan-cache'))
    runtime = transport.StreamRuntime(binary)
    try:
        response, payload = runtime.submit_crop(request).result(timeout=5)
        assert response['output'] == str(destination) and not payload
        published = json.loads((destination / 'manifest.json').read_text())
        assert published['retained_points'] == 4 and response['manifest']['retained_points'] == 4
        np.testing.assert_array_equal(np.fromfile(destination / 'positions.f32', dtype='<f4').reshape(-1, 3),
                                      [record[0] for record in records])
        np.testing.assert_array_equal(np.fromfile(destination / 'source_ids.u64', dtype='<u8'), [0, 1, 2, 3])
        (destination / 'sentinel').write_bytes(b'previous output data')
        complete = {path: path.read_bytes() for path in destination.iterdir()}
        for existing in (destination, cache):
            with pytest.raises(RuntimeError, match='already exists'):
                runtime.submit_crop(dict(request, output=str(existing))).result(timeout=5)
        assert {path: path.read_bytes() for path in destination.iterdir()} == complete
        assert {path: path.read_bytes() for path in before} == before
        assert set(tmp_path.iterdir()) == {source, cache, destination}
    finally:
        runtime.close()
