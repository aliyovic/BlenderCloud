"""Exercise corridor planning and reconciliation through the delivered CLI files.

Authoring gate: these tests protect overlap, bounded section coordinates,
project-wide identity, and explicit conflict delivery. A missing overlap or
source-hash-based ID breaks extraction continuity; silently selecting one
revision loses conflicting evidence. IFC export tests cannot reach planning
before fitted components exist. The tests use public commands without hooks.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

CLI = Path(__file__).resolve().parents[1] / "scripts" / "railway_bim.py"
PROJECT_ID = "fd50975a-e67e-4d99-b1e1-31460b477e58"


def fixture_corridor(root):
    project = root / "project.json"
    project.write_text(json.dumps({
        "schema": "railway-project-contract/1", "project_id": PROJECT_ID,
        "units": "metre", "tracks": [],
        "reference": {"local_origin_m": [2689000., 1254000., 450.],
                      "horizontal": {"status": "UNKNOWN", "crs": "UNKNOWN"},
                      "vertical": {"status": "UNKNOWN", "datum": "UNKNOWN"}},
        "source_sha256": "a" * 64,
    }))
    geometry = root / "corridor.json"
    geometry.write_text(json.dumps({"bounds_local_m": [[0., -20., -2.], [1200., 20., 10.]],
                                    "path_local_m": [[0., 0., 0.], [1200., 0., 0.]]}))
    return project, geometry


def invoke(root, *arguments, success=True):
    result = subprocess.run([sys.executable, str(CLI), *map(str, arguments)], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return result


def make_plan(root):
    project, geometry = fixture_corridor(root)
    plan = root / "plan.json"
    invoke(root, "plan-corridor", project, geometry, plan,
           "--section-length", 400, "--overlap", 40, "--local-limit", 300)
    return project, geometry, plan


@pytest.mark.parametrize("path_kind", ["SUPPLIED_POLYLINE", "BOUNDS_AXIS"])
def test_adjacent_sections_overlap_and_keep_coordinates_within_local_limit(tmp_path, path_kind):
    project, geometry = fixture_corridor(tmp_path)
    if path_kind == "BOUNDS_AXIS":
        value = json.loads(geometry.read_text())
        del value["path_local_m"]
        geometry.write_text(json.dumps(value))
    path = tmp_path / "plan.json"
    invoke(tmp_path, "plan-corridor", project, geometry, path,
           "--section-length", 400, "--overlap", 40, "--local-limit", 300)
    plan = json.loads(path.read_text())
    assert plan["schema"] == "railway-corridor-plan/1"
    assert plan["path_kind"] == path_kind
    assert not Path(plan["source_project"]).is_absolute()
    assert len(plan["segments"]) == 3
    expected = [(0., 420.), (380., 820.), (780., 1200.)]
    for segment, interval in zip(plan["segments"], expected):
        assert segment["observed_route_interval_m"] == list(interval)
        assert max(abs(value) for corner in segment["bounds_local_m"] for value in corner) < 300
        restored = [[value + offset for value, offset in zip(point, segment["local_origin_in_project_m"])]
                    for point in segment["path_local_m"]]
        elevation = 0.0 if path_kind == "SUPPLIED_POLYLINE" else 4.0
        assert restored[0] == [interval[0], 0., elevation]
        assert restored[-1] == [interval[1], 0., elevation]
    for left, right in zip(plan["segments"], plan["segments"][1:]):
        assert left["observed_route_interval_m"][1] - right["observed_route_interval_m"][0] == 40


def test_curved_path_sections_retain_the_supplied_turn_and_project_coordinates(tmp_path):
    project, geometry = fixture_corridor(tmp_path)
    geometry.write_text(json.dumps({"bounds_local_m": [[0., -20., -2.], [640., 640., 10.]],
                                    "path_local_m": [[0., 0., 0.], [600., 0., 0.], [600., 600., 0.]]}))
    output = tmp_path / "curved.json"
    invoke(tmp_path, "plan-corridor", project, geometry, output,
           "--section-length", 400, "--overlap", 40, "--local-limit", 300)
    plan = json.loads(output.read_text())
    assert plan["route_length_m"] == 1200
    middle = plan["segments"][1]
    restored = [[value + offset for value, offset in zip(point, middle["local_origin_in_project_m"])]
                for point in middle["path_local_m"]]
    for actual, expected in zip(restored, [[380., 0., 0.], [600., 0., 0.], [600., 220., 0.]]):
        assert actual == pytest.approx(expected, abs=1e-9)


def test_short_terminal_section_retains_the_requested_overlap(tmp_path):
    project, geometry = fixture_corridor(tmp_path)
    geometry.write_text(json.dumps({"bounds_local_m": [[0., -20., -2.], [401., 20., 10.]],
                                    "path_local_m": [[0., 0., 0.], [401., 0., 0.]]}))
    output = tmp_path / "short-terminal.json"
    invoke(tmp_path, "plan-corridor", project, geometry, output,
           "--section-length", 400, "--overlap", 40, "--local-limit", 300)
    segments = json.loads(output.read_text())["segments"]
    assert len(segments) == 2
    assert segments[0]["observed_route_interval_m"][1] - segments[1]["observed_route_interval_m"][0] == 40


def test_segment_identity_survives_a_new_scan_observation(tmp_path):
    project, geometry, first = make_plan(tmp_path)
    value = json.loads(project.read_text())
    value["source_sha256"] = "b" * 64
    project.write_text(json.dumps(value))
    second = tmp_path / "new-observation.json"
    invoke(tmp_path, "plan-corridor", project, geometry, second,
           "--section-length", 400, "--overlap", 40, "--local-limit", 300)
    previous, current = [json.loads(path.read_text()) for path in (first, second)]
    assert [segment["segment_id"] for segment in previous["segments"]] == [segment["segment_id"] for segment in current["segments"]]


@pytest.mark.parametrize("failure", ["transverse_extent", "overlap"])
def test_invalid_sections_do_not_publish_a_plan(tmp_path, failure):
    project, geometry = fixture_corridor(tmp_path)
    if failure == "transverse_extent":
        value = json.loads(geometry.read_text())
        value["bounds_local_m"][0][1] = -400
        value["bounds_local_m"][1][1] = 400
        geometry.write_text(json.dumps(value))
    output = tmp_path / "invalid.json"
    result = invoke(tmp_path, "plan-corridor", project, geometry, output,
                    "--section-length", 400, "--overlap", 400 if failure == "overlap" else 40,
                    "--local-limit", 300, success=False)
    assert "overlap" in result.stderr.lower() if failure == "overlap" else "local" in result.stderr.lower()
    assert not output.exists()


def test_reconciliation_preserves_project_asset_ids_and_reports_conflicting_revisions(tmp_path):
    project, geometry, plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text())
    observations = []
    for number, segment in enumerate(plan["segments"][:2]):
        path = tmp_path / f"observation-{number}.json"
        value = {"schema": "railway-segment-observation/1", "project_id": PROJECT_ID,
                 "segment_id": segment["segment_id"], "source_sha256": str(number + 1) * 64,
                 "parameter_frame": "project-local", "units": "metre",
                 "assets": [{"asset_id": "mast-a", "revision": 1, "parameters": {"height_m": 10.0}}]}
        path.write_text(json.dumps(value))
        observations.append(path)
    clean = tmp_path / "reconciled.json"
    invoke(tmp_path, "reconcile-corridor", plan_path, clean, *observations)
    value = json.loads(clean.read_text())
    assert value["status"] == "reconciled"
    assert [asset["asset_id"] for asset in value["assets"]] == ["mast-a"]
    assert len(value["assets"][0]["observations"]) == 2
    changed = json.loads(observations[1].read_text())
    changed["assets"][0].update(revision=2, parameters={"height_m": 12.0})
    observations[1].write_text(json.dumps(changed))
    rejected = tmp_path / "conflicts.json"
    invoke(tmp_path, "reconcile-corridor", plan_path, rejected, *observations, success=False)
    value = json.loads(rejected.read_text())
    assert value["status"] == "conflict"
    assert value["assets"] == []
    assert value["conflicts"][0]["asset_id"] == "mast-a"
    assert set(value["conflicts"][0]["reasons"]) == {"revision", "parameters"}
    assert {item["revision"] for item in value["conflicts"][0]["observations"]} == {1, 2}
