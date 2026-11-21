import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_behavior.core import digest
from gamecraft_training.package.playtests import replay_candidate
from gamecraft_training.package import models


class RSIPlaytestTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GAMECRAFT_TEST_GODOT'), 'Set GAMECRAFT_TEST_GODOT for engine smoke test')
    def test_real_engine_candidate_policy_handles_changed_input_window(self):
        from gamecraft_behavior.runtime import rollout
        sys.path.insert(0, str(Path(__file__).parent/'fixtures'))
        from counter_adapter import CounterAdapter
        class Adapter(CounterAdapter):
            def policy_observation(self, status):
                return {**super().policy_observation(status), 'input_ready': status['ticks'] >= 12}
        adapter = Adapter()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root/'candidate'
            shutil.copytree(Path(__file__).parent/'fixtures/counter_game', project)
            code = project/'main.gd'
            code.write_text(code.read_text().replace('if finished or not event is InputEventKey',
                            'if ticks < 12 or finished or not event is InputEventKey'))
            actions = iter(['jab', 'wait', 'wait'])
            stale = rollout(project, root/'stale', '',
                lambda *a, **k: {'action': next(actions), 'reason': 'old input'},
                godot=os.environ['GAMECRAFT_TEST_GODOT'], adapter=adapter,
                scenario='counter', step_frames=6, max_decisions=3, player_state='engine')
            def author(*args, **kwargs):
                payload = json.loads(args[2][0]['text'])
                self.assertNotIn('behavior_skill', payload)
                self.assertIn('ticks < 12', payload['game_source']['main.gd'])
                return {'source': 'def act(state, recent):\n'
                        '    if state.get("input_ready"):\n'
                        '        return {"action": "jab", "reason": "window open"}\n'
                        '    return {"action": "wait", "reason": "window closed"}'}
            fake_llm = SimpleNamespace(call=author, img=lambda *a, **k: {'type': 'input_image'})
            with patch.dict(sys.modules, {'llm': fake_llm}), patch.object(fake_llm, 'call', side_effect=author) as call:
                fresh = replay_candidate(project, [stale], root/'fresh', adapter=adapter,
                    diagnosis={'problem': 'new input window'}, changes={'delay_frames': 12},
                    rollout=rollout, godot=os.environ['GAMECRAFT_TEST_GODOT'], model='test',
                    backend='codex', step_frames=6, max_decisions=3, player_state='engine')
            self.assertEqual(call.call_count, 1)
            self.assertEqual(stale['replay']['points'], 0)
            self.assertEqual(fresh[0]['replay']['points'], 4)
            self.assertEqual([d['action'] for d in fresh[0]['decisions']], ['wait', 'jab', 'jab'])
            # Observe readiness, not a presumed offset between engine ticks and
            # trace frames (the engine has startup ticks before the first input).
            for decision in fresh[0]['decisions']:
                self.assertEqual(decision['action'] == 'jab', decision['player_state']['input_ready'])
            self.assertTrue(Path(fresh[0]['video']).is_file())

    def test_pixel_only_rsi_fails_before_starting_an_experiment(self):
        from gamecraft_training.package.loop import run
        with self.assertRaisesRegex(ValueError, 'acceptance policies require engine'):
            run('unused', 'unused', godot='unused', asset_python='unused', player_state='pixels')

    def exercise(self, root, *, bad=None):
        project = root/'game'
        project.mkdir()
        (project/'rules.gd').write_text('input_ready_after = 12')
        calls = []
        def author(candidate, output, **kwargs):
            calls.append(kwargs)
            output.write_text('def act(state, recent):\n    return {}\n')
            def act(*args, **kw):
                return {'action': 'jab' if kw['current_state']['frame'] >= 12 else 'wait'}
            return act
        def rollout(candidate, output, skill, policy, **kwargs):
            self.assertEqual(skill, '')
            self.assertEqual(kwargs['max_decisions'], 4)
            output.mkdir()
            actions = [policy(current_state={'frame': t})['action'] for t in (0, 6, 12, 18)]
            self.assertEqual(actions, ['wait', 'wait', 'jab', 'jab'])
            for name in ('trace.json', 'video.mp4'):
                (output/name).write_text('fixture')
            result = {'scenario': kwargs['scenario'], 'normal_launch': kwargs['normal_launch'],
                      'project_sha256': digest(candidate), 'replay_verified': True,
                      'decisions': [{'action': a} for a in actions],
                      'trace': str(output/'trace.json'), 'video': str(output/'video.mp4')}
            if bad == 'unverified': result['replay_verified'] = False
            if bad == 'wrong_project': result['project_sha256'] = {}
            if bad == 'missing_video': result['video'] = str(output/'absent.mp4')
            if bad == 'mutated': (candidate/'rules.gd').write_text('changed')
            return result
        old = [{'scenario': 'combat', 'normal_launch': True, 'decision_frames': 6,
                'decisions': [{'action': 'jab'}]}]
        runs = replay_candidate(project, old, root/'acceptance', adapter=SimpleNamespace(),
            diagnosis={'problem': 'delayed contact'}, changes={'delay': 12},
            rollout=rollout, godot='unused', model='test-model', backend='codex',
            step_frames=6, max_decisions=4, player_state='engine', policy_factory=author)
        return runs, calls, old

    def test_fresh_policy_reacts_instead_of_replaying_short_old_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs, calls, old = self.exercise(root)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['playtest_context']['candidate_changes'], {'delay': 12})
            self.assertEqual(len(runs[0]['decisions']), 4)
            self.assertFalse(runs[0]['playtest']['uses_behavior_skill'])
            self.assertEqual(old[0]['decisions'], [{'action': 'jab'}])
            manifest = json.loads((root/'acceptance/result.json').read_text())
            self.assertEqual(manifest['episodes'][0]['policy'], runs[0]['player_policy'])

    def test_invalid_candidate_evidence_is_not_eligible(self):
        for bad in ('unverified', 'wrong_project', 'missing_video', 'mutated'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(RuntimeError):
                    self.exercise(Path(temp), bad=bad)

    def test_selection_uses_independent_evidence_and_discloses_policy_difference(self):
        left = [{'scenario': 'combat', 'decisions': [{}]}]
        right = [{'scenario': 'combat', 'decisions': [{}, {}, {}],
                  'playtest': {'purpose': 'candidate_acceptance'}}]
        answer = {'preferred': 'B', 'confidence': 'high', 'evidence_valid': True,
                  'critical_regression': False, 'reason': 'visible repair', 'issue_assessments': []}
        with patch.object(models, '_independent_repair_content', return_value=[]) as evidence, \
             patch.object(models, '_paired_repair_content', side_effect=AssertionError('old alignment')), \
             patch.object(models, 'judge_call', return_value=answer) as judge:
            models.selection({'_profile': {'name': 'rsi'}, 'quality': {'criteria': []}},
                             SimpleNamespace(instructions='rules'), {}, left, right, max_reviews=1)
        evidence.assert_called_once()
        self.assertIn('do not use the learned Behavior Skill', judge.call_args.args[0])
        self.assertIn('version_outcomes', judge.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
