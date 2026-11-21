import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter
from gamecraft_behavior.program_policy import compile_policy, program_player


class ProgramPlayerTests(unittest.TestCase):
    def test_only_bounded_reactive_policy_is_executable(self):
        source = '''def act(state, recent):
    if state.get("phase") == "ready":
        return {"action": "tap", "reason": "start"}
    if state.get("bird_center_y") > state.get("next_gap_center_y"):
        return {"action": "tap", "reason": "rise toward gap"}
    return {"action": "wait", "reason": "hold course"}
'''
        act = compile_policy(source)
        self.assertEqual(act({'phase': 'ready'}, [])['action'], 'tap')
        self.assertEqual(act({'phase': 'playing', 'bird_center_y': 400,
                              'next_gap_center_y': 300}, [])['action'], 'tap')
        for invalid in ('import os\ndef act(state, recent): return {}',
                        'def act(state, recent):\n    while True: pass',
                        'def act(state, recent):\n    return state.__class__',
                        'def act(state, recent):\n    state["score"] = 42'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compile_policy(invalid)

    def test_agent_authors_once_and_policy_reacts_to_new_state(self):
        source = '''def act(state, recent):
    if state.get("phase") == "ready":
        return {"action": "tap", "reason": "start"}
    if state.get("bird_center_y") > state.get("next_gap_center_y"):
        return {"action": "tap", "reason": "below gap"}
    return {"action": "wait", "reason": "within gap"}
'''
        calls = []
        def call(*args, **kwargs):
            calls.append((args, kwargs))
            return {'source': source}
        fake_llm = types.SimpleNamespace(call=call, img=lambda *args, **kwargs: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {'llm': fake_llm}):
            root = Path(temp)
            project = root / 'project'
            (project / 'scripts').mkdir(parents=True)
            (project / 'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            (project / 'scripts/main.gd').write_text('extends Node\n')
            policy = root / 'round-001/train/policy.py'
            decide = program_player(project, policy, adapter=DreadWingsAdapter())
            ready = {'phase': 'ready', 'bird_center_y': 300, 'next_gap_center_y': 320}
            self.assertEqual(decide('frame.png', 'Flap toward the gap.', [], 6,
                                    current_state=ready)['action'], 'tap')
            playing = {'phase': 'playing', 'bird_center_y': 400, 'next_gap_center_y': 320}
            self.assertEqual(decide('frame.png', 'Flap toward the gap.', [], 6,
                                    current_state=playing)['action'], 'tap')
            self.assertEqual(decide('frame.png', 'Flap toward the gap.', [], 6,
                                    current_state={**playing, 'bird_center_y': 300})['action'], 'wait')
            self.assertEqual(len(calls), 1)
            self.assertEqual(policy.read_text().strip(), source.strip())
            self.assertEqual(json.loads(policy.with_suffix('.json').read_text())['skill'],
                             'Flap toward the gap.')

    def test_bird_program_state_exposes_visible_pipe_geometry(self):
        state = DreadWingsAdapter().policy_observation({
            'state': 'playing', 'score': 2, 'bird_y': 280, 'velocity': 10,
            'first_pipe_x': 450, 'first_gap_center': 320, 'speed': 190})
        self.assertEqual(state['next_pipe_x'], 450)
        self.assertEqual(state['next_gap_center_y'], 320)
        self.assertNotIn('seed', state)

    def test_acceptance_policy_uses_new_code_and_goals_without_skill(self):
        calls = []
        def call(*args, **kwargs):
            calls.append(args)
            return {'source': 'def act(state, recent):\n    return {"action": "wait", "reason": "observe"}'}
        fake = types.SimpleNamespace(call=call, img=lambda *a, **k: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(temp)
            (root/'scripts').mkdir()
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            (root/'scripts/main.gd').write_text('const NEW_DELAY = 12\n')
            context = {'goal': 'show delayed input', 'candidate_changes': {'delay': 12}}
            decide = program_player(root, root/'policy.py', adapter=DreadWingsAdapter(),
                                    playtest_context=context)
            decide('frame.png', 'MUST_NOT_REACH_AUTHOR', [], 6, current_state={'phase': 'ready'})
            sent = json.loads(calls[0][2][0]['text'])
            self.assertNotIn('behavior_skill', sent)
            self.assertNotIn('MUST_NOT_REACH_AUTHOR', json.dumps(sent))
            self.assertEqual(sent['playtest'], context)
            self.assertIn('NEW_DELAY', sent['game_source']['scripts/main.gd'])
            saved = json.loads((root/'policy.json').read_text())
            self.assertNotIn('skill', saved)
            self.assertEqual(saved['purpose'], 'candidate_acceptance')

    def test_initialization_skill_reaches_program_author_when_enabled(self):
        calls = []
        def call(*args, **kwargs):
            calls.append(args)
            return {'source': 'def act(state, recent):\n    return {"action": "wait", "reason": "observe"}'}
        fake = types.SimpleNamespace(call=call, img=lambda *a, **k: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(temp)
            (root/'scripts').mkdir()
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            (root/'scripts/main.gd').write_text('extends Node\n')
            context = {'phase': 'initialization', 'goal': 'Reach the first obstacle',
                       'behavior_skill_enabled': True}
            decide = program_player(root, root/'policy.py', adapter=DreadWingsAdapter(),
                                    playtest_context=context)
            decide('frame.png', 'Use short taps near obstacles.', [], 6,
                   current_state={'phase': 'ready'})
            sent = json.loads(calls[0][2][0]['text'])
            self.assertEqual(sent['behavior_skill'], 'Use short taps near obstacles.')
            self.assertEqual(sent['playtest'], context)
            self.assertEqual(json.loads((root/'policy.json').read_text())['skill'],
                             'Use short taps near obstacles.')

    def test_final_showcase_exposes_profile_and_coverage_feedback(self):
        calls = []
        def call(*args, **kwargs):
            calls.append(args)
            return {'source': 'def act(state, recent):\n    return {"action": "wait", "reason": "observe"}'}
        fake = types.SimpleNamespace(call=call, img=lambda *a, **k: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(temp)
            (root/'scripts').mkdir()
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            (root/'scripts/main.gd').write_text('extends Node\n')
            context = {'phase': 'final_showcase', 'profiles': [
                {'profile': 'showcase/core', 'scenario': 'title_flow',
                 'normal_launch': True, 'goal': 'show score'}],
                'coverage_feedback': {'status': 'not_shown'},
                'behavior_skill_enabled': False}
            decide = program_player(root, root/'policy.py', adapter=DreadWingsAdapter(),
                                    playtest_context=context)
            decide.set_profile('showcase/core')
            decide('frame.png', '', [], 6, current_state={'phase': 'ready'})
            sent = json.loads(calls[0][2][0]['text'])
            self.assertEqual(sent['sample_state']['_profile'], 'showcase/core')
            self.assertEqual(sent['playtest']['coverage_feedback']['status'], 'not_shown')

    def test_cli_selects_program_mode_without_changing_default_backend(self):
        from gamecraft_training.package import __main__ as cli
        args = ['runner', '--game-package', 'unused', '--output', 'unused',
                '--asset-python', 'unused', '--godot', 'unused', '--player-mode', 'program']
        with patch.object(sys, 'argv', args), patch.object(
                cli, 'run', return_value={'state': 'completed', 'errors': []}) as run:
            cli.main()
        self.assertEqual(run.call_args.kwargs['player_mode'], 'program')
        self.assertEqual(run.call_args.kwargs['player_backend'], 'local')

    def test_program_mode_rejects_pixel_only_player(self):
        from gamecraft_training.package.loop import run
        with self.assertRaisesRegex(ValueError, 'engine state'):
            run('unused', 'unused', godot='unused', asset_python='unused',
                player_mode='program', player_state='pixels')


if __name__ == '__main__':
    unittest.main()
