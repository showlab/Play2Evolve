import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter
from gamecraft_behavior.core import digest
from gamecraft_behavior.program_policy import program_player
from gamecraft_training.package.version_player import play_version, profiles_for
from gamecraft_training.package.initialization_player import validate_groups

SOURCE = '''def act(state, recent):
    if state.get("_profile") == "explore":
        return {"action": "tap", "reason": "explore"}
    return {"action": "wait", "reason": "observe"}
'''


class VersionPlayerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GAMECRAFT_TEST_GODOT'), 'Set GAMECRAFT_TEST_GODOT')
    def test_real_engine_reuses_one_policy_across_profile_launches(self):
        from gamecraft_behavior.runtime import rollout
        from gamecraft_training.package.initialization_player import InitializationPlayer
        sys.path.insert(0, str(Path(__file__).parent/'fixtures'))
        from counter_adapter import CounterAdapter
        source = ('def act(state, recent):\n'
                  '    if state.get("_profile") == "explore":\n'
                  '        return {"action": "jab", "reason": "score"}\n'
                  '    return {"action": "wait", "reason": "observe"}\n')
        fake = SimpleNamespace(call=lambda *a, **kw: {'source': source},
                               img=lambda *a, **kw: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}), \
             patch.object(fake, 'call', wraps=fake.call) as calls:
            root = Path(tmp)
            game = root/'game'
            shutil.copytree(Path(__file__).parent/'fixtures/counter_game', game)
            tasks = [{'id': 'counter', 'profile': name, 'launch': 'scenario'}
                     for name in ('explore', 'observe')]
            units = [{'id': task['profile'], 'goal': task['profile'], 'members': [task]}
                     for task in tasks]
            runner = InitializationPlayer(adapter=CounterAdapter(), rollout=rollout,
                godot=os.environ['GAMECRAFT_TEST_GODOT'], model='test', backend='codex',
                step_frames=6, max_decisions=3)
            runner.configure(units)
            first = runner.play(game, [tasks[0]], root/'first', unit=units[0])
            runner.remember(game, units[0], first)
            second = runner.play(game, [tasks[1]], root/'second', unit=units[1])
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(first[0]['replay']['points'], 4)
            self.assertEqual(second[0]['replay']['points'], 0)
            self.assertTrue(first[0]['replay_verified'] and second[0]['replay_verified'])
            self.assertEqual(first[0]['decisions'][0]['frame'], 0)
            self.assertEqual(second[0]['decisions'][0]['frame'], 0)
            self.assertEqual(Path(first[0]['player_policy']).read_text(),
                             Path(second[0]['player_policy']).read_text())

    def test_planner_can_start_three_profiles_at_one_normal_entry(self):
        tasks = [{'id': 'ready', 'launch': 'scenario'}]
        proposal = {'groups': [{'id': name, 'goal': name, 'scenario': None}
                               for name in ('explore', 'observe', 'retry')]}
        units = validate_groups(proposal, tasks, 3)
        self.assertEqual(len(units), 3)
        self.assertTrue(all(u['members'][0]['launch'] == 'default' for u in units))
        self.assertEqual([u['members'][0]['profile'] for u in units], ['explore', 'observe', 'retry'])
        invalid = copy.deepcopy(proposal)
        invalid['groups'][0]['scenario'] = 'invented'
        with self.assertRaises(ValueError):
            validate_groups(invalid, tasks, 3)

    def test_one_author_call_dispatches_profiles_and_inherits_selected_program(self):
        calls, launches = [], []
        def author(*args, **kwargs):
            calls.append(json.loads(args[2][0]['text']))
            return {'source': SOURCE}
        fake = SimpleNamespace(call=author, img=lambda *a, **k: {'type': 'input_image'})
        def rollout(project, output, skill, decide, **kwargs):
            output.mkdir()
            for name in ('frame.png', 'trace.json', 'video.mp4'):
                (output/name).write_text('fixture')
            value = decide(output/'frame.png', skill, [], 6, current_state={'phase': 'title'})
            launches.append((kwargs['scenario'], kwargs['normal_launch']))
            return {'scenario': kwargs['scenario'], 'normal_launch': kwargs['normal_launch'],
                    'project_sha256': digest(project), 'replay_verified': True,
                    'decisions': [{**value, 'image': str(output/'frame.png')}],
                    'trace': str(output/'trace.json'), 'video': str(output/'video.mp4')}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(tmp)
            project = root/'game'
            project.mkdir()
            (project/'main.gd').write_text('extends Node\n')
            (project/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            tasks = [{'id': 'ready', 'profile': name, 'launch': 'default'} for name in ('explore', 'observe')]
            options = dict(profiles=profiles_for(tasks), adapter=DreadWingsAdapter(), rollout=rollout,
                           godot='unused', model='test', backend='codex', step_frames=6,
                           max_decisions=1, factory=program_player)
            before = play_version(project, tasks, root/'before', **options)
            self.assertEqual(len(calls), 1)
            self.assertEqual([r['decisions'][0]['action'] for r in before], ['tap', 'wait'])
            self.assertEqual(len({r['player_policy'] for r in before}), 1)
            self.assertEqual(launches, [('ready', True), ('ready', True)])
            self.assertEqual(len(calls[0]['playtest']['profiles']), 2)
            # Reuse the same version's compiled source with no model call.
            reused = play_version(project, tasks, root/'reuse', prior=before, reuse=True, **options)
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(reused[0]['player_policy']).read_text(), SOURCE)
            # An edited version gets one private adapted program, with the prior.
            (project/'main.gd').write_text('extends Node\n# changed\n')
            candidate = play_version(project, tasks, root/'candidate', prior=before, **options)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1]['playtest']['prior_policy'], SOURCE)
            self.assertNotEqual(before[0]['player_policy'], candidate[0]['player_policy'])
            Path(before[0]['player_policy']).write_text('tampered')
            with self.assertRaisesRegex(RuntimeError, 'missing or changed'):
                play_version(project, tasks, root/'bad', prior=before, **options)


if __name__ == '__main__':
    unittest.main()
