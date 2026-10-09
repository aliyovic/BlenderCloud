"""Rust worker protocol and bounded caches. Blender calls these methods on its main thread."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import queue
import subprocess
import threading
import time

import numpy as np

def node_key(node):
    return (node["source"], node.get("cache", ""), node["stamp"], node["node"],
            tuple(node["origin"]), node.get("class_filter", -1))


class RustClient:
    def __init__(self, binary):
        self.process = subprocess.Popen([str(binary), "serve"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def request(self, message):
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Rust point reader stopped")
        response = json.loads(line)
        size = response.get("payload_bytes", 0)
        if size < 0 or size > 64 * 1024 * 1024:
            raise RuntimeError("Invalid point buffer size from Rust")
        payload = bytearray(size)
        view = memoryview(payload)
        offset = 0
        while offset < size:
            count = self.process.stdout.readinto(view[offset:])
            if not count:
                raise RuntimeError("Incomplete point buffer from Rust")
            offset += count
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Rust point reader failed"))
        return response, payload

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class StreamRuntime:
    def __init__(self, binary):
        self.binary = binary
        self.planner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-plan")
        self.worker_count = 6
        self.condition = threading.Condition()
        self.pending_reads = []
        self.completed_reads = queue.SimpleQueue()
        self.active_reads = set()
        self.threads = [threading.Thread(target=self._decode_loop, name=f"scan-decode-{i}", daemon=True)
                        for i in range(self.worker_count)]
        self.local = threading.local()
        self.clients = []
        self.clients_lock = threading.Lock()
        self.closed = False
        self.signature = None
        self.request = None
        self.changed_at = 0.0
        self.generation = 0
        self.planned_generation = -1
        self.plan_future = None
        self.plan = []
        self.inflight = {}
        self.failed = set()
        self.modes = {}
        self.cpu = OrderedDict()
        self.cpu_bytes = 0
        self.gpu = OrderedDict()
        self.gpu_bytes = 0
        self.cpu_limit = 256 * 1024 * 1024
        self.gpu_limit = 128 * 1024 * 1024
        self.error = ""
        self.metrics = {"plans": 0, "node_reads": 0, "stale_plans": 0, "cpu_evictions": 0,
                        "gpu_evictions": 0, "last_plan_ms": 0.0, "decode_ms": 0.0}
        for thread in self.threads:
            thread.start()

    def _call(self, request):
        if self.closed:
            raise RuntimeError("Point reader is closed")
        client = getattr(self.local, "client", None)
        if client is None or client.process.poll() is not None:
            with self.clients_lock:
                if self.closed:
                    raise RuntimeError("Point reader is closed")
                client = RustClient(self.binary)
                self.local.client = client
                self.clients.append(client)
        return client.request(request)

    def request_view(self, views, budget, pixel_error, gpu_budget):
        signature = json.dumps([views, budget, pixel_error, gpu_budget], separators=(",", ":"))
        if signature != self.signature:
            self.signature = signature
            self.changed_at = time.monotonic()
            self.generation += 1
            self.request = {"op": "plan", "views": views, "budget": max(0, budget),
                            "pixel_error": pixel_error, "gpu_budget": max(0, gpu_budget)}
            with self.condition:
                for key, _ in self.pending_reads:
                    self.inflight.pop(key, None)
                self.pending_reads.clear()

    def _decode_loop(self):
        while True:
            with self.condition:
                while not self.closed and not self.pending_reads:
                    self.condition.wait()
                if self.closed:
                    return
                key, request = self.pending_reads.pop(0)
                self.active_reads.add(key)
            try:
                result = self._call(request)
                self.completed_reads.put((key, result, None))
            except Exception as exc:
                self.completed_reads.put((key, None, exc))

    def tick(self):
        changed = False
        if self.plan_future is not None and self.plan_future[1].done():
            generation, future = self.plan_future
            self.plan_future = None
            try:
                response, _ = future.result()
                if generation == self.generation:
                    self.plan = response["nodes"]
                    self.failed.clear()
                    self.planned_generation = generation
                    self.metrics["plans"] += 1
                    self.metrics["last_plan_ms"] = response["elapsed_ms"]
                    self.error = ""
                    changed = True
                else:
                    self.metrics["stale_plans"] += 1
            except Exception as exc:
                self.error = str(exc)
                self.planned_generation = generation
        if (self.request is not None and self.plan_future is None
                and self.planned_generation != self.generation
                and time.monotonic() - self.changed_at >= 0.12):
            self.plan_future = (self.generation, self.planner.submit(self._call, self.request))
        while True:
            try:
                key, result, error = self.completed_reads.get_nowait()
            except queue.Empty:
                break
            with self.condition:
                self.inflight.pop(key, None)
                self.active_reads.discard(key)
            try:
                if error is not None:
                    raise error
                response, payload = result
                self.metrics["node_reads"] += 1
                self.metrics["decode_ms"] += response["elapsed_ms"]
                previous = self.cpu.pop(key, None)
                if previous is not None:
                    self.cpu_bytes -= len(previous[1])
                self.cpu[key] = (response, payload)
                self.cpu_bytes += len(payload)
                changed = True
            except Exception as exc:
                self.error = str(exc)
                self.failed.add(key)
                if "Source changed" in self.error:
                    self.generation += 1
        while self.cpu_bytes > self.cpu_limit and self.cpu:
            _, (_, payload) = self.cpu.popitem(last=False)
            self.cpu_bytes -= len(payload)
            self.metrics["cpu_evictions"] += 1
        if self.planned_generation == self.generation:
            with self.condition:
                self.pending_reads.clear()
                for key in list(self.inflight):
                    if key not in self.active_reads:
                        self.inflight.pop(key, None)
                for node in self.plan:
                    key = node_key(node)
                    if key in self.cpu or key in self.inflight or key in self.failed or key in self.gpu:
                        continue
                    request = {"op": "node", "source": node["source"], "stamp": node["stamp"],
                               "cache": node.get("cache", ""),
                               "node": node["node"], "origin": node["origin"],
                               "source_count": node["source_count"],
                               "class_filter": node.get("class_filter", -1)}
                    if node.get("file"):
                        request.update(file=node["file"], source_count=node["source_count"],
                                       node_total_count=node["total_count"],
                                       class_filter=node["class_filter"],
                                       cache_origin=node["cache_origin"], bounds=node["bounds"],
                                       has_rgb=node["has_rgb"])
                    self.pending_reads.append((key, request))
                    self.inflight[key] = True
                self.condition.notify_all()
        return changed

    def attributes(self, key):
        response, payload = self.cpu[key]
        self.cpu.move_to_end(key)
        count = response["count"]
        positions = np.frombuffer(payload, dtype='<f4', count=count*3).reshape(count, 3)
        rgb = np.frombuffer(payload, dtype=np.uint8, count=count*3, offset=count*12).reshape(count, 3)
        colors = np.empty((count, 4), dtype=np.uint8)
        colors[:, :3] = rgb
        colors[:, 3] = 255
        intensity = np.frombuffer(payload, dtype='<u2', count=count, offset=count*15).astype(np.float32)
        intensity /= 65535.0
        classes = np.frombuffer(payload, dtype=np.uint8, count=count, offset=count*17).astype(np.float32)
        return positions, colors, intensity, classes

    def trim_gpu(self, limit=None):
        limit = self.gpu_limit if limit is None else max(0, limit)
        while self.gpu_bytes > limit and self.gpu:
            _, saved = self.gpu.popitem(last=False)
            size = saved[1]
            self.gpu_bytes -= size
            self.metrics["gpu_evictions"] += 1

    def submit_crop(self, request):
        return self.planner.submit(self._call, request)

    def submit_pick(self, request):
        return self.planner.submit(self._call, request)

    def stats(self):
        return dict(self.metrics, cpu_bytes=self.cpu_bytes, gpu_bytes=self.gpu_bytes,
                    cpu_nodes=len(self.cpu), gpu_nodes=len(self.gpu), pending=len(self.inflight),
                    planned_nodes=len(self.plan), planned_points=sum(n["count"] for n in self.plan),
                    max_level=max((n["key"][0] for n in self.plan), default=0), error=self.error)

    def close(self):
        with self.condition:
            self.closed = True
            self.pending_reads.clear()
            self.condition.notify_all()
        self.planner.shutdown(wait=False, cancel_futures=True)
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.close()
        self.cpu.clear()
        self.gpu.clear()
