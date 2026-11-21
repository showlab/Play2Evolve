import copy
import json
import tempfile
import unittest
import sys
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_training.package import models, trajectories


def proposal(tasks, actions=None, step=6):
    return {'scenarios': [{'id': task['id'], 'goal': 'show the interaction and its outcome',
                          'reason': 'adapt to the new timing', 'step_frames': step,
                          'actions': list(actions if actions is not None else task['actions'])}
                         for task in tasks]}


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [{'id': 'fight', 'purpose': 'initialization', 'launch': 'default',
                       'actions': ['jab'], 'step_frames': 6}]
        def validate(value):
            if value['action'] not in ('jab', 'wait'):
                raise ValueError('Unknown action')
        self.adapter = SimpleNamespace(validate_action=validate)

    def test_invalid_script_cannot_change_launch_or_skip_a_scene(self):
        for mutate in (lambda p: p['scenarios'].clear(),
                       lambda p: p['scenarios'][0].update(launch='scenario'),
                       lambda p: p['scenarios'][0].update(actions=['shell']),
                       lambda p: p['scenarios'][0].update(step_frames=True),
                       lambda p: p['scenarios'][0].update(actions=['wait'] * 101)):
            value = proposal(self.tasks)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                trajectories.validate(value, self.tasks, self.adapter)
        result = trajectories.validate(proposal(self.tasks, ['wait', 'jab'], 12), self.tasks, self.adapter)
        self.assertEqual(result[0]['launch'], 'default')
        self.assertEqual(self.tasks[0]['actions'], ['jab'])

    def test_export_declared_demos_accepts_pointer_actions(self):
        from gamecraft_behavior.adapters.base import GameAdapter
        adapter = GameAdapter(
            task_id='test', instructions='test', actions={'wait': None},
            bridge_script=Path('bridge.gd'), observation_fields={},
            state_description='test', replay_fields=('state',),
            pointer_actions=('drag',))
        cases = [{'id': 'plant', 'purpose': 'initialization', 'launch': 'default',
                  'step_frames': 6, 'actions': [
                      {'action': 'drag', 'pointer': {'x': .1, 'y': .2, 'to_x': .3, 'to_y': .4}},
                      'wait']}]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = trajectories.export_declared_demos(cases, adapter, Path(tmp) / 'demos')
            exported = json.loads((Path(tmp) / 'demos/01_initialization.json').read_text())
        self.assertEqual(manifest['demos'][0]['scenario'], 'plant')
        self.assertEqual([event['type'] for event in exported['events']],
                         ['mouse_down', 'mouse_move', 'mouse_up'])

    def test_execution_failure_feedback_is_bounded_and_does_not_adopt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'game'
            game.mkdir()
            episode = {'scenario': 'fight', 'scripted_case': copy.deepcopy(self.tasks[0])}
            feedbacks = []
            def propose(_config, _adapter, _project, tasks, _diagnosis, _changes, _episodes, feedback):
                feedbacks.append(feedback)
                return proposal(tasks, ['wait', 'jab'])
            def play(*args):
                raise RuntimeError('No observation from candidate')
            with self.assertRaisesRegex(RuntimeError, 'Candidate trajectory unavailable'):
                trajectories.replay_candidate({}, self.adapter, game, [episode], root / 'trial',
                    diagnosis={}, changes={}, play=play, propose=propose)
            self.assertEqual(len(feedbacks), 2)
            self.assertIn('No observation', feedbacks[1][0]['error'])
            self.assertEqual(episode['scripted_case']['actions'], ['jab'])

    def test_unmet_goal_reauthors_only_failed_scene_and_keeps_original_checks(self):
        from gamecraft_behavior.core import digest, write_json
        for second_succeeds in (True, False):
            with self.subTest(second_succeeds=second_succeeds), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                game = root / 'game'
                game.mkdir()
                tasks = [dict(self.tasks[0], id='training'), dict(self.tasks[0], id='championship')]
                episodes = [{'scenario': task['id'], 'scripted_case': copy.deepcopy(task)} for task in tasks]
                proposed_calls = []
                def propose(_cfg, _adapter, _project, active, _diagnosis, _changes, _episodes, feedback):
                    proposed_calls.append([task['id'] for task in active])
                    if feedback:
                        self.assertEqual(_episodes[0]['replay']['training_score'], 2)
                        self.assertEqual(feedback[0]['kind'], 'goal_not_met')
                        self.assertEqual(feedback[0]['failed_scenarios'], ['training'])
                        self.assertEqual(feedback[0]['observed']['training']['final_state']['training_score'], 2)
                    rows = proposal(active, ['jab', 'jab'] if feedback else ['jab'])['scenarios']
                    for row in rows:
                        row['checks'] = ([{'field': 'training_score', 'op': 'gte',
                                           'value': 1 if feedback else 3, 'when': 'final'},
                                          {'field': 'state', 'op': 'eq', 'value': 'fight', 'when': 'ever'}]
                                         if row['id'] == 'training' else
                                         [{'field': 'wins', 'op': 'gte', 'value': 1, 'when': 'final'}])
                    return {'scenarios': rows}
                played = []
                def play(_project, active, directory):
                    results = []
                    for index, task in enumerate(active):
                        played.append(task['id'])
                        folder = directory / f'episode-{index:03}'
                        folder.mkdir(parents=True)
                        write_json(folder / 'trace.json', {'events': []})
                        (folder / 'video.mp4').write_bytes(b'video')
                        score = (3 if len(proposed_calls) == 2 and second_succeeds else 2)
                        state = ({'training_score': score,
                                  'state': 'fight' if score == 3 else 'training'}
                                 if task['id'] == 'training' else {'wins': 1, 'state': 'result'})
                        results.append({'replay_verified': True, 'scenario': task['id'],
                                        'normal_launch': True, 'project_sha256': digest(game),
                                        'decision_frames': task['step_frames'], 'replay': state,
                                        'decisions': [{'action': action, 'evaluation_state': state}
                                                      for action in task['actions']],
                                        'trace': str(folder / 'trace.json'),
                                        'video': str(folder / 'video.mp4')})
                    return results
                runs = trajectories.replay_candidate({}, self.adapter, game, episodes, root/'trial',
                    diagnosis={}, changes={}, play=play, propose=propose)
                self.assertEqual(proposed_calls, [['training', 'championship'], ['training']])
                self.assertEqual(played, ['training', 'championship', 'training'])
                self.assertIn('attempt-01', runs[1]['trajectory']['proposal'])
                self.assertIn('attempt-02', runs[0]['trajectory']['proposal'])
                manifest = json.loads((root/'trial/result.json').read_text())
                self.assertEqual(manifest['status'],
                                 'verified' if second_succeeds else 'verified_goal_incomplete')
                self.assertEqual(runs[0]['goal_coverage']['status'],
                                 'passed' if second_succeeds else 'failed')
                # The second author cannot weaken the first attempt's target of 3.
                self.assertEqual(runs[0]['goal_coverage']['checks'][0]['value'], 3)

    def test_goal_check_distinguishes_reached_state_from_final_state(self):
        run={'decisions':[{'evaluation_state':{'state':'fight','training_score':3}}],
             'replay':{'state':'result','training_score':3}}
        checks=[{'field':'state','op':'eq','value':'fight','when':'ever'},
                {'field':'state','op':'eq','value':'fight','when':'final'}]
        result=trajectories.check_goals(run,checks)
        self.assertEqual([item['passed'] for item in result['checks']],[True,False])
        self.assertEqual(result['status'],'failed')

    def test_accepted_actions_reach_next_round_and_final_benchmark_json(self):
        from tests.test_scene_initialization import SceneInitializationTests
        for mode, groups in (('per_scene', None), ('whole_game', None),
                             ('grouped', [{'id': 'both', 'scenarios': ['initialization', 'second']}])):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def propose(_cfg, _adapter, project, tasks, *args):
                    self.assertTrue((Path(project) / 'fixed.gd').is_file())
                    return proposal(tasks, ['jab', 'jab'], 12)
                result, _ = SceneInitializationTests().execute(root, trajectory=propose,
                    initialization_mode=mode, initialization_groups=groups)
                self.assertEqual(result['state'], 'completed')
                current = json.loads((root / 'run/current_scenarios.json').read_text())
                final = json.loads((Path(result['package']) / 'scenarios.json').read_text())
                initial = json.loads((root / 'run/initial/scenarios.json').read_text())
                self.assertEqual(final, current)
                self.assertEqual(final[0]['actions'], ['jab', 'jab'])
                self.assertEqual(initial[0]['actions'], ['jab'])
                self.assertEqual(final[0]['step_frames'], 12)
                demo = json.loads((Path(result['package']) / 'game/demo_outputs/01_initialization.json').read_text())
                self.assertEqual(demo['duration_frames'], 24)
                self.assertEqual([e['frame'] for e in demo['events']], [0, 12])
                self.assertEqual(demo['scenario'], 'initialization')
                self.assertEqual(result['benchmark_demos']['status'], 'declared_unverified')
                # The next round's cached rollout must have the newly adopted input signature.
                if mode == 'per_scene':
                    self.assertTrue((root / 'run/scenes/001/round-002/initialization_current_reused.json').exists())

    def test_rejected_actions_do_not_reach_next_round(self):
        from tests.test_scene_initialization import SceneInitializationTests
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = []
            def propose(_cfg, _adapter, _project, tasks, *args):
                seen.append(copy.deepcopy(tasks))
                return proposal(tasks, ['jab', 'jab'], 12)
            SceneInitializationTests().execute(root, trajectory=propose, reject=True)
            self.assertGreaterEqual(len(seen), 1)
            self.assertTrue(all(tasks[0]['actions'] == ['jab'] for tasks in seen))
            current = json.loads((root / 'run/current_scenarios.json').read_text())
            self.assertEqual(current[0]['actions'], ['jab'])
            self.assertTrue((root / 'run/scenes/001/round-002/initialization_current_reused.json').exists())

    def test_comparison_keeps_candidate_tail_when_trajectories_have_different_lengths(self):
        def episode(count, step):
            return [{'scenario': 'fight', 'normal_launch': False, 'decisions': [
                {'frame': i * step, 'action': 'jab', 'reason': '', 'image': f'{count}-{i}.png'}
                for i in range(count)]}]
        left, right = episode(1, 6), episode(5, 12)
        with patch.dict(sys.modules, {'llm': SimpleNamespace(
                img=lambda path, **kw: {'type': 'input_image', 'path': path})}):
            rows = models._independent_repair_content(left, right, limit=12)
        self.assertIn('5-4.png', [row['path'] for row in rows if row['type'] == 'input_image'])
        labels = [row['text'] for row in rows if row['type'] == 'input_text']
        self.assertTrue(any('VERSION B' in row for row in labels))
        with patch.object(models, '_independent_repair_content', return_value=[]) as independent, \
             patch.object(models, '_paired_repair_content', side_effect=AssertionError('No index pairing')), \
             patch.object(models, 'judge_call', return_value={'preferred': 'B'}) as judge:
            models.selection({'_profile': {'name': 'initialization'}, 'quality': {'criteria': []}},
                             SimpleNamespace(instructions='rules'), {}, left, right, max_reviews=1)
        self.assertEqual(independent.call_count, 1)
        self.assertIn('Actions, durations and decision indices may differ', judge.call_args.args[0])

    def test_export_preserves_normal_launch_and_rejects_another_game_version(self):
        from gamecraft_behavior.core import digest, write_json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / 'game'
            game.mkdir()
            trace = root / 'trace.json'
            write_json(trace, {'scenario': 'debug_start', 'normal_launch': True,
                              'duration_frames': 6, 'events': []})
            episode = {'scenario': 'entry', 'normal_launch': True, 'replay_verified': True,
                       'project_sha256': digest(game), 'trace': str(trace)}
            trajectories.export_demos(game, [episode], root / 'export')
            exported = json.loads((root / 'export/01_initialization.json').read_text())
            self.assertEqual(set(exported), {'duration_frames', 'events'})
            (game / 'main.gd').write_text('changed version')
            with self.assertRaisesRegex(ValueError, 'does not belong'):
                trajectories.export_demos(game, [episode], root / 'wrong')

    @unittest.skipUnless(os.environ.get('GAMECRAFT_TEST_GODOT'), 'Set GAMECRAFT_TEST_GODOT for engine smoke test')
    def test_real_engine_timing_change_and_new_exported_inputs(self):
        from gamecraft_behavior.runtime import rollout
        sys.path.insert(0, str(Path(__file__).parent / 'fixtures'))
        from counter_adapter import CounterAdapter
        adapter = CounterAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, candidate = root / 'original', root / 'candidate'
            shutil.copytree(Path(__file__).parent / 'fixtures/counter_game', original)
            shutil.copytree(original, candidate)
            source = candidate / 'main.gd'
            source.write_text(source.read_text().replace(
                'if finished or not event is InputEventKey',
                'if ticks < 12 or finished or not event is InputEventKey'))
            tasks = [{'id': 'counter', 'purpose': 'initialization', 'step_frames': 6,
                      'actions': ['jab', 'wait', 'wait']}]
            def play(project, cases, output):
                runs = []
                for index, task in enumerate(cases):
                    actions = iter(task['actions'])
                    def decide(*args, **kwargs):
                        return {'action': next(actions), 'reason': 'fixed input'}
                    result = rollout(project, Path(output) / f'episode-{index}', '', decide,
                        godot=os.environ['GAMECRAFT_TEST_GODOT'], adapter=adapter,
                        scenario=task['id'], step_frames=task['step_frames'], max_decisions=len(task['actions']))
                    result['scripted_case'] = copy.deepcopy(task)
                    runs.append(result)
                return runs
            baseline = play(original, tasks, root / 'baseline')
            stale = play(candidate, tasks, root / 'control')
            def propose(_cfg, _adapter, project, cases, *args):
                self.assertIn('ticks < 12', (Path(project) / 'main.gd').read_text())
                return proposal(cases, ['wait', 'wait', 'jab'])
            fresh = trajectories.replay_candidate({}, adapter, candidate, baseline, root / 'fresh',
                diagnosis={'problem': 'new input window'}, changes={'changed_files': ['main.gd']},
                play=play, propose=propose)
            self.assertEqual(baseline[0]['replay']['points'], 2)
            self.assertEqual(stale[0]['replay']['points'], 0)
            self.assertEqual(fresh[0]['replay']['points'], 2)
            manifest = trajectories.export_demos(candidate, fresh, root / 'demos')
            demo = json.loads((root / 'demos/01_initialization.json').read_text())
            self.assertEqual([event['frame'] for event in demo['events']], [12])
            self.assertEqual(manifest['status'], 'collection_required')


if __name__ == '__main__':
    unittest.main()
