import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_training.package.loop import run
from gamecraft_training.package.interactive_initialization import InteractiveInitializationPlayer
from gamecraft_behavior.adapters.base import GameAdapter
from gamecraft_behavior.core import digest
from gamecraft_behavior.runtime import player as acting_player
from tests.test_scene_initialization import SceneInitializationTests


class InitializationInteractiveModesTests(unittest.TestCase):
    def test_interactive_player_receives_brief_demo_opening_and_behavior_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'package/game'
            game.mkdir(parents=True)
            (game / 'project.godot').write_text('config_version=5\n')
            (game.parent / 'instruction.md').write_text('Enter the map and plant defenders.')
            adapter = GameAdapter(task_id='plant', instructions='Defend the garden',
                actions={'wait': None, 'confirm': 'ENTER'}, bridge_script=root / 'bridge.gd',
                observation_fields={'screen': 'state'}, state_description='screen',
                replay_fields=('state',))
            received = []
            def rollout(project, output, guidance, decide, **kwargs):
                received.append(guidance)
                output.mkdir(parents=True)
                for name in ('trace.json', 'replay.mp4'):
                    (output / name).write_text('evidence')
                return {'project_sha256': digest(project), 'replay_verified': True,
                        'decisions': [{'action': 'confirm'}], 'scenario': 'title',
                        'normal_launch': True, 'trace': str(output / 'trace.json'),
                        'video': str(output / 'replay.mp4')}
            instance = InteractiveInitializationPlayer(adapter=adapter, rollout=rollout,
                godot='godot', model='local', backend='local', step_frames=6,
                max_decisions=2, factory=lambda **kwargs: lambda *args, **kw: None)
            result = instance.play(game, [{'id': 'title', 'launch': 'default',
                'actions': ['wait', 'confirm']}], root / 'output', unit={'id': 'title'},
                skill='When the title is visible, enter the map before planting.')
            self.assertIn('Enter the map and plant defenders.', received[0])
            self.assertIn('wait, confirm', received[0])
            self.assertIn('When the title is visible', received[0])
            self.assertTrue(result[0]['playtest']['uses_behavior_skill'])

    def test_acting_player_reports_controls_and_ineffective_last_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = GameAdapter(task_id='plant', instructions='Defend the garden',
                actions={'wait': None, 'confirm': 'ENTER', 'click_sun': 'C'},
                bridge_script=root / 'bridge.gd', observation_fields={'screen': 'state'},
                state_description='screen', replay_fields=('state',))
            captured = {}
            def call(model, system, content, *args, **kwargs):
                captured.update(system=system, content=content)
                return {'action': 'confirm', 'reason': 'enter map'}
            with patch.dict(sys.modules, {'llm': SimpleNamespace(
                    call=call, img=lambda path: {'image': 'frame'})}):
                decision = acting_player(adapter=adapter)(root / 'current.png',
                    'Enter the map first.', [{'frame': 0, 'action': 'click_sun',
                    'reason': 'start', 'image': str(root / 'old.png'),
                    'player_state': {'screen': 'title'}}], 6,
                    current_state={'screen': 'title'})
            self.assertEqual(decision['action'], 'confirm')
            prompt = ' '.join(str(item) for item in captured['content'])
            self.assertIn('press ENTER', prompt)
            self.assertIn('did not change the observed state', prompt)
            self.assertIn('Enter the map first.', captured['system'])

    def test_pointer_replaces_hide_fixed_target_shortcuts_only_from_interactive_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'game'
            game.mkdir()
            (game / 'project.godot').write_text('config_version=5\n')
            adapter = GameAdapter(task_id='plant', instructions='Place plants',
                actions={'wait': None, 'fixed_cell': 'Q'},
                pointer_actions=('click', 'drag'), pointer_replaces=('fixed_cell',),
                bridge_script=root / 'bridge.gd', observation_fields={'screen': 'state'},
                state_description='screen', replay_fields=('state',))
            seen = []
            def factory(**kwargs):
                seen.append(kwargs['adapter'].action_schema['properties']['action']['enum'])
                return lambda *args, **kwargs: {'action': 'wait', 'reason': 'test'}
            def rollout(project, output, guidance, decide, **kwargs):
                seen.append(kwargs['adapter'].action_schema['properties']['action']['enum'])
                output.mkdir(parents=True)
                for name in ('trace.json', 'replay.mp4'):
                    (output / name).write_text('evidence')
                return {'project_sha256': digest(project), 'replay_verified': True,
                        'decisions': [{'action': 'drag', 'pointer': {'x': 0, 'y': 0, 'to_x': 1, 'to_y': 1}}],
                        'scenario': 'night', 'normal_launch': False,
                        'trace': str(output / 'trace.json'), 'video': str(output / 'replay.mp4')}
            player = InteractiveInitializationPlayer(adapter=adapter, rollout=rollout,
                godot='godot', model='local', backend='local', step_frames=6,
                max_decisions=2, factory=factory)
            player.play(game, [{'id': 'night'}], root / 'output', unit={'id': 'night'})
            self.assertEqual(seen, [['wait', 'click', 'drag'], ['wait', 'click', 'drag']])
            self.assertIn('fixed_cell', adapter.actions)

    def test_agent_uses_one_model_decision_per_action_and_keeps_selected_evidence(self):
        calls = []

        def acting_player(*, model, backend, adapter):
            calls.append((model, backend))
            return lambda *args, **kwargs: {'action': 'jab', 'reason': 'observed'}

        with tempfile.TemporaryDirectory() as tmp, patch(
                'gamecraft_training.package.interactive_initialization.player',
                side_effect=acting_player):
            root = Path(tmp)
            result, observed = SceneInitializationTests().execute(
                root, rounds=3, initialization_playtest='agent')
            self.assertEqual(result['initialization_playtest'], 'agent')
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(calls)
            self.assertTrue(all(pair == ('local', 'local') for pair in calls))
            saved = json.loads(Path(result['selected_playtest']).read_text())
            self.assertEqual(saved['mode'], 'agent')
            for entry in saved['groups'].values():
                for episode in entry['episodes']:
                    self.assertNotIn('player_policy', episode)
                    self.assertTrue(Path(episode['trace']).is_file())
                    self.assertTrue(Path(episode['video']).is_file())
            archive = Path(result['package']) / 'initialization_playtests'
            self.assertTrue((archive / 'selected.json').is_file())
            self.assertFalse(list(archive.glob('*.py')))
            self.assertFalse((archive / 'groups.json').exists())
            manifest = json.loads(Path(result['selected_recordings']).read_text())
            self.assertTrue(manifest['complete'])
            self.assertTrue(observed)

    def test_qwen27_rejects_unverified_service_identity_before_checkout(self):
        with patch.dict(os.environ, {'LOCAL_URL': 'http://127.0.0.1:30021/v1'}, clear=False):
            os.environ.pop('GAMECRAFT_LOCAL_MODEL_ID', None)
            with self.assertRaisesRegex(ValueError, 'GAMECRAFT_LOCAL_MODEL_ID'):
                run('missing-package', 'missing-output', godot='godot', asset_python='unused',
                    profile='initialization', initialization_playtest='qwen27')
        with patch.dict(os.environ, {'LOCAL_URL': 'http://127.0.0.1:30021/v1',
                                      'GAMECRAFT_LOCAL_MODEL_ID': 'Qwen3.8-27B'}, clear=False):
            with self.assertRaisesRegex(ValueError, 'local Player backend'):
                run('missing-package', 'missing-output', godot='godot', asset_python='unused',
                    profile='initialization', initialization_playtest='qwen27',
                    player_backend='codex', player_model='gpt-6-sol')

    def test_qwen27_keeps_local_model_identity_in_run_manifest(self):
        def acting_player(*, model, backend, adapter):
            self.assertEqual((model, backend), ('local', 'local'))
            return lambda *args, **kwargs: {'action': 'jab', 'reason': 'observed'}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'LOCAL_URL': 'http://127.0.0.1:30021/v1',
                'GAMECRAFT_LOCAL_MODEL_ID': 'Qwen3.8-27B'}, clear=False), patch(
                'gamecraft_training.package.interactive_initialization.player',
                side_effect=acting_player):
            root = Path(tmp)
            result, _ = SceneInitializationTests().execute(
                root, rounds=1, initialization_playtest='qwen27')
            settings = json.loads((root / 'run/run.json').read_text())
            self.assertEqual(result['initialization_playtest'], 'qwen27')
            self.assertEqual(settings['player']['local_model_identity'], 'Qwen3.8-27B')
            self.assertEqual(settings['initialization_trajectories']['mode'], 'interactive_agent')

    def test_qwen27_uses_supplied_behavior_skill_without_evolving_it(self):
        def fixed_player(**kwargs):
            return lambda *args, **kw: {'action': 'jab', 'reason': 'observed'}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'LOCAL_URL': 'http://127.0.0.1:30021/v1',
                'GAMECRAFT_LOCAL_MODEL_ID': 'Qwen3.8-27B'}, clear=False), patch(
                'gamecraft_training.package.interactive_initialization.player',
                side_effect=fixed_player):
            root = Path(tmp)
            skill = root / 'behavior_skill.txt'
            skill.write_text('Enter the game before attacking.')
            result, _ = SceneInitializationTests().execute(root, rounds=1,
                initialization_playtest='qwen27', initial_behavior_skill=str(skill))
            saved = json.loads(Path(result['selected_playtest']).read_text())
            self.assertTrue(all(episode['playtest']['uses_behavior_skill']
                                for group in saved['groups'].values()
                                for episode in group['episodes']))
            self.assertEqual((root / 'run/best_skill.txt').read_text(), skill.read_text())


if __name__ == '__main__':
    unittest.main()
