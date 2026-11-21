import copy
import hashlib
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
from gamecraft_training.package import initialization_player as player
from gamecraft_training.package.loop import run
from tests import test_scene_initialization


class InitializationPlayerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GAMECRAFT_TEST_GODOT'), 'Set GAMECRAFT_TEST_GODOT for engine smoke test')
    def test_real_game_executes_reactive_candidate_and_preserves_selected_program(self):
        from gamecraft_behavior.runtime import rollout
        sys.path.insert(0, str(Path(__file__).parent/'fixtures'))
        from counter_adapter import CounterAdapter
        class Adapter(CounterAdapter):
            def policy_observation(self, status):
                return {**super().policy_observation(status), 'input_ready': status['ticks'] >= 12}
            def policy_state_fields(self):
                return super().policy_state_fields() | {'input_ready'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root/'original'
            shutil.copytree(Path(__file__).parent/'fixtures/counter_game', original)
            candidate = root/'candidate'
            shutil.copytree(original, candidate)
            code = candidate/'main.gd'
            code.write_text(code.read_text().replace('if finished or not event is InputEventKey',
                            'if ticks < 12 or finished or not event is InputEventKey'))
            contexts = []
            def author(*args, **kwargs):
                data = json.loads(args[2][0]['text'])
                contexts.append(data)
                self.assertNotIn('behavior_skill', data)
                if data['playtest']['prior_policy'] is None:
                    return {'source': 'def act(state, recent):\n    return {"action": "wait", "reason": "observe"}'}
                self.assertIn('"wait"', data['playtest']['prior_policy'])
                return {'source': 'def act(state, recent):\n'
                        '    if state.get("input_ready"):\n'
                        '        return {"action": "jab", "reason": "input ready"}\n'
                        '    return {"action": "wait", "reason": "wait for input"}'}
            fake = SimpleNamespace(call=author, img=lambda *a, **kw: {'type': 'input_image'})
            task = {'id': 'counter', 'purpose': 'initialization', 'launch': 'scenario'}
            unit = {'id': 'core', 'goal': 'Observe scoring', 'members': [task]}
            runner = player.InitializationPlayer(adapter=Adapter(), rollout=rollout,
                godot=os.environ['GAMECRAFT_TEST_GODOT'], model='test', backend='codex',
                step_frames=6, max_decisions=3)
            with patch.dict(sys.modules, {'llm': fake}):
                baseline = runner.play(original, [task], root/'before', unit=unit)
                improved = runner.candidate(candidate, baseline, root/'after',
                    diagnosis={'problem': 'scoring feedback'}, changes={'input_delay': 12})
            self.assertEqual(len(contexts), 2)
            self.assertEqual(baseline[0]['replay']['points'], 0)
            self.assertEqual(improved[0]['replay']['points'], 4)
            self.assertEqual([d['action'] for d in improved[0]['decisions']], ['wait', 'jab', 'jab'])
            runner.remember(candidate, unit, improved)
            self.assertEqual(runner.reusable(candidate, unit)[0]['player_policy'], improved[0]['player_policy'])

    def test_cli_exposes_both_modes_and_passes_player_provider(self):
        from gamecraft_training.package import __main__ as cli
        base = ['runner', '--game-package', 'unused', '--output', 'unused',
                '--asset-python', 'unused', '--godot', 'unused', '--profile', 'initialization']
        for mode in ('fixed', 'program'):
            options = ['--initialization-playtest', mode]
            if mode == 'program':
                options += ['--player-backend', 'codex', '--player-model', 'configured-model']
            with patch.object(sys, 'argv', base + options), \
                 patch.object(cli.shutil, 'which', return_value='/fake/codex'), \
                 patch.object(cli, 'run', return_value={'errors': []}) as invoke:
                cli.main()
            self.assertEqual(invoke.call_args.kwargs['initialization_playtest'], mode)
            self.assertEqual(invoke.call_args.kwargs['initialization_group_count'], 3)
            if mode == 'program':
                self.assertEqual(invoke.call_args.kwargs['player_model'], 'configured-model')

        with patch.object(sys, 'argv', base + ['--initialization-playtest', 'program',
                '--initialization-behavior-skill']), \
             patch.object(cli, 'run', return_value={'errors': []}) as invoke:
            cli.main()
        self.assertTrue(invoke.call_args.kwargs['initialization_behavior_skill'])

    def test_plan_preserves_all_launches_and_rejects_duplicate_or_unknown_scenarios(self):
        tasks = [{'id': 'title', 'launch': 'default'}, {'id': 'combat', 'launch': 'scenario'}]
        proposal = {'groups': [
            {'id': 'entry', 'goal': 'Start normally', 'scenarios': ['title']},
            {'id': 'fight', 'goal': 'Attack and defend', 'scenarios': ['combat']}]}
        units = player.validate_groups(proposal, tasks, 3)
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]['members'][0]['launch'], 'default')
        for bad in ('title', 'nonexistent'):
            changed = copy.deepcopy(proposal)
            changed['groups'][1]['scenarios'] = [bad]
            with self.assertRaises(ValueError):
                player.validate_groups(changed, tasks, 3)

    def test_planner_receives_requirements_code_and_player_provider(self):
        adapter = SimpleNamespace(instructions='public game requirements')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('gamecraft_training.package.models.call', return_value={'groups': []}) as call:
            root = Path(tmp)
            (root/'main.gd').write_text('extends Node\n')
            player.plan_groups(adapter, root, [{'id': 'entry'}], [{'id': 'R1'}],
                               count=3, backend='codex', model='configured-model')
            self.assertEqual(call.call_args.kwargs['model'], 'configured-model')
            self.assertEqual(call.call_args.kwargs['backend'], 'codex')
            self.assertEqual(call.call_args.args[1]['group_count'], 3)
            self.assertEqual(call.call_args.args[1]['requirements'], [{'id': 'R1'}])

    def test_judge_gets_same_group_goal_with_independent_version_evidence(self):
        from gamecraft_training.package import models
        episode = {'scenario': 'entry', 'normal_launch': True,
                   'initialization_unit': {'id': 'core', 'goal': 'Observe scoring'}}
        with patch.object(models, 'content', return_value=[]), \
             patch.object(models, 'outcome_content', return_value=[]):
            rows = models._independent_repair_content([episode], [episode])
        goals = [json.loads(row['text']) for row in rows if row['text'].startswith('{')]
        self.assertEqual(len(goals), 2)
        self.assertEqual(goals[0]['playtest_goals'], goals[1]['playtest_goals'])
        self.assertEqual(goals[0]['playtest_goals'][0]['goal'], 'Observe scoring')

    def exercise(self, root, *, reject=False, count=1, behavior_skill=False,
                 final_showcase=False):
        authored = []
        proposals = []
        def planner(adapter, project, tasks, requirements, **kwargs):
            if count == 1:
                return {'groups': [{'id': 'both', 'goal': 'Observe both counters',
                                    'scenarios': [t['id'] for t in tasks]}]}
            return {'groups': [{'id': 'both' if i == 0 else 'other',
                                'goal': 'Observe counter', 'scenarios': [t['id']]}
                               for i, t in enumerate(tasks)]}
        def factory(project, output, **kwargs):
            authored.append(copy.deepcopy(kwargs['playtest_context']))
            source = f'def act(state, recent):\n    return {{"action": "jab", "reason": "policy {len(authored)}"}}\n'
            Path(output).write_text(source)
            return lambda *args, **kw: {'action': 'jab', 'reason': 'observed counter'}
        def propose_skill(adapter, current, episodes, history):
            proposals.append(current)
            return {'skill': f'play-skill-{len(proposals)}', 'reason': 'Follow observed state'}
        with patch.object(player, 'plan_groups', side_effect=planner) as planning, \
             patch.object(player, 'program_player', side_effect=factory):
            result, observed = test_scene_initialization.SceneInitializationTests().execute(
                root, rounds=3, reject=reject, initialization_playtest='program',
                initialization_group_count=count,distinct_candidates=reject,
                initialization_behavior_skill=behavior_skill,
                final_showcase=final_showcase,
                skill_proposer=propose_skill if behavior_skill else None)
        self.assertEqual(planning.call_count, 1)
        return result, observed, authored

    def test_final_showcase_is_packaged_separately_from_three_group_comparison(self):
        from gamecraft_training.package import final_showcase as showcase
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def recorded(_project, directory, **kwargs):
                directory = Path(directory)
                (directory / 'demo_outputs').mkdir(parents=True)
                write = directory / 'demo_outputs/01_core.json'
                write.write_text('{"duration_frames":30,"events":[]}')
                return {'demos': [{'name': write.name, 'archived_video': 'videos/01_core.mp4'}],
                        'status': 'official_replays_collected',
                        'bootstrap_scene_floor_passed': True}
            def archived(_source, target, manifest):
                target = Path(target)
                (target / 'videos').mkdir(parents=True)
                (target / 'videos/01_core.mp4').write_bytes(b'video')
                (target / 'manifest.json').write_text(json.dumps(manifest))
            with patch.object(showcase, 'run', side_effect=recorded) as run_showcase, \
                 patch.object(showcase, 'archive', side_effect=archived):
                result, _, _ = self.exercise(root, final_showcase=True)
            self.assertEqual(run_showcase.call_count, 1)
            floor = run_showcase.call_args.kwargs['baseline_groups']
            self.assertEqual(len(floor), 2)
            for entry in floor:
                self.assertEqual(entry['scenario'], entry['final_record']['scenario'])
                self.assertTrue(Path(entry['bootstrap_video']).is_file())
            self.assertTrue((Path(result['package']) / 'game/demo_outputs/01_core.json').is_file())
            self.assertTrue(Path(result['showcase_benchmark_videos'][0]).is_file())
            self.assertEqual(len(result['final_benchmark_videos']), 2)

    def test_unverified_bootstrap_floor_prevents_final_package(self):
        from gamecraft_training.package import final_showcase as showcase
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(showcase, 'run', return_value={
                    'bootstrap_scene_floor_passed': False}):
                with self.assertRaisesRegex(RuntimeError, 'Bootstrap scene coverage floor'):
                    self.exercise(root, final_showcase=True)
            self.assertFalse((root / 'run/initialized-package').exists())

    def test_behavior_skill_reauthors_each_round_and_is_frozen_for_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result,_,calls=self.exercise(root,reject=True,behavior_skill=True)
            self.assertEqual(len(calls),8)  # Bootstrap, three baselines/candidates, final
            self.assertEqual(result['initialization_behavior_skill'],True)
            for round_number,expected in ((1,''),(2,'play-skill-1'),(3,'play-skill-2')):
                rd=root/'run/scenes/001'/f'round-{round_number:03}'
                baseline=json.loads((rd/'initialization_current/episode-000/result.json').read_text())
                candidate=json.loads((rd/'composition/candidate_trajectory/episode-000/result.json').read_text())
                self.assertEqual(baseline['player_skill'],expected)
                self.assertEqual(candidate['player_skill'],expected)
                self.assertEqual((rd/'skill_before.txt').read_text(),expected)
                self.assertTrue((rd/'skill_after.txt').read_text().startswith('play-skill-'))
                self.assertFalse((rd/'initialization_current_reused.json').exists())
            self.assertEqual(Path(result['skill']).read_text(),'play-skill-3')
            final=json.loads(Path(result['final_recordings']).read_text())
            self.assertEqual(final['groups']['both']['episodes'][0]['behavior_skill_sha256'],
                             hashlib.sha256(b'play-skill-3').hexdigest())
            self.assertIn('policy 6',calls[7]['prior_policy'])

    def test_winner_program_and_replay_are_inherited_without_reauthoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed, calls = self.exercise(root)
            # The Bootstrap recording is separate from the optimization baseline.
            self.assertEqual(len(calls), 4)
            self.assertIsNone(calls[0]['prior_policy'])
            self.assertIsNone(calls[1]['prior_policy'])
            self.assertIn('policy 2', calls[2]['prior_policy'])
            self.assertEqual(calls[0]['goal'], calls[1]['goal'])
            bootstrap = json.loads(Path(result['bootstrap_recordings']).read_text())
            self.assertTrue(bootstrap['complete'])
            self.assertEqual(bootstrap['groups']['both']['selected_round'], 0)
            self.assertIn('policy 1', (Path(result['bootstrap_recordings']).parent /
                bootstrap['groups']['both']['episodes'][0]['files']['player_policy']['path']).read_text())
            saved = json.loads((root/'run/selected_initialization_playtests.json').read_text())
            episodes = saved['groups']['both']['episodes']
            self.assertIn('policy 3', Path(episodes[0]['player_policy']).read_text())
            final = json.loads(Path(result['final_recordings']).read_text())
            final_policy = (Path(result['final_recordings']).parent /
                            final['groups']['both']['episodes'][0]['files']['player_policy']['path'])
            self.assertIn('policy 4', final_policy.read_text())
            self.assertIn('policy 3', calls[3]['prior_policy'])
            self.assertEqual(episodes[0]['project_sha256'], digest(saved['groups']['both']['project']))
            reused = root/'run/scenes/001/round-002/initialization_current_reused.json'
            self.assertTrue(reused.exists())
            self.assertFalse((root/'run/scenes/001/round-002/initialization_current').exists())
            self.assertEqual(result['initialization_playtest'], 'program')
            self.assertEqual(result['final_acceptance'], 'not_run')
            archive = Path(result['package'])/'initialization_playtests'
            self.assertIn('policy 3', (archive/'group-000-policy-000.py').read_text())
            self.assertFalse((root/'run/final_smoke').exists())

    def test_rejected_candidate_never_supplies_next_round_prior_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed, calls = self.exercise(root, reject=True)
            self.assertEqual(len(calls), 6)  # Bootstrap, baseline, three candidates, final replay
            for index in (2, 3, 4):
                self.assertIn('policy 2', calls[index]['prior_policy'])
            saved = json.loads(Path(result['selected_playtest']).read_text())
            self.assertIn('policy 2', Path(saved['groups']['both']['episodes'][0]['player_policy']).read_text())

    def test_next_group_inherits_shared_selected_program_for_all_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, observed, calls = self.exercise(Path(tmp), count=2)
            self.assertEqual(len(calls), 7)
            bootstrap_path = Path(result['bootstrap_recordings'])
            bootstrap = json.loads(bootstrap_path.read_text())
            self.assertTrue(all(row['project_sha256'] == digest(Path(tmp)/'run/initial/game')
                                for row in bootstrap['groups'].values()))
            policies = [bootstrap_path.parent / row['episodes'][0]['files']['player_policy']['path']
                        for row in bootstrap['groups'].values()]
            self.assertEqual(len({path.read_text() for path in policies}), 2)
            self.assertIsNone(calls[2]['prior_policy'])  # fresh optimization program
            self.assertIn('policy 4', calls[4]['prior_policy'])
            self.assertEqual(calls[4]['reuse_source'], calls[4]['prior_policy'])
            self.assertEqual(len(calls[4]['profiles']), 2)
            self.assertEqual(calls[4]['group'], 'other')
            self.assertIn('policy 4', calls[5]['prior_policy'])
            self.assertIn('policy 5', calls[6]['prior_policy'])
            self.assertNotIn('reuse_source', calls[5])
            self.assertNotIn('reuse_source', calls[6])
            self.assertIn(('second', True), observed)

    def test_invalid_program_settings_fail_before_creating_run(self):
        for options in ({'profile': 'rsi'}, {'player_state': 'pixels'},
                        {'initialization_group_count': 0}, {'initialization_mode': 'whole_game'},
                        {'initialization_groups': []}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run('missing', 'missing', godot='unused', asset_python='unused',
                    **{'profile': 'initialization', 'initialization_playtest': 'program', **options})
        for options in ({'profile': 'rsi'}, {'initialization_playtest': 'fixed'}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'Behavior Skill'):
                run('missing', 'missing', godot='unused', asset_python='unused',
                    **{'profile': 'initialization', 'initialization_playtest': 'program',
                       'initialization_behavior_skill': True, **options})

    def test_reactive_play_cache_detects_modified_policy_and_preserves_goal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root/'game'
            project.mkdir()
            (project/'main.gd').write_text('extends Node\n')
            task = {'id': 'play', 'launch': 'default'}
            unit = {'id': 'core', 'goal': 'Score a point', 'members': [task]}
            def factory(project, output, **kwargs):
                output.write_text('reactive fixture')
                return lambda **kw: {'action': 'tap' if kw['current_state']['y'] > 5 else 'wait'}
            def rollout(project, directory, skill, decide, **kw):
                directory.mkdir()
                for name in ('frame.png', 'video.mp4', 'trace.json'):
                    (directory/name).write_text('fixture')
                ds = [{'action': decide(current_state={'y': y})['action'],
                       'image': str(directory/'frame.png')} for y in (0, 8, 2)]
                return {'project_sha256': digest(project), 'replay_verified': True,
                        'scenario': 'play', 'normal_launch': True, 'decisions': ds,
                        'replay': {'score': 0}, 'trace': str(directory/'trace.json'),
                        'video': str(directory/'video.mp4')}
            runner = player.InitializationPlayer(adapter=None, rollout=rollout, godot='unused',
                model='test', backend='codex', step_frames=6, max_decisions=3, policy_factory=factory)
            episodes = runner.play(project, [task], root/'play', unit=unit)
            self.assertEqual([d['action'] for d in episodes[0]['decisions']], ['wait', 'tap', 'wait'])
            runner.remember(project, unit, episodes)
            self.assertIsNotNone(runner.reusable(project, unit))
            Path(episodes[0]['player_policy']).write_text('changed')
            self.assertIsNone(runner.reusable(project, unit))


if __name__ == '__main__':
    unittest.main()
