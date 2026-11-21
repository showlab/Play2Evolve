"""Post-RSI selection, frozen inputs, coverage retries and legacy isolation."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gamecraft_behavior.core import digest, write_json
from gamecraft_evaluation import rsi_showcase as post
from gamecraft_evaluation import rsi_baseline_comparison as rsi_compare
from gamecraft_evaluation.showcase_coverage import (CoverageIncompleteError, compact_choices,
                                                    require_ready_for_scoring)
from gamecraft_evaluation.rsi_score_only import select_traces
from gamecraft_evaluation.merged_showcase import (assess_with_retry, validate_assignments,
                                                  preserves)
from gamecraft_training.package import final_showcase as showcase


class RSIShowcaseTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run_dir = self.root / 'run'
        initial = self.run_dir / 'initial'
        (initial / 'adapter').mkdir(parents=True)
        (initial / 'adapter/__init__.py').write_text('# trusted adapter fixture\n')
        self.original = initial / 'game'
        self.project = self.run_dir / 'round-002/candidate_project'
        for project in (self.original, self.project):
            (project / 'demo_outputs').mkdir(parents=True)
            (project / 'project.godot').write_text('[application]\n')
            write_json(project / 'game_plan.json', {'task_id': 'example'})
            write_json(project / 'demo_outputs/old.json', {'duration_frames': 30, 'events': []})
        (self.project / 'upgrade.gd').write_text('# selected upgrade\n')
        (self.run_dir / 'best_skill.txt').write_text('Observe the tell before responding.')
        (self.run_dir / 'working_skill.txt').write_text('UNSELECTED SKILL')
        # Stored host paths deliberately differ from the local test mount.
        old = Path('/old-mount/experiment/run')
        self.info = {'profile': 'rsi', 'state': 'completed_with_errors',
                     'upgrade_status': 'accepted', 'full_build_status': 'playable',
                     'project': str(old / 'round-002/candidate_project'),
                     'working_project': str(old / 'round-002/candidate_project'),
                     'original_project': str(old / 'initial/game'),
                     'skill': str(old / 'best_skill.txt'),
                     'final_review': str(old / 'final_upgrade_review.json'),
                     'upgrade_plan': str(old / 'rsi_upgrade_plan.json'),
                     'errors': [{'round': 1, 'reason': 'recovered service error'}]}
        write_json(self.run_dir / 'result.json', self.info)
        write_json(self.run_dir / 'run.json', {'profile': 'rsi', 'task_id': 'example',
                   'step_frames': 6, 'player': {'backend': 'codex', 'model': 'test-model'}})
        write_json(self.run_dir / 'final_upgrade_review.json', {'status': 'accepted', 'selected': 'upgrade'})
        write_json(initial / 'requirements.json', {'requirements': [
            {'id': 'R1', 'dynamic': True, 'description': 'Core play'},
            {'id': 'R2', 'dynamic': True, 'description': 'Retry from defeat'}]})
        write_json(initial / 'scenarios.json', [
            {'id': 'title', 'launch': 'default'},
            {'id': 'fight', 'launch': 'scenario'}, {'id': 'advanced', 'launch': 'scenario'}])
        write_json(self.run_dir / 'rsi_upgrade_playtests.json', [
            {'id': 'fight', 'launch': 'scenario', 'profile': 'core', 'goal': 'Play the core fight'},
            {'id': 'advanced', 'launch': 'scenario', 'profile': 'upgrade', 'goal': 'Read and evade a feint'}])
        write_json(self.run_dir / 'rsi_upgrade_plan.json', {
            'gameplay_plan': {'change': 'Add a feint', 'validation': 'Evade after the second tell'},
            'aesthetic_plan': {'change': 'Animate the feint', 'validation': 'Visible continuous motion'}})
        self.godot = self.root / 'godot'
        self.godot.write_text('#!/bin/sh\nexit 0\n')
        self.godot.chmod(0o755)
        self.bench = self.root / 'GameCraft-Bench'
        (self.bench / 'tasks/example/tests').mkdir(parents=True)
        write_json(self.bench / 'tasks/example/tests/rubric.json', {'SECRET_RUBRIC': 'never used in planning'})
        verifier = self.bench / 'gamecraft_bench/verifier'
        verifier.mkdir(parents=True)
        for name in ('replay.py', 'score.py'):
            (verifier / name).write_text('# mock official tool\n')
        self.output = self.root / 'post'
        self.calls = []
        self.planned = []

    def fake_factory(self, project, policy, **kwargs):
        self.calls.append(kwargs['playtest_context'])
        policy.write_text('def act(state, recent):\n    return {"action": "wait"}\n')
        return lambda *a, **kw: {'action': 'wait'}

    def fake_rollout(self, project, directory, skill, decide, **kwargs):
        self.assertEqual(skill, 'Observe the tell before responding.')
        self.assertEqual(kwargs['player_state'], 'engine')
        directory.mkdir(parents=True)
        trace, video = directory / 'trace.json', directory / 'video.mp4'
        write_json(trace, {'duration_frames': 30, 'events': [
            {'frame': 1, 'type': 'key_press', 'keycode': 'SPACE'}]})
        video.write_bytes(b'recorded play')
        return {'project_sha256': digest(project), 'replay_verified': True,
                'decisions': [{'action': 'wait', 'frame': 0}],
                'scenario': kwargs['scenario'], 'normal_launch': kwargs['normal_launch'],
                'trace': str(trace), 'video': str(video)}

    def fake_official(self, project, record, directory, godot, **kwargs):
        directory.mkdir(parents=True)
        trace, video, frame = directory / 'demo.json', directory / 'official.mp4', directory / 'frame.png'
        write_json(trace, showcase.public_trace(record))
        video.write_bytes(b'OS replay')
        frame.write_bytes(b'frame')
        return {'trace': str(trace), 'video': str(video), 'frames': [str(frame)],
                'duration_seconds': 1.0, 'trace_seconds': 1.0}

    def fake_assess(self, row, official, **kwargs):
        return [{'id': rid, 'status': 'not_shown' if rid == 'R2' else 'shown',
                 'reason': 'Observed coverage', 'evidence': [] if rid == 'R2' else ['frame.png']}
                for rid in row['requirement_ids']]

    def fake_plan(self, adapter, project, scenarios, requirements, previous, **kwargs):
        self.planned.append((requirements, kwargs))
        self.assertEqual([r['id'] for r in requirements], ['R2'])
        self.assertEqual(kwargs['recording_context'], 'rsi')
        self.assertEqual(kwargs['max_demos'], 7)
        self.assertNotIn('SECRET_RUBRIC', json.dumps(requirements))
        return {'profiles': [{'id': 'retry_flow', 'goal': 'Show retry', 'scenario': None,
                              'requirement_ids': ['R2'], 'expected': 'Retry returns to play',
                              'max_decisions': 100}]}

    def fake_collect(self, project, output, bench, godot):
        (output / 'collection/pass-01').mkdir(parents=True)
        write_json(output / 'collection/pass-01/breakdown.json', {'demos': [
            {'demo_id': p.stem, 'frames': ['frame.png'], 'duration_seconds': 1.0}
            for p in sorted((project / 'demo_outputs').glob('*.json'))]})
        write_json(output / 'collection/collection.json', {'build_ok': True, 'errors': []})
        return {'build_ok': True, 'errors': []}

    def execute(self, collector=None, *, mode='full', assessor=None,
                merge_planner=None, scene_comparator=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(post, 'configure_benchmark', return_value=self.bench))
            stack.enter_context(patch.object(showcase, 'benchmark_root', return_value=self.bench))
            stack.enter_context(patch('gamecraft_evaluation.preserve_showcase.configure_benchmark', return_value=self.bench))
            stack.enter_context(patch.object(post, 'load_adapter', return_value=SimpleNamespace(task_id='example')))
            stack.enter_context(patch('gamecraft_behavior.program_policy.program_player', self.fake_factory))
            stack.enter_context(patch('gamecraft_behavior.runtime.rollout', self.fake_rollout))
            stack.enter_context(patch.object(showcase, '_official_record', self.fake_official))
            stack.enter_context(patch.object(showcase, 'assess', assessor or self.fake_assess))
            def scenes(group, official, *, facts=None, **kwargs):
                rows = facts or [{'id': 'B1', 'description': 'Original scene',
                                  'bootstrap_frame': 'B01'}]
                return {'facts': [{**row, 'covered': True, 'final_frame': 'F01'}
                                  for row in rows], 'passed': True}
            stack.enter_context(patch.object(rsi_compare, 'compare_baseline',
                                             scene_comparator or scenes))
            stack.enter_context(patch.object(showcase, 'plan', self.fake_plan))
            if merge_planner is not None:
                stack.enter_context(patch.object(post, 'plan_route_merges', merge_planner))
            stack.enter_context(patch.object(post, 'collect_official', collector or self.fake_collect))
            stack.enter_context(patch('gamecraft_evaluation.rsi_score_only.collect_official',
                                      collector or self.fake_collect))
            return post.run(run_dir=self.run_dir, output=self.output, bench=self.bench,
                            godot=self.godot, provider={'codex_provider': 'openai'}, mode=mode)

    def test_accepted_with_errors_uses_selected_version_and_rebased_paths(self):
        source = post.load_source(self.run_dir)
        self.assertEqual(source['project'], self.project)
        self.assertEqual(source['skill'], 'Observe the tell before responding.')
        requirements, profiles = post.coverage_inputs(source, 6)
        self.assertEqual([r['id'] for r in profiles['profiles']], ['rsi_core', 'rsi_upgrade', 'rsi_entry'])
        self.assertIsNone(profiles['profiles'][2]['scenario'])
        self.assertTrue(any(r['description'] == 'Add a feint' for r in requirements))

    def test_retained_original_does_not_claim_rejected_upgrade_goals(self):
        self.info.update(upgrade_status='retained_original', project=self.info['original_project'])
        write_json(self.run_dir / 'result.json', self.info)
        write_json(self.run_dir / 'final_upgrade_review.json', {'status': 'retained_original', 'selected': 'original'})
        source = post.load_source(self.run_dir)
        self.assertEqual(source['project'], self.original)
        requirements, profiles = post.coverage_inputs(source, 6)
        self.assertEqual([r['id'] for r in profiles['profiles']], ['rsi_core', 'rsi_entry'])
        self.assertNotIn('Add a feint', json.dumps(requirements))

    def test_explicit_working_project_uses_last_round_admitted_upgrade(self):
        self.info.update(upgrade_status='retained_original',
                         project=self.info['original_project'],
                         working_skill='/old-mount/experiment/run/working_skill.txt')
        write_json(self.run_dir / 'result.json', self.info)
        write_json(self.run_dir / 'final_upgrade_review.json',
                   {'status': 'retained_original', 'selected': 'original'})
        write_json(self.run_dir / 'history.json', [{
            'action': 'accept_repair',
            'project_after': self.info['working_project']}])
        self.assertEqual(post.load_source(self.run_dir)['project'], self.original)
        source = post.load_source(self.run_dir, use_working_project=True)
        self.assertEqual(source['project'], self.project)
        self.assertEqual(source['skill'], 'UNSELECTED SKILL')
        self.assertEqual(source['selection_override'], 'working_project')
        self.assertEqual(len(post.coverage_inputs(source, 6)[1]['profiles']), 3)

    def test_working_project_override_requires_round_acceptance(self):
        self.info.update(upgrade_status='retained_original',
                         project=self.info['original_project'],
                         working_skill='/old-mount/experiment/run/working_skill.txt')
        write_json(self.run_dir / 'result.json', self.info)
        write_json(self.run_dir / 'final_upgrade_review.json',
                   {'status': 'retained_original', 'selected': 'original'})
        write_json(self.run_dir / 'history.json', [{
            'action': 'reject_repair',
            'project_after': self.info['working_project']}])
        with self.assertRaisesRegex(ValueError, 'not admitted'):
            post.load_source(self.run_dir, use_working_project=True)

    def test_reuses_only_rsi_playtests_recorded_on_final_project(self):
        source = post.load_source(self.run_dir)
        final_hash = digest(self.project)
        for round_name, project_hash in [('round-001', digest(self.original)),
                                         ('round-002', final_hash)]:
            for profile, scenario in [('core', 'fight'), ('upgrade', 'advanced')]:
                episode = self.run_dir / round_name / 'candidate_playtest' / (
                    'episode-000' if profile == 'core' else 'episode-001')
                episode.mkdir(parents=True)
                write_json(episode / 'trace.json', {'duration_frames': 30, 'events': [
                    {'frame': 1, 'type': 'key_press', 'keycode': 'SPACE'}]})
                write_json(episode / 'result.json', {
                    'player_profile': profile, 'scenario': scenario,
                    'normal_launch': False, 'replay_verified': True,
                    'project_sha256': project_hash,
                    'trace': str(Path('/old-mount/experiment/run') /
                                 (episode / 'trace.json').relative_to(self.run_dir))})
        output = self.root / 'reuse'
        demos = post.reusable_rsi_playtests(source, output, final_hash)
        self.assertEqual([d['file'] for d in demos], ['core.json', 'upgrade.json'])
        self.assertTrue(all('round-002' in d['source_file'] for d in demos))
        self.assertTrue(all(d['source'] == 'rsi_playtest_trace' for d in demos))

    def test_existing_rsi_trace_can_cover_upgrade_without_new_player(self):
        for profile, scenario in [('core', 'fight'), ('upgrade', 'advanced')]:
            episode = self.run_dir / 'round-002/candidate_playtest' / (
                'episode-000' if profile == 'core' else 'episode-001')
            episode.mkdir(parents=True)
            write_json(episode / 'trace.json', {'duration_frames': 30, 'events': [
                {'frame': 1, 'type': 'key_press', 'keycode': 'SPACE'}]})
            write_json(episode / 'result.json', {
                'player_profile': profile, 'scenario': scenario,
                'normal_launch': False, 'replay_verified': True,
                'project_sha256': digest(self.project),
                'trace': str(Path('/old-mount/experiment/run') /
                             (episode / 'trace.json').relative_to(self.run_dir))})
        def assess(row, official, **kwargs):
            return [{'id': rid, 'status': ('not_shown' if row['id'] == 'retained_01'
                    and rid.startswith('RSI_') else 'shown'), 'reason': 'Observed',
                    'evidence': ['frame.png']} for rid in row['requirement_ids']]
        self.fake_assess = assess
        self.execute(mode='incremental')
        result = post.read(self.output / 'result.json')
        self.assertEqual(result['state'], 'ready_for_judging')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.planned, [])
        self.assertGreater(result['reused_rsi_playtest_count'], 0)

    def test_unfinished_or_inconsistent_selection_is_rejected(self):
        for updates in ({'state': 'running'}, {'upgrade_status': 'error'},
                        {'project': self.info['original_project']}):
            with self.subTest(updates=updates):
                write_json(self.run_dir / 'result.json', {**self.info, **updates})
                with self.assertRaises(ValueError):
                    post.load_source(self.run_dir)

    def test_fresh_goals_then_uncovered_only_and_source_stays_unchanged(self):
        before = digest(self.run_dir)
        env_before = os.environ.get('GAMECRAFT_CODEX_PROVIDER')
        with self.assertRaises(CoverageIncompleteError):
            self.execute()
        project = self.output / 'project'
        self.assertEqual(digest(self.run_dir), before)
        self.assertEqual(os.environ.get('GAMECRAFT_CODEX_PROVIDER'), env_before)
        self.assertFalse((project / 'demo_outputs/old.json').exists())
        traces = sorted((project / 'demo_outputs').glob('*.json'))
        self.assertEqual(len(traces), 4)
        self.assertEqual(len(self.planned), 1)
        # Missing an unrelated public feature must not replay both seed profiles.
        self.assertEqual(len(self.calls), 3)  # seed batch, supplemental batch, one retry
        self.assertEqual(len(self.calls[0]['profiles']), 3)
        self.assertNotIn('reuse_source', self.calls[0])
        self.assertNotIn('scenario', post.read(project / 'demo_outputs/03_rsi_entry.json'))
        result = post.read(self.output / 'result.json')
        self.assertEqual(result['state'], 'coverage_incomplete')
        self.assertEqual(result['not_fully_shown'], ['R2'])
        self.assertFalse(result['coverage_complete'])
        self.assertFalse(result['official_benchmark_metric'])
        self.assertEqual(result['source_errors'], self.info['errors'])
        self.assertIsNone(post.read(self.output / 'showcase/manifest.json')['bootstrap_scene_floor_passed'])

    def test_replay_error_does_not_change_training_or_report_ready(self):
        before = digest(self.run_dir)
        def fail(*args):
            raise RuntimeError('replay failed')
        with self.assertRaisesRegex(RuntimeError, 'replay failed'):
            self.execute(fail)
        self.assertEqual(digest(self.run_dir), before)
        self.assertEqual(post.read(self.output / 'status.json')['state'], 'failed')
        self.assertFalse((self.output / 'result.json').exists())

    def test_no_extra_policy_when_all_goals_already_shown(self):
        self.fake_assess = lambda row, official, **kwargs: [
            {'id': rid, 'status': 'shown', 'reason': 'Visible', 'evidence': ['frame.png']}
            for rid in row['requirement_ids']]
        self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.planned, [])
        self.assertTrue(post.read(self.output / 'result.json')['coverage_complete'])

    def test_incremental_checks_old_inputs_before_recording_upgrade(self):
        def assess(row, official, **kwargs):
            return [{'id': rid, 'status': ('not_shown' if row['id'].startswith('retained_')
                    and rid.startswith('RSI_') else 'shown'),
                     'reason': 'Observed', 'evidence': ['frame.png']}
                    for rid in row['requirement_ids']]
        self.fake_assess = assess
        with patch.object(post.models, 'call') as planner:
            project = self.execute(mode='incremental')
        planner.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]['showcase_plan']['profiles']), 3)
        self.assertFalse(self.planned)
        result = post.read(self.output / 'result.json')
        self.assertEqual(result['showcase_mode'], 'incremental')
        self.assertTrue(result['coverage_complete'])
        manifest = post.read(self.output / 'showcase/manifest.json')
        self.assertEqual(manifest['initialization_routes_checked'], 1)
        self.assertTrue((self.output / 'showcase/retained_coverage.json').is_file())
        self.assertLessEqual(len(list((project / 'demo_outputs').glob('*.json'))), 10)

    def test_final_recording_gap_blocks_scoring_even_when_playtest_passed(self):
        def assess(row, official, **kwargs):
            # Selected playtests pass, but final clean collection misses the upgrade.
            return [{'id': rid, 'status': ('not_shown' if
                    row['goal'].startswith('Verify public') and rid == 'RSI_GAMEPLAY' else 'shown'),
                    'reason': 'Result screen only', 'evidence': ['frame.png']}
                    for rid in row['requirement_ids']]
        self.fake_assess = assess
        with self.assertRaises(CoverageIncompleteError):
            self.execute(mode='incremental')
        result = post.read(self.output / 'result.json')
        self.assertEqual(result['not_fully_shown'], ['RSI_GAMEPLAY'])
        self.assertEqual(post.read(self.output / 'status.json')['state'], 'coverage_incomplete')
        self.assertTrue((self.output / 'project/project.godot').is_file())
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.output / 'project')
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.root / 'copied-project', self.output / 'collection')

    def test_incremental_retained_original_needs_no_new_policy(self):
        self.info.update(upgrade_status='retained_original', project=self.info['original_project'])
        write_json(self.run_dir / 'result.json', self.info)
        write_json(self.run_dir / 'final_upgrade_review.json',
                   {'status': 'retained_original', 'selected': 'original'})
        self.fake_assess = lambda row, official, **kw: [
            {'id': rid, 'status': 'shown', 'reason': 'Visible', 'evidence': ['frame.png']}
            for rid in row['requirement_ids']]
        with patch.object(post.models, 'call') as planner:
            project = self.execute(mode='incremental')
        planner.assert_not_called()
        self.assertFalse(self.calls)
        self.assertTrue((project / 'demo_outputs/01_retained_01.json').is_file())
        self.assertEqual(post.read(self.output / 'result.json')['new_demo_count'], 0)

    def test_invalid_or_missing_entry_cannot_be_fabricated(self):
        source = post.load_source(self.run_dir)
        source['scenarios'] = [row for row in source['scenarios'] if row['id'] != 'title']
        with self.assertRaisesRegex(ValueError, 'normal-launch'):
            post.coverage_inputs(source, 6)

    def test_normal_launch_alias_uses_existing_default_entry(self):
        source = post.load_source(self.run_dir)
        source['tasks'][0].update(id='ready', launch='default')
        requirements, proposal = post.coverage_inputs(source, 6)
        self.assertEqual(len(proposal['profiles']), 2)
        self.assertIsNone(proposal['profiles'][0]['scenario'])
        _, tasks = showcase.validate_plan(proposal, source['scenarios'], requirements, 6)
        self.assertEqual(tasks[0]['id'], 'title')
        self.assertEqual(tasks[0]['launch'], 'default')

    def test_large_coverage_is_batched_without_dropping_goals(self):
        ids = [f'R{i}' for i in range(37)]
        sizes = []
        def assessor(row, official, **kwargs):
            sizes.append(len(row['requirement_ids']))
            return [{'id': rid, 'status': 'shown'} for rid in row['requirement_ids']]
        with patch.object(showcase, 'assess', assessor):
            result = post.assess_requirements({'id': 'core', 'requirement_ids': ids}, {})
        self.assertEqual([r['id'] for r in result], ids)
        self.assertEqual(sizes, [8, 8, 8, 8, 5])

    def test_old_route_missing_in_final_is_repaired_from_baseline_evidence(self):
        def assessor(row, official, **kwargs):
            return [{'id': rid, 'status': ('not_shown' if
                    row['id'].startswith('retained_') and rid == 'R2' else 'shown'),
                    'reason': 'Missing retry flow', 'evidence': ['frame.png']}
                    for rid in row['requirement_ids']]
        self.fake_assess = assessor
        def planner(adapter, project, scenarios, requirements, previous, **kwargs):
            self.planned.append(requirements)
            self.assertEqual([r['id'] for r in requirements], ['R2'])
            return {'profiles': [{'id': 'restore_retry', 'goal': 'Show retry',
                    'scenario': None, 'requirement_ids': ['R2'],
                    'expected': 'Retry returns to gameplay', 'max_decisions': 100}]}
        self.fake_plan = planner
        self.execute(mode='incremental')
        baseline = post.read(self.output / 'showcase/baseline_coverage.json')
        self.assertIn('R2', baseline['required_public_ids'])
        self.assertEqual(len(self.planned), 1)
        self.assertEqual(len(self.calls), 1)  # Only the missing retry route is generated.
        result = post.read(self.output / 'result.json')
        self.assertTrue(result['coverage_complete'])
        self.assertIn('R2', result['required_coverage_ids'])

    def test_broken_old_replay_does_not_abort_targeted_replacement(self):
        real = self.fake_official
        def replay(project, record, directory, godot, **kwargs):
            if directory.name == 'retained_01':
                raise RuntimeError('Old input no longer works')
            return real(project, record, directory, godot, **kwargs)
        self.fake_official = replay
        self.fake_assess = lambda row, official, **kw: [
            {'id': rid, 'status': 'shown', 'reason': 'Visible', 'evidence': ['frame.png']}
            for rid in row['requirement_ids']]
        self.execute(mode='incremental')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(post.read(self.output / 'result.json')['coverage_complete'])
        self.assertIn('Old input no longer works',
                      (self.output / 'showcase/retained_replay_errors.json').read_text())

    def test_preexisting_unshown_feature_is_reported_without_impossible_gate(self):
        def planner(adapter, project, scenarios, requirements, previous, **kwargs):
            self.planned.append(requirements)
            return {'profiles': [{'id': 'try_retry', 'goal': 'Show retry',
                    'scenario': None, 'requirement_ids': ['R2'],
                    'expected': 'Retry returns to gameplay', 'max_decisions': 100}]}
        self.fake_plan = planner
        self.execute(mode='incremental')  # R2 is absent in both versions in this fixture.
        result = post.read(self.output / 'result.json')
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['public_requirements_not_shown'], ['R2'])
        self.assertNotIn('R2', result['required_coverage_ids'])
        self.assertEqual(len(self.planned), 1)
        self.assertEqual(len(self.calls), 2)  # One gap policy and one bounded retry.

    def test_compaction_preserves_unique_and_partial_public_coverage(self):
        def clip(name, **facts):
            return {'row': {'id': name}, 'coverage': [
                {'id': rid, 'status': status} for rid, status in facts.items()]}
        clips = [clip('core', core='shown'), clip('duplicate', core='shown'),
                 clip('map', map='shown'), clip('ending', ending='partial'),
                 clip('upgrade', upgrade='shown')]
        selected = compact_choices(clips, 4)
        self.assertEqual([c['row']['id'] for c in selected], ['core', 'map', 'ending', 'upgrade'])
        with self.assertRaises(CoverageIncompleteError):
            compact_choices(clips, 3)

    def test_scoring_does_not_accept_running_collection_or_legacy_incomplete(self):
        self.output.mkdir()
        write_json(self.output / 'configuration.json', {'protocol': 'post-rsi-showcase-v2'})
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.output / 'project')
        write_json(self.output / 'result.json', {'state': 'ready_for_judging',
                   'showcase_mode': 'incremental', 'coverage_complete': False})
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.output / 'project')
        write_json(self.output / 'result.json', {'state': 'ready_for_judging',
                   'showcase_mode': 'incremental', 'coverage_complete': True})
        require_ready_for_scoring(self.output / 'project')
        require_ready_for_scoring(self.original)  # Ordinary Bootstrap scores are unaffected.

    def test_score_only_replays_existing_inputs_without_coverage_judge(self):
        before = digest(self.run_dir)
        self.execute(mode='score_only')
        result = post.read(self.output / 'result.json')
        self.assertEqual(result['state'], 'ready_for_judging')
        self.assertTrue(result['replay_complete'])
        self.assertIsNone(result['coverage_complete'])
        self.assertEqual(result['selected_demo_count'], 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.planned, [])
        self.assertEqual(digest(self.run_dir), before)
        require_ready_for_scoring(self.output / 'project', self.output / 'collection')

    def test_score_only_selection_keeps_rsi_and_spreads_initial_routes(self):
        initial = [{'file': f'{i:02d}.json', 'scenario': None if i == 0 else f's{i}'}
                   for i in range(10)]
        rsi = [{'file': 'core.json', 'scenario': 'core'},
               {'file': 'upgrade.json', 'scenario': 'upgrade'}]
        selected = select_traces(initial, rsi)
        self.assertEqual(len(selected), 10)
        self.assertEqual(selected[0]['file'], '00.json')
        self.assertEqual(selected[-2:], rsi)
        self.assertEqual(selected[-3]['file'], '09.json')

    def test_merged_mode_keeps_all_initialization_routes(self):
        demo_dir = self.original / 'demo_outputs'
        (demo_dir / 'old.json').unlink()
        for index in range(10):
            write_json(demo_dir / f'{index:02d}.json', {
                **({'scenario': 'fight'} if index else {}),
                'duration_frames': 30, 'events': []})
        before = digest(self.run_dir)
        self.execute(mode='merged')
        result = json.loads((self.output / 'result.json').read_text())
        provenance = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(result['showcase_mode'], 'merged')
        self.assertEqual(len(provenance['demos']), 10)
        self.assertEqual([r['original_file'] for r in provenance['demos']],
                         [f'{index:02d}.json' for index in range(10)])
        self.assertTrue(all(r['source'] == 'initialization_trace'
                            for r in provenance['demos']))
        self.assertEqual(digest(self.run_dir), before)

    def test_preserve_mode_keeps_all_routes_without_planning_extra_goals(self):
        demo_dir = self.original / 'demo_outputs'
        (demo_dir / 'old.json').unlink()
        for index in range(10):
            write_json(demo_dir / f'{index:02d}.json', {
                **({'scenario': 'fight'} if index else {}),
                'duration_frames': 30, 'events': []})
        active = 0
        peak = 0
        lock = threading.Lock()
        def scenes(group, official, *, facts=None, **kwargs):
            nonlocal active, peak
            if group['id'].startswith('init_'):
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
            return {'facts': [{'id': 'B1', 'description': 'Original scene',
                               'bootstrap_frame': 'B01',
                               'final_frame': 'F01', 'covered': True}],
                    'passed': True}
        self.execute(mode='preserve', scene_comparator=scenes)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        result = json.loads((self.output / 'result.json').read_text())
        self.assertEqual(len(manifest['demos']), 10)
        self.assertEqual(manifest['repair_results'], [])
        self.assertEqual(manifest['parallelism'], 5)
        self.assertTrue(result['coverage_assessed'])
        self.assertEqual(result['public_requirements_not_shown'], ['R2'])
        self.assertEqual(result['rsi_goals_not_fully_shown'], [])
        self.assertEqual(result['state'], 'ready_for_judging')
        self.assertEqual(self.calls, [])
        self.assertGreaterEqual(peak, 2)

    def test_preserve_mode_repairs_only_a_lost_visible_scene(self):
        def scenes(group, official, *, facts=None, **kwargs):
            covered = facts is not None
            return {'facts': [{'id': 'B1', 'description': 'Original result screen',
                               'bootstrap_frame': 'B01',
                               'final_frame': 'F01' if covered else None,
                               'covered': covered}], 'passed': covered}

        self.execute(mode='preserve', scene_comparator=scenes)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(manifest['demos'][0]['source'],
                         'repaired_initialization_trace')
        self.assertTrue(manifest['repair_results'][0]['accepted'])
        self.assertEqual(len(self.calls), 1)

    def test_preserve_mode_keeps_route_when_baseline_comparison_is_unverified(self):
        def scenes(group, official, *, facts=None, **kwargs):
            if group['id'] == 'init_01':
                return {'facts': [], 'passed': False, 'status': 'unverified',
                        'error': 'Invalid baseline frame reference'}
            return {'facts': [{'id': 'B1', 'description': 'Original scene',
                               'bootstrap_frame': 'B01', 'final_frame': 'F01',
                               'covered': True}], 'passed': True}

        self.execute(mode='preserve', scene_comparator=scenes)
        manifest = post.read(self.output / 'showcase/manifest.json')
        self.assertEqual(len(manifest['demos']), 1)
        self.assertEqual(manifest['demos'][0]['source'], 'initialization_trace')
        self.assertEqual(manifest['demos'][0]['baseline_scene_status'], 'unverified')
        self.assertEqual(manifest['repair_results'][0]['acceptance_reason'], 'unverified')
        self.assertEqual(manifest['repair_results'][0]['attempts'], [])
        self.assertEqual(self.calls, [])
        coverage = post.read(self.output / 'collection/coverage.json')
        self.assertEqual(coverage['route_regressions'][0]['requirement_id'],
                         'scene:unverified')

    def test_preserve_mode_scores_after_two_failed_repairs_with_missing_coverage_recorded(self):
        def scenes(group, official, *, facts=None, **kwargs):
            return {'facts': [{'id': 'B1', 'description': 'Original scene',
                               'bootstrap_frame': 'B01', 'final_frame': None,
                               'covered': False}], 'passed': False}

        self.execute(mode='preserve', scene_comparator=scenes)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(manifest['demos'][0]['source'], 'initialization_trace')
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(manifest['repair_results'][0]['accepted'])
        self.assertFalse(manifest['repair_results'][0]['scene_preserved'])
        self.assertEqual(manifest['repair_results'][0]['acceptance_reason'],
                         'original_more_visible')
        self.assertEqual(len(manifest['repair_results'][0]['attempts']), 2)
        self.assertEqual(post.read(self.output / 'result.json')['state'], 'coverage_incomplete')
        self.assertFalse(post.read(self.output / 'result.json')['coverage_complete'])
        require_ready_for_scoring(self.output / 'project')

    def test_preserve_mode_keeps_best_partial_repair(self):
        def scenes(group, official, *, facts=None, **kwargs):
            name = group['id']
            count = 2 if 'attempt-01' in name else 1
            rows = [{'id': f'B{i}', 'description': f'Scene {i}',
                     'bootstrap_frame': f'B0{i}', 'final_frame': f'F0{i}' if i <= count else None,
                     'covered': i <= count} for i in range(1, 4)]
            return {'facts': rows, 'passed': False}

        self.execute(mode='preserve', scene_comparator=scenes)
        manifest = post.read(self.output / 'showcase/manifest.json')
        report = manifest['repair_results'][0]
        self.assertEqual(manifest['demos'][0]['source'], 'repaired_initialization_trace')
        self.assertIn('attempt-01', manifest['demos'][0]['source_trace'])
        self.assertEqual(report['covered_scene_count'], 2)
        self.assertEqual(report['acceptance_reason'], 'most_visible_facts')
        self.assertEqual(len(report['attempts']), 2)

    def test_preserve_mode_reuses_final_game_playtest_when_it_adds_rsi_evidence(self):
        episode = self.run_dir / 'round-002/candidate_playtest/episode-000'
        episode.mkdir(parents=True)
        write_json(episode / 'trace.json', {'duration_frames': 30, 'events': [
            {'frame': 1, 'type': 'key_press', 'keycode': 'SPACE'}]})
        write_json(episode / 'result.json', {
            'player_profile': 'core', 'scenario': None, 'normal_launch': True,
            'replay_verified': True, 'project_sha256': digest(self.project),
            'trace': str(Path('/old-mount/experiment/run') /
                         (episode / 'trace.json').relative_to(self.run_dir))})

        def assess(row, official, **kwargs):
            return [{'id': rid, 'status': ('not_shown' if
                    row['id'].endswith('_original') and rid.startswith('RSI_') else 'shown'),
                    'reason': 'Observed', 'evidence': ['frame.png']}
                    for rid in row['requirement_ids']]

        self.execute(mode='preserve', assessor=assess)
        result = post.read(self.output / 'result.json')
        manifest = post.read(self.output / 'showcase/manifest.json')
        self.assertEqual(manifest['demos'][0]['source'], 'rsi_playtest_trace')
        self.assertIn('RSI_GAMEPLAY', manifest['initially_missing_rsi_goals'])
        self.assertTrue((self.output / 'showcase/initial_coverage.json').is_file())
        self.assertEqual(result['reused_rsi_playtest_count'], 1)
        self.assertEqual(result['rsi_goals_not_fully_shown'], [])
        self.assertEqual(self.calls, [])

    def test_preserve_mode_uses_missing_facts_in_second_repair(self):
        attempts = 0
        def scenes(group, official, *, facts=None, **kwargs):
            nonlocal attempts
            if group['id'].startswith('preserve_'):
                attempts += 1
            covered = facts is not None and attempts >= 2
            return {'facts': [{'id': 'B1', 'description': 'Show the result screen',
                               'bootstrap_frame': 'B01',
                               'final_frame': 'F01' if covered else None,
                               'covered': covered}], 'passed': covered}

        self.execute(mode='preserve', scene_comparator=scenes)
        report = post.read(self.output / 'showcase/repair_results.json')[0]
        self.assertTrue(report['scene_preserved'])
        self.assertEqual(len(report['attempts']), 2)
        self.assertEqual(len(self.calls), 2)
        second = self.calls[1]['showcase_plan']['profiles'][0]
        self.assertEqual(second['missing_scene_facts'][0]['id'], 'B1')
        require_ready_for_scoring(self.output / 'project')

    def test_merged_mode_rewrites_route_only_with_preserved_coverage(self):
        def assess(row, official, **kwargs):
            baseline = row['id'].startswith('init_')
            return [{'id': rid, 'status': ('not_shown' if rid == 'R2' or
                     (baseline and rid.startswith('RSI_')) else
                     'partial' if rid.startswith('RSI_') else 'shown'),
                     'reason': 'Observed', 'evidence': []}
                    for rid in row['requirement_ids']]

        def plan(project, routes, requirements, choices, missing, **kwargs):
            return {'assignments': [{'source_file': 'old.json',
                     'goal_ids': sorted(missing), 'goal': 'Show core and upgrade in this route'}]}

        self.execute(mode='merged', assessor=assess, merge_planner=plan)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(len(manifest['demos']), 1)
        self.assertEqual(manifest['demos'][0]['source'], 'merged_rsi_trace')
        self.assertEqual(manifest['demos'][0]['original_file'], 'old.json')
        self.assertTrue(manifest['merge_results'][0]['accepted'])
        self.assertEqual(manifest['demos'][0]['original_coverage'][0]['status'], 'shown')
        result = json.loads((self.output / 'result.json').read_text())
        self.assertEqual(result['state'], 'ready_for_judging')
        self.assertTrue(result['rsi_goals_not_fully_shown'])

    def test_merged_mode_keeps_original_when_new_route_loses_old_evidence(self):
        def assess(row, official, **kwargs):
            rewriting = row['id'].startswith('merge_')
            return [{'id': rid,
                     'status': ('partial' if rid == 'R1' and rewriting else
                                'not_shown' if rid == 'R2' or
                                (rid.startswith('RSI_') and not rewriting) else 'shown'),
                     'reason': 'Observed', 'evidence': []}
                    for rid in row['requirement_ids']]

        def plan(project, routes, requirements, choices, missing, **kwargs):
            return {'assignments': [{'source_file': 'old.json',
                     'goal_ids': sorted(missing), 'goal': 'Show RSI goals'}]}

        self.execute(mode='merged', assessor=assess, merge_planner=plan)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(manifest['demos'][0]['source'], 'initialization_trace')
        self.assertFalse(manifest['merge_results'][0]['accepted'])
        self.assertEqual(json.loads((self.output / 'result.json').read_text())['state'],
                         'ready_for_judging')

    def test_merged_mode_repairs_lost_scene_using_archived_program(self):
        archive = self.run_dir / 'initial/final_recordings'
        archive.mkdir()
        policy = archive / 'old.py'
        policy.write_text('ORIGINAL_ROUTE_POLICY = True\n')
        from gamecraft_evaluation.trained_demos import file_hash
        write_json(archive / 'manifest.json', {'groups': {'core': {'episodes': [
            {'scenario': 'title', 'normal_launch': True, 'files': {'player_policy': {
                'path': 'old.py', 'sha256': file_hash(policy)}}}]}}})
        comparisons = []
        def scenes(group, official, *, facts=None, **kwargs):
            comparisons.append(group['id'])
            covered = facts is not None or group['id'].startswith('final_')
            return {'facts': [{'id': 'B1', 'description': 'Visible original scene',
                               'bootstrap_frame': 'B01',
                               'final_frame': 'F01' if covered else None,
                               'covered': covered}], 'passed': covered}

        self.execute(mode='merged', scene_comparator=scenes)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(manifest['demos'][0]['source'], 'merged_rsi_trace')
        self.assertTrue(manifest['merge_results'][0]['lost_scenes'])
        self.assertTrue(any('ORIGINAL_ROUTE_POLICY = True' in json.dumps(context)
                            for context in self.calls))
        self.assertIn('final_01_old', comparisons)

    def test_merged_mode_uses_initialization_game_as_public_baseline(self):
        def assess(row, official, **kwargs):
            video = str(official.get('video', ''))
            return [{'id': rid,
                     'status': ('shown' if '/baseline/' in video or
                                row['id'].startswith('merge_') else 'partial')
                     if rid == 'R1' else
                     ('partial' if row['id'].startswith('merge_') else 'not_shown'),
                     'reason': 'Observed', 'evidence': []}
                    for rid in row['requirement_ids']]

        def plan(project, routes, requirements, choices, missing, **kwargs):
            return {'assignments': [{'source_file': 'old.json',
                     'goal_ids': sorted(missing), 'goal': 'Show RSI goals'}]}
        with self.assertRaises(CoverageIncompleteError):
            self.execute(mode='merged', assessor=assess, merge_planner=plan)
        manifest = json.loads((self.output / 'showcase/manifest.json').read_text())
        self.assertEqual(manifest['demos'][0]['original_coverage'][0]['status'], 'shown')
        self.assertTrue(manifest['merge_results'][0]['lost_public'])
        self.assertEqual(manifest['demos'][0]['source'], 'merged_rsi_trace')

    def test_merge_plan_rejects_missing_route_and_weakened_evidence(self):
        with self.assertRaises(ValueError):
            validate_assignments({'assignments': []}, [{'file': 'old.json'}], {'RSI_CORE'})
        self.assertFalse(preserves([{'id': 'R1', 'status': 'shown'}],
                                   [{'id': 'R1', 'status': 'partial'}], {'R1'}))

    def test_merged_coverage_retries_one_omitted_response(self):
        calls = []
        def assessor(row, official, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError('Final showcase coverage assessment omitted planned requirements')
            return [{'id': 'R1', 'status': 'shown'}]
        self.assertEqual(assess_with_retry(assessor, {}, {}),
                         [{'id': 'R1', 'status': 'shown'}])
        self.assertEqual(len(calls), 2)

    def test_merged_coverage_splits_persistent_omission(self):
        seen = []
        def assessor(row, official, **kwargs):
            ids = row['requirement_ids']
            seen.append(ids)
            if len(ids) > 2:
                raise ValueError('Final showcase coverage assessment omitted planned requirements')
            return [{'id': rid, 'status': 'shown'} for rid in ids]
        ids = ['R1', 'R2', 'R3', 'R4']
        result = assess_with_retry(assessor, {'requirement_ids': ids}, {})
        self.assertEqual([item['id'] for item in result], ids)
        self.assertEqual(seen, [ids, ids, ids[:2], ids[2:]])

    def test_score_only_scoring_gate_requires_completed_replay(self):
        self.output.mkdir()
        write_json(self.output / 'configuration.json', {'protocol': 'post-rsi-score-only-v1'})
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.output / 'project')
        write_json(self.output / 'result.json', {'showcase_mode': 'score_only',
                   'state': 'ready_for_judging', 'replay_complete': False,
                   'coverage_assessed': False})
        with self.assertRaises(CoverageIncompleteError):
            require_ready_for_scoring(self.output / 'project')


if __name__ == '__main__':
    unittest.main()
