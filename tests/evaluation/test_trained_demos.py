"""Focused tests for post-RSI demo provenance and clean packaging."""
import json
from pathlib import Path
import tempfile
import unittest

from gamecraft_behavior.core import digest
from gamecraft_evaluation.trained_demos import key_outcome_matches, package, public_trace, select_cases


class TrainedDemosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        (self.run / "initial").mkdir(parents=True)
        (self.run / "initial/scenarios.json").write_text(json.dumps([
            {"id": "title", "launch": "default"},
            {"id": "core", "launch": "scenario"},
        ]))
        self.project = self.root / "source"
        (self.project / "demo_outputs").mkdir(parents=True)
        (self.project / "project.godot").write_text('[application]\n')
        (self.project / "game_plan.json").write_text('{"task_id":"example"}')
        (self.project / "demo_outputs/title.json").write_text(json.dumps({
            "duration_frames": 30, "events": [{"frame": 1, "type": "wait"}],
        }))
        self.episode = self.root / "episode"
        self.episode.mkdir()
        (self.episode / "trace.json").write_text(json.dumps({
            "scenario": "title", "normal_launch": True, "duration_frames": 60,
            "events": [{"frame": 0, "type": "key_press", "keycode": "SPACE"}],
        }))
        (self.episode / "video.mp4").write_bytes(b"preview")
        (self.episode / "checked.json").write_text('{"key_outcome_matched":true}')
        self.record = {
            "trace": str(self.episode / "trace.json"),
            "video": str(self.episode / "video.mp4"),
            "replay": {"state": "playing", "score": 1},
            "replay_protocol": "physics_frames_v1",
            "replay_verified": True, "scenario": "title", "normal_launch": True,
            "os_input_check": {"comparison": str(self.episode / "checked.json"),
                               "video": "checked.mp4"},
            "project_sha256": digest(self.project),
        }

    def test_declared_scenarios_are_ordered_and_unique(self):
        self.assertEqual([c["id"] for c in select_cases(self.run, ["core", "title"])],
                         ["core", "title"])
        with self.assertRaisesRegex(ValueError, "distinct"):
            select_cases(self.run, ["core", "core"])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            select_cases(self.run, ["unknown"])

    def test_normal_launch_trace_does_not_inject_scenario(self):
        self.assertNotIn("scenario", public_trace(self.record))
        self.record["normal_launch"] = False
        self.assertEqual(public_trace(self.record)["scenario"], "title")

    def test_public_trace_converts_training_drag_to_portable_os_events(self):
        (self.episode / "trace.json").write_text(json.dumps({
            "duration_frames": 30, "events": [
                {"frame": 2, "type": "mouse_drag", "button": "left",
                 "x": 0.1, "y": 0.2, "to_x": 0.7, "to_y": 0.8}]}))
        events = public_trace(self.record)["events"]
        self.assertEqual([event["type"] for event in events],
                         ["mouse_down", "mouse_move", "mouse_up"])
        self.assertTrue(all(event["frame"] == 2 for event in events))

    def test_package_preserves_original_and_uses_real_episode(self):
        cases = select_cases(self.run, ["title"])
        out = self.root / "out"
        out.mkdir()
        target = package(self.project, out, cases, [self.record],
                         [self.project / "demo_outputs/title.json"],
                         project_hash=digest(self.project), skill_hash="skill-hash",
                         policy_hash=None)
        names = sorted(p.name for p in (target / "demo_outputs").glob("*.json"))
        self.assertEqual(names, ["00_original_title.json", "01_agent_title.json"])
        self.assertNotIn("scenario", json.loads((target / "demo_outputs/01_agent_title.json").read_text()))
        self.assertTrue((self.project / "demo_outputs/title.json").is_file())
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["verification"]["official_build_and_replay"], "pending")
        self.assertEqual([d["kind"] for d in manifest["demos"]], ["original", "agent"])

    def test_key_outcome_parity_is_not_duration_exactness(self):
        expected = {"state": "dead", "score": 2, "survival_seconds": 4.2}
        self.assertTrue(key_outcome_matches(expected, {"state": "dead", "score": 2,
                                                       "survival_seconds": 4.3}))
        self.assertFalse(key_outcome_matches(expected, {"state": "dead", "score": 1,
                                                        "survival_seconds": 4.2}))
        self.assertFalse(key_outcome_matches(expected, {"state": "playing", "score": 2,
                                                        "survival_seconds": 4.2}))

    def test_stale_or_unverified_episode_is_rejected(self):
        cases = select_cases(self.run, ["title"])
        self.record["replay_verified"] = False
        with self.assertRaisesRegex(ValueError, "provenance/replay"):
            package(self.project, self.root / "out", cases, [self.record], [],
                    project_hash=digest(self.project), skill_hash="x", policy_hash=None)

    def test_native_episode_requires_matching_os_input_outcome(self):
        cases = select_cases(self.run, ["title"])
        (self.root / "out").mkdir()
        self.record.update(capture_mode="native", replay_verified=False,
                           replay_protocol="native_os_input_v1")
        (self.episode / "checked.json").write_text('{"key_outcome_matched":false}')
        with self.assertRaisesRegex(ValueError, "OS-input outcome"):
            package(self.project, self.root / "out", cases, [self.record], [],
                    project_hash=digest(self.project), skill_hash="x", policy_hash="policy-hash")


if __name__ == "__main__":
    unittest.main()
