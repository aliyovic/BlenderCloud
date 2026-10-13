"""Rust worker transport and caches used by the Blender extension."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import select
import shutil
import subprocess
import tempfile
import threading
import time

import numpy as np

MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_NODE_BYTES = 128 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024 * 1024
POINT_BYTES = 18
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_PENDING_TOOLS = 2


class ProtocolError(RuntimeError):
    """The worker cannot safely continue this conversation."""


class WorkingBudget:
    """Reserve decoder and assembly memory across worker processes."""

    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.peak = 0
        self.condition = threading.Condition()

    def acquire(self, size, deadline, closed):
        if size > self.limit:
            raise RuntimeError("Point request exceeds the shared working-memory budget. Repartition the source.")
        with self.condition:
            while self.used + size > self.limit:
                if closed():
                    raise ProtocolError("Rust point reader stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Point request timed out waiting for working memory")
                self.condition.wait(min(remaining, 0.1))
            self.used += size
            self.peak = max(self.peak, self.used)

    def release(self, size):
        with self.condition:
            self.used -= size
            self.condition.notify_all()


class ReplyReservation:
    """Keep completed point buffers charged until Blender consumes them."""

    def __init__(self, budget, size):
        self.budget = budget
        self.size = size
        self.lock = threading.Lock()

    def release(self):
        with self.lock:
            if self.size:
                self.budget.release(self.size)
                self.size = 0

    def __del__(self):
        self.release()


def node_key(node):
    return (node["source"], node.get("cache", ""), node["stamp"], node["node"],
            tuple(node["origin"]), node.get("class_filter", -1))


class RustClient:
    def __init__(self, binary, *, timeout=REQUEST_TIMEOUT_SECONDS, working_budget=None):
        self.process = subprocess.Popen([str(binary), "serve"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.timeout = timeout
        self.working_budget = working_budget
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.closed = False

    def _wait(self, stream, deadline, *, writing=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Rust point request timed out")
        ready = select.select([] if writing else [stream], [stream] if writing else [], [], remaining)
        if not ready[1 if writing else 0]:
            raise TimeoutError("Rust point request timed out")

    def _receive(self, deadline):
        self._wait(self.process.stdout, deadline)
        block = os.read(self.process.stdout.fileno(), 65536)
        if not block:
            raise ProtocolError("Rust point reader stopped before completing its response")
        self.buffer.extend(block)

    def _packet(self, message, deadline):
        wire = memoryview(json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n")
        while wire:
            self._wait(self.process.stdin, deadline, writing=True)
            try:
                sent = os.write(self.process.stdin.fileno(), wire[:65536])
            except BlockingIOError:
                continue
            wire = wire[sent:]
        while b"\n" not in self.buffer:
            if len(self.buffer) > MAX_HEADER_BYTES:
                raise ProtocolError("Rust response header exceeds the protocol limit")
            self._receive(deadline)
        end = self.buffer.index(b"\n")
        if end > MAX_HEADER_BYTES:
            raise ProtocolError("Rust response header exceeds the protocol limit")
        response = json.loads(self.buffer[:end])
        del self.buffer[:end + 1]
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ProtocolError("Invalid Rust response header")
        size = response.get("payload_bytes", 0)
        if type(size) is not int or size < 0 or size > MAX_FRAME_BYTES:
            raise ProtocolError("Invalid point buffer size from Rust")
        if response.get("protocol_version") not in (None, 1, 2):
            raise ProtocolError("Unsupported Rust point protocol version")
        if message.get("op") == "node" and response["ok"]:
            count = response.get("count")
            total = response.get("total_count", count)
            declared = message.get("source_count", 0)
            if (type(count) is not int or type(total) is not int or not 0 <= count <= total
                    or size != count * POINT_BYTES or total * POINT_BYTES > MAX_NODE_BYTES
                    or type(declared) is not int or declared < 0
                    or declared > 0 and total > declared):
                raise ProtocolError("Invalid or stale point count in Rust response")
        elif size:
            raise ProtocolError("Unexpected point payload for this Rust operation")
        payload = bytearray(size)
        offset = 0
        while offset < size:
            if not self.buffer:
                self._receive(deadline)
            count = min(size - offset, len(self.buffer))
            payload[offset:offset + count] = self.buffer[:count]
            del self.buffer[:count]
            offset += count
        if not response["ok"]:
            raise RuntimeError(response.get("error", "Rust point reader failed"))
        return response, payload

    def _node(self, message, deadline):
        response, payload = self._packet(message, deadline)
        count = response.get("count")
        total = response.get("total_count", count)
        if (type(count) is not int or type(total) is not int or not 0 <= count <= total
                or len(payload) != count * POINT_BYTES):
            raise ProtocolError("Invalid point count in Rust response")
        if total * POINT_BYTES > MAX_NODE_BYTES:
            raise ProtocolError("Node exceeds the assembly memory budget. Repartition the source.")
        if "point_offset" in message:
            return response, payload
        if response.get("point_offset", 0) != 0:
            raise ProtocolError("Rust returned the wrong point page")
        if response.get("next_offset") is None:
            if total != count:
                raise ProtocolError("Rust omitted points from the node")
            return response, payload

        combined = bytearray(total * POINT_BYTES)
        destination = memoryview(combined)
        first = response
        seen = 0
        while True:
            count = response.get("count")
            if (type(count) is not int or count <= 0 or seen + count > total
                    or response.get("point_offset") != seen or response.get("total_count") != total
                    or response.get("stamp") != first.get("stamp") or len(payload) != count * POINT_BYTES):
                raise ProtocolError("Inconsistent Rust point pages")
            page = memoryview(payload)
            for width, preceding in ((12, 0), (3, 12), (2, 15), (1, 17)):
                start = total * preceding + seen * width
                destination[start:start + count * width] = page[count * preceding:count * (preceding + width)]
            page.release()
            seen += count
            following = response.get("next_offset")
            if following is None:
                if seen != total:
                    raise ProtocolError("Rust omitted points from the node")
                break
            if type(following) is not int or following != seen or seen >= total:
                raise ProtocolError("Invalid Rust point continuation")
            response, payload = self._packet(dict(message, point_offset=following), deadline)
        result = dict(first, count=total, payload_bytes=len(combined), point_offset=0, next_offset=None)
        return result, combined

    def request(self, message, *, retain_result=False, deadline=None):
        with self.lock:
            timeout_deadline = time.monotonic() + self.timeout
            deadline = timeout_deadline if deadline is None else min(deadline, timeout_deadline)
            node = message.get("op") == "node"
            reservation = 0
            retained = 0
            wire = dict(message)
            try:
                if self.working_budget is not None:
                    decoder_bytes = wire.setdefault("working_budget_bytes", 64 * 1024 * 1024)
                    if type(decoder_bytes) is not int or decoder_bytes <= 0:
                        raise ValueError("Working memory must be a positive byte count")
                    declared = wire.get("source_count", 0)
                    if type(declared) is not int or declared < 0:
                        raise ValueError("Point count must be a nonnegative integer")
                    node_bytes = (declared * POINT_BYTES if declared else MAX_NODE_BYTES) if node else 0
                    if type(node_bytes) is not int or not 0 <= node_bytes <= MAX_NODE_BYTES:
                        raise ValueError("Node exceeds the assembly memory budget. Repartition the source.")
                    charge = decoder_bytes + node_bytes * 2
                    self.working_budget.acquire(charge, deadline, lambda: self.closed)
                    reservation = charge
                if node:
                    wire.setdefault("max_payload_bytes", MAX_FRAME_BYTES)
                    response, payload = self._node(wire, deadline)
                else:
                    response, payload = self._packet(wire, deadline)
                if retain_result:
                    retained = len(payload) if self.working_budget is not None else 0
                    return response, payload, ReplyReservation(self.working_budget, retained)
                return response, payload
            except (ProtocolError, TimeoutError, OSError, ValueError) as exc:
                self.close()
                raise RuntimeError(str(exc)) from exc
            finally:
                if reservation:
                    self.working_budget.release(reservation - retained)

    def close(self):
        self.closed = True
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
        self.tools = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-tool")
        self.tool_slots = threading.BoundedSemaphore(MAX_PENDING_TOOLS)
        self.worker_count = 6
        self.working_budget = WorkingBudget(512 * 1024 * 1024)
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

    def _call(self, request, *, retain_result=False, deadline=None):
        if self.closed:
            raise RuntimeError("Point reader is closed")
        client = getattr(self.local, "client", None)
        if client is None or client.process.poll() is not None:
            with self.clients_lock:
                if self.closed:
                    raise RuntimeError("Point reader is closed")
                client = RustClient(self.binary, working_budget=self.working_budget)
                self.local.client = client
                self.clients = [saved for saved in self.clients if saved.process.poll() is None]
                self.clients.append(client)
        return client.request(request, retain_result=retain_result, deadline=deadline)

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
                result = self._call(request, retain_result=True)
                with self.condition:
                    if self.closed:
                        result[2].release()
                        return
                    self.completed_reads.put((key, result, None))
                result = None
            except Exception as exc:
                self.completed_reads.put((key, None, RuntimeError(str(exc))))

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
            lease = None
            try:
                if error is not None:
                    raise error
                response, payload, lease = result
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
            finally:
                if lease is not None:
                    lease.release()
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

    def _crop(self, request, *, deadline=None):
        try:
            output = Path(request["output"])
            if output.exists() or output.is_symlink():
                raise RuntimeError("Crop output already exists")
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".crop-", dir=output.parent, ignore_cleanup_errors=True) as staging:
                temporary = Path(staging) / "output"
                created_output = False
                try:
                    response, payload = self._call(dict(request, output=str(temporary)), deadline=deadline)
                    try:
                        output.mkdir()
                    except FileExistsError as exc:
                        raise RuntimeError("Crop output already exists") from exc
                    created_output = True
                    for path in sorted(temporary.iterdir(), key=lambda path: path.name == "manifest.json"):
                        path.rename(output / path.name)
                    return dict(response, output=str(output)), payload
                except Exception:
                    client = getattr(self.local, "client", None)
                    if client is not None:
                        client.close()
                    if created_output:
                        shutil.rmtree(output, ignore_errors=True)
                    raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _submit_tool(self, request, operation):
        with self.condition:
            if self.closed:
                raise RuntimeError("Point reader is closed")
            if not self.tool_slots.acquire(blocking=False):
                raise RuntimeError("Point reader is busy. Wait for the current crop or pick to finish.")
            deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
            try:
                future = self.tools.submit(operation, request, deadline=deadline)
            except Exception:
                self.tool_slots.release()
                raise
            future.add_done_callback(lambda _future: self.tool_slots.release())
            return future

    def submit_crop(self, request):
        return self._submit_tool(request, self._crop)

    def submit_pick(self, request):
        return self._submit_tool(request, self._call)

    def stats(self):
        return dict(self.metrics, cpu_bytes=self.cpu_bytes, gpu_bytes=self.gpu_bytes,
                    working_bytes=self.working_budget.used, working_peak_bytes=self.working_budget.peak,
                    working_limit_bytes=self.working_budget.limit,
                    cpu_nodes=len(self.cpu), gpu_nodes=len(self.gpu), pending=len(self.inflight),
                    planned_nodes=len(self.plan), planned_points=sum(n["count"] for n in self.plan),
                    max_level=max((n["key"][0] for n in self.plan), default=0), error=self.error)

    def close(self):
        with self.condition:
            self.closed = True
            self.pending_reads.clear()
            self.condition.notify_all()
        self.planner.shutdown(wait=False, cancel_futures=True)
        self.tools.shutdown(wait=False, cancel_futures=True)
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.close()
        for thread in self.threads:
            thread.join(timeout=1)
        while True:
            try:
                _, result, _ = self.completed_reads.get_nowait()
            except queue.Empty:
                break
            if result is not None:
                result[2].release()
        self.cpu.clear()
        self.gpu.clear()
