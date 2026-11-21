import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game_initialization.package import freeze
from gamecraft_behavior.core import write_json, digest, trace
from gamecraft_training.package.evidence import sample
from gamecraft_training.package.loop import run
from gamecraft_training.package import models


class SceneInitializationTests(unittest.TestCase):
    def package(self, root):
        from tests.test_package_training import PackageTrainingTests
        PackageTrainingTests().package(root, 'scene_game', 'hero')
        draft = root / 'draft'
        cases = json.loads((draft / 'scenarios.json').read_text())
        cases.insert(1, {'id': 'second', 'purpose': 'initialization', 'actions': ['jab']})
        write_json(draft / 'scenarios.json', cases)
        write_json(draft / 'acceptance_plan.json', {'requirements': [
            {'id': 'R1', 'implementation': 'Both counter scenes',
             'scenarios': ['initialization', 'second']}]})
        with patch('gamecraft_behavior.runtime.rollout', return_value={'replay_verified': True, 'decisions': [1]}):
            return freeze(draft, root / 'scene-frozen', 'godot')

    def execute(self, root, *, unverified=False, missing_evidence=None, stage_calls=None,
                rounds=3, selected_scene=None, initialization_mode='per_scene',
                initialization_groups=None, trajectory=None, reject=False,
                initialization_playtest='fixed',initialization_group_count=3,distinct_candidates=False,
                initialization_behavior_skill=False,initial_behavior_skill=None,skill_proposer=None,
                final_showcase=False):
        package = self.package(root)
        observed = []

        def rollout(project, out, skill, decide, **kw):
            Path(out).mkdir(parents=True, exist_ok=True)
            actions = [decide(None, None, None, None)['action'] for _ in range(kw['max_decisions'])]
            image = Path(out) / 'image.png'
            image.write_bytes(b'frame')
            decisions = [{'frame': index * kw['step_frames'], 'action': action, 'reason': 'script',
                          'image': str(image)} for index, action in enumerate(actions)]
            trace_path = Path(out) / 'trace.json'
            write_json(trace_path, trace(decisions, len(actions) * kw['step_frames'], kw['adapter']))
            video = Path(out) / 'video.mp4'
            video.write_bytes(b'fake video')
            observed.append((kw['scenario'], (Path(project) / 'fixed.gd').exists()))
            return {'replay_verified': True, 'replay': {'points': 2},
                    'scenario': kw['scenario'], 'fixed': (Path(project) / 'fixed.gd').exists(),
                    'normal_launch': kw['normal_launch'],
                    'decision_frames': kw['step_frames'],
                    'project_sha256': digest(project), 'trace': str(trace_path), 'video': str(video),
                    'decisions': decisions}

        def gate(*args):
            raise AssertionError('Independent initialization Judge must not run')

        def judge(config, adapter, project, episodes):
            if stage_calls is not None:stage_calls.append('quality')
            return {'build_ok': True, 'errors': [], 'num_demos': len(episodes),
                    'scores': {'art': 1.0, 'layout': 1.0}}

        issue = {'id': 'fix', 'route': 'composition', 'asset_ids': [],
                 'problem': 'counter missing', 'rendered_acceptance': ['counter works']}

        def route(config, adapter, project, plan, composition, evaluation, episodes, history):
            if stage_calls is not None:stage_calls.append('route')
            self.assertNotIn('initialization_gate', evaluation)
            scope = config.get('_initialization_scene')
            scene = scope['id'] if scope else None
            return {'coverage_passed': True, 'coverage_reason': 'shown', 'missing_evidence': missing_evidence or [],
                    'issues': [issue] if scene in ('initialization', 'both', None) and
                    not (Path(project) / 'fixed.gd').exists() else []}

        def repair(*args):
            return {'status': 'not_exercised' if unverified else 'gone',
                    'coverage_passed': not unverified, 'coverage_reason': 'not shown' if unverified else 'shown',
                    'defect_fixed': not unverified, 'regressions': [],
                    'acceptance_checks': [{'criterion': 'counter works', 'passed': not unverified,
                                           'evidence': 'frame'}]}

        service = SimpleNamespace(
            judge=judge,
            initialization=gate, route=route, repair=repair)
        if skill_proposer is not None:
            service.skill=skill_proposer
        service.selection=lambda _config,_adapter,_diagnosis,_before,after,**kwargs:[{
            'preferred_role':'unavailable' if unverified else
                             'candidate' if after[0]['fixed'] and not reject else 'incumbent',
            'evidence_valid':not unverified,'confidence':'high',
            'critical_regression':False,'reason':'matched scene comparison'}]
        service.trajectory = trajectory or (lambda _config, _adapter, _project, tasks, *args: {
            'scenarios': [{'id': case['id'], 'goal': 'show counter', 'reason': 'same timing remains valid',
                           'actions': case['actions'], 'step_frames': case.get('step_frames', 6)}
                          for case in tasks]})

        edit_count=0
        def editor(project, candidate, out, **kw):
            nonlocal edit_count
            edit_count+=1
            shutil.copytree(project, candidate)
            (candidate / 'fixed.gd').write_text('extends Node\n'+
                (f'# candidate {edit_count}\n' if distinct_candidates else ''))
            return {'changed_files': ['fixed.gd']}

        class NoopGenerator:
            def __init__(self, path, *args):
                self.root = Path(path)
                self.root.mkdir()

        with contextlib.redirect_stdout(io.StringIO()), \
             patch('gamecraft_behavior.runtime.rollout', side_effect=rollout):
            result = run(package, root / 'run', godot='godot', asset_python='unused',
                         profile='initialization', rounds=rounds, services=service,
                         rollout_fn=rollout, generator_factory=NoopGenerator,
                         composition_mode='codex', composition_policy={'protected': []},
                         code_editor=editor, initialization_max_frames=18,
                         initialization_scene=selected_scene,
                         initialization_mode=initialization_mode,
                         initialization_groups=initialization_groups,
                         initialization_playtest=initialization_playtest,
                         final_showcase=final_showcase,
                         initialization_behavior_skill=initialization_behavior_skill,
                         initial_behavior_skill=initial_behavior_skill,
                         initialization_group_count=initialization_group_count)
        return result, observed

    def test_accepted_scene_change_is_inherited_without_final_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed = self.execute(root)
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(any(scene == 'second' and fixed for scene, fixed in observed))
            self.assertEqual([row['rounds_completed'] for row in result['scenes']], [2, 1])
            self.assertEqual(result['final_acceptance'], 'not_run')
            self.assertEqual(result['test_scope'], 'not_run')
            self.assertIsNone(result['test'])
            self.assertNotIn('initialization_gate', result)
            self.assertFalse((root / 'run/final_scene_gates.json').exists())
            self.assertFalse((root / 'run/final_smoke').exists())
            self.assertTrue(Path(result['package']).exists())
            self.assertEqual(json.loads((root / 'run/scene_schedule.json').read_text())['max_frames'], 18)
            self.assertTrue((root / 'run/scenes/001/round-001/result.json').exists())

    def test_initialization_uses_only_quality_and_router_before_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            result,_=self.execute(root,stage_calls=calls)
            self.assertEqual(calls,['quality','route']*3)
            self.assertEqual([row['status'] for row in result['scenes']],
                             ['no_observed_issues','no_observed_issues'])
            self.assertTrue(all('gate' not in row for row in result['scenes']))
            self.assertEqual(list((root/'run').rglob('initialization_gate*.json')),[])
            for path in (root/'run').rglob('quality_before.json'):
                quality=json.loads(path.read_text())
                self.assertNotIn('initialization_gate',quality)
                self.assertEqual(quality['execution_profile']['acceptance'],'relative')

    def test_missing_requirement_evidence_does_not_claim_clean_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            result,_=self.execute(Path(tmp),selected_scene='second',rounds=2,
                                  missing_evidence=['Victory was not reached'])
            scene=result['scenes'][0]
            self.assertEqual(scene['rounds_completed'],2)
            self.assertEqual(scene['status'],'optimized')
            self.assertEqual(scene['missing_evidence'],['Victory was not reached'])
            self.assertEqual(result['final_acceptance'],'not_run')

    def test_next_round_reuses_verified_winner_replay_only_for_same_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.execute(root)
            reused = root / 'run/scenes/001/round-002/initialization_current_reused.json'
            self.assertTrue(reused.exists())
            record = json.loads(reused.read_text())
            self.assertEqual(record['scenarios'], ['initialization'])
            self.assertFalse((root / 'run/scenes/001/round-002/initialization_current').exists())
            self.assertFalse((root / 'run/scenes/002/round-001/initialization_current_reused.json').exists())

    def test_insufficient_coverage_retains_candidate_without_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed = self.execute(root, unverified=True)
            side = json.loads((root / 'run/scenes/001/round-001/composition/result.json').read_text())
            self.assertEqual(side['action'], 'retained_unverified')
            self.assertTrue(Path(side['candidate']).exists())
            self.assertEqual(result['scenes'][0]['status'], 'unverified')
            self.assertFalse(any(scene == 'second' and fixed for scene, fixed in observed))
            self.assertFalse((root / 'run/scenes/001/round-002').exists())
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(Path(result['package']).exists())

    def test_no_final_quality_gate_blocks_package_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _ = self.execute(root)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['final_acceptance'], 'not_run')
            self.assertTrue(Path(result['package']).exists())
            self.assertEqual(json.loads((Path(result['package'])/'checks/report.json').read_text())['status'],
                             'not_run')

    def test_scene_sampling_keeps_more_than_four_timepoints(self):
        episode = {'scenario': 'training', 'decisions': [
            {'frame': index, 'image': f'{index}.png', 'action': 'jab', 'reason': 'test'}
            for index in range(14)]}
        self.assertEqual(len(sample([episode], limit=14, scene_focus=True)), 14)
        self.assertEqual(len(sample([episode], limit=14)), 4)


    def test_project_artifacts_show_shipped_demo_and_event_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'project.godot').write_text('config_version=5\n')
            (project / 'assets').mkdir()
            (project / 'scenes').mkdir()
            (project / 'scripts').mkdir()
            demos = project / 'demo_outputs'
            demos.mkdir()
            write_json(demos / 'editor.json', {'duration_frames': 30, 'events': [
                {'frame': 0, 'type': 'mouse_click', 'x': 2, 'y': 3},
                {'frame': 8, 'type': 'key_press', 'keycode': 'P'}]})
            facts = models._project_artifacts(project)
            self.assertTrue(facts['project_godot_exists'])
            self.assertTrue(facts['directories']['scenes'])
            self.assertIn('scenes', facts['top_level_directories'])
            self.assertEqual(facts['demo_file_count'], 1)
            self.assertEqual(facts['demo_files'][0]['event_types'], ['key_press', 'mouse_click'])
            self.assertTrue(facts['demo_files'][0]['frames_ordered'])
            self.assertTrue(facts['demo_files'][0]['frames_within_duration'])

    def test_single_scene_runs_no_other_scene_or_full_package_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed = self.execute(root, selected_scene='second')
            self.assertEqual(result['initialization_scope'], 'single_scene')
            self.assertEqual([row['scene'] for row in result['scenes']], ['second'])
            self.assertTrue(all(scene == 'second' for scene, _ in observed))
            self.assertNotIn('package', result)
            self.assertEqual(result['state'], 'completed')

    def test_whole_game_runs_both_scenes_in_each_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, observed = self.execute(root, rounds=2, initialization_mode='whole_game')
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['initialization_scope'], 'whole_game')
            self.assertEqual([row['scene'] for row in result['scenes']], [])
            self.assertEqual(len(json.loads((root / 'run/history.json').read_text())), 2)
            self.assertTrue((root / 'run/round-001/result.json').exists())
            self.assertTrue((root / 'run/round-002/result.json').exists())
            reused = json.loads((root / 'run/round-002/initialization_current_reused.json').read_text())
            self.assertEqual(reused['scenarios'], ['initialization', 'second'])
            self.assertFalse((root / 'run/scene_schedule.json').exists())
            self.assertEqual([scene for scene, _ in observed[:2]], ['initialization', 'second'])
            self.assertTrue(any(scene == 'second' and fixed for scene, fixed in observed))

    def test_grouped_round_reuses_all_member_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.execute(root, rounds=2, initialization_mode='grouped',
                         initialization_groups=[{'id': 'both',
                                                 'scenarios': ['initialization', 'second']}])
            reused = json.loads((root / 'run/scenes/001/round-002/initialization_current_reused.json').read_text())
            self.assertEqual(reused['scenarios'], ['initialization', 'second'])


if __name__ == '__main__':
    unittest.main()
