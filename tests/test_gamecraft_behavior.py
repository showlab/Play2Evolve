import json
import tempfile
import unittest
from pathlib import Path
from gamecraft_behavior.core import action, gain, prepare, trace, train, digest


class BehaviorTests(unittest.TestCase):
    def result(self, score=0, seconds=1, verified=True):
        return {'replay': {'score': score, 'survival_seconds': seconds, 'state': 'dead'},
                'replay_verified': verified, 'decisions': []}

    def test_action_allowlist(self):
        for invalid in [{'action': 'shell', 'reason': 'x'}, {'action': 'tap'}, {'action': 'wait', 'reason': 3}]:
            with self.assertRaises(ValueError): action(invalid)
        self.assertEqual(action({'action': 'tap', 'reason': 'falling'})['action'], 'tap')

    def test_trace_uses_game_frames_not_model_latency(self):
        t = trace([{'frame': 0, 'action': 'tap', 'reason': ''},
                   {'frame': 6, 'action': 'wait', 'reason': ''},
                   {'frame': 12, 'action': 'tap', 'reason': ''}], 18)
        self.assertEqual([e['frame'] for e in t['events']], [0, 12])
        self.assertEqual(t['scenario'], 'ready')
        with self.assertRaises(ValueError): trace([{'frame': 18, 'action': 'tap', 'reason': ''}], 18)

    def test_gate_requires_replay_and_no_score_regression(self):
        self.assertTrue(gain(self.result(), self.result(seconds=2)))
        self.assertFalse(gain(self.result(), self.result(seconds=2, verified=False)))
        self.assertFalse(gain(self.result(score=1), self.result(seconds=10)))
        self.assertFalse(gain(self.result(), self.result(seconds=1.1)))
        self.assertFalse(gain({**self.result(), 'seed': 73}, {**self.result(seconds=3), 'seed': 74}))
        self.assertFalse(gain({**self.result(), 'project_sha256': {'game': 'old'}},
                              {**self.result(seconds=3), 'project_sha256': {'game': 'changed'}}))

    def test_adapter_changes_only_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / 'game'; p.mkdir()
            (p / 'project.godot').write_text('config_version=5\n')
            (p / 'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            before = digest(p)
            copy = prepare(p, root / 'copy')
            self.assertEqual(digest(p), before)
            self.assertTrue((copy / 'behavior_bridge.gd').exists())
            self.assertIn('[autoload]', (copy / 'project.godot').read_text())
            with self.assertRaises(ValueError): prepare(p, p / 'nested')

    def test_noisy_improvement_is_rejected(self):
        values = iter([self.result(seconds=1), self.result(seconds=3), self.result(seconds=2), self.result(seconds=1)])
        with tempfile.TemporaryDirectory() as td:
            r = train('old', Path(td) / 'run', lambda *_: next(values), lambda *_: {'skill': 'new', 'reason': 'x'})
            self.assertEqual(r['skill'], 'old')
            self.assertFalse(r['history'][0]['accepted'])

    def test_confirmed_improvement_is_accepted(self):
        values = iter([self.result(seconds=1), self.result(seconds=3), self.result(seconds=1), self.result(seconds=3)])
        with tempfile.TemporaryDirectory() as td:
            r = train('old', Path(td) / 'run', lambda *_: next(values), lambda *_: {'skill': 'new', 'reason': 'x'})
            self.assertEqual(r['skill'], 'new')
            self.assertTrue(r['history'][0]['accepted'])

    def test_unverified_baseline_cannot_train(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                train('', Path(td) / 'run', lambda *_: self.result(verified=False), None)


if __name__ == '__main__':
    unittest.main()
