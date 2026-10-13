# Scan worker protocol

`scan-cache serve` accepts one JSON request per line on stdin. Each response is a JSON line followed by exactly `payload_bytes` binary bytes on stdout. Requests run sequentially within one worker. Errors use `ok: false`, an error message, and an empty payload.

Protocol version 2 retains the `plan`, `node`, `crop`, and `pick` operations. Node requests add `point_offset`, default zero, and `max_payload_bytes`, default 64 MiB. Every operation accepts `working_budget_bytes`, default 256 MiB and limited to 1 MiB through 1 GiB. Rust uses operation-specific deserialization types.

A node response contains `count`, `total_count`, `point_offset`, and `next_offset`. The total is the complete filtered node count. `next_offset` is the next requested point offset, or null after the last page. A page is at most 64 MiB and may be smaller to fit the requested working budget. A request that cannot fit one record returns an explicit error.

| Column | Encoding | Start offset | Bytes |
|---|---|---|---|
| Positions | Three little-endian `f32` values in local metres | 0 | `count * 12` |
| RGB | Three `u8` values | `count * 12` | `count * 3` |
| Intensity | One little-endian `u16` value | `count * 15` | `count * 2` |
| Classification | One `u8` value | `count * 17` | `count` |

Append pages to each column separately. Concatenating complete page payloads does not produce a valid complete-node payload. The Blender client returns the assembled count and columns to existing rendering callers. Preview and rendering intensity values divide the raw integer by 65535.

A source stamp and cache manifest generation identify the observation read by an operation. Native requests validate their node metadata against that manifest. Changed observations or inconsistent counts return a structured error without indexing outside allocated buffers. Invalid JSON or operation requests also return structured errors so the next valid request can run.

The Python client closes the worker after invalid framing, incomplete payloads, or a transport deadline. The runtime replaces closed workers on the next request. Valid operation errors retain the worker. Python supports the older single-page response for existing installations, but complete large-node transfers require version 2 on both sides.

`tests/test_worker_protocol.py` owns worker storage, error, source-freshness, paging, and picking contracts. `tests/test_transport.py` owns pipe recovery, deadlines, shared reservations, complete SoA assembly, and the actual 64 MiB boundary across Rust and Python.
