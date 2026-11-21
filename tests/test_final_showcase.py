import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase as showcase


class FinalShowcaseTests(unittest.TestCase):
    def test_official_replay_cache_reuses_identical_trace_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'game'
            project.mkdir()
            (project / 'project.godot').write_text('[application]\n')
            trace = root / 'trace.json'
            write_json(trace, {'duration_frames': 30, 'events': []})
            record = {'trace': str(trace), 'scenario': 'entry', 'normal_launch': True}
            calls = []

            def official(_project, source, directory, _godot, **kwargs):
                calls.append(source['trace'])
                directory.mkdir()
                replay = directory / 'demo.json'
                write_json(replay, showcase.public_trace(source))
                video = directory / 'official.mp4'
                video.write_bytes(b'official-video')
                frame = directory / 'frames' / 'frame_0001.png'
                frame.parent.mkdir()
                frame.write_bytes(b'frame')
                return {'trace': str(replay), 'video': str(video),
                        'frames': [str(frame)], 'duration_seconds': 1.0,
                        'trace_seconds': 0.5}

            kwargs = dict(demo_name='entry', project_hash=digest(project),
                          cache_root=root / 'cache')
            with patch.object(showcase, '_official_record', side_effect=official):
                first = showcase._cached_official_record(project, record, root / 'one',
                                                          'godot', **kwargs)
                second = showcase._cached_official_record(project, record, root / 'two',
                                                           'godot', **kwargs)
                self.assertFalse(first['cache_hit'])
                self.assertTrue(second['cache_hit'])
                self.assertEqual(len(calls), 1)
                write_json(trace, {'duration_frames': 30, 'events': [
                    {'frame': 1, 'type': 'key_press', 'keycode': 'SPACE'}]})
                third = showcase._cached_official_record(project, record, root / 'three',
                                                          'godot', **kwargs)
                self.assertFalse(third['cache_hit'])
                self.assertEqual(len(calls), 2)

    def test_plan_can_use_new_profiles_and_reports_unassigned_requirements(self):
        scenarios = [{'id': 'entry', 'launch': 'default'},
                     {'id': 'later', 'launch': 'scenario'}]
        requirements = [{'id': 'R1', 'dynamic': True},
                        {'id': 'R2', 'dynamic': True},
                        {'id': 'R3', 'dynamic': False}]
        proposal = {'profiles': [
            {'id': 'start', 'goal': 'Show the normal entry and core play',
             'scenario': None, 'requirement_ids': ['R1'],
             'expected': 'Title followed by play', 'max_decisions': 60},
            {'id': 'late', 'goal': 'Show later progression',
             'scenario': 'later', 'requirement_ids': ['R1'],
             'expected': 'A later state appears', 'max_decisions': 80}]}
        result, tasks = showcase.validate_plan(proposal, scenarios, requirements, 6)
        self.assertEqual([task['profile'] for task in tasks],
                         ['showcase/start', 'showcase/late'])
        self.assertEqual([task['launch'] for task in tasks], ['default', 'scenario'])
        self.assertEqual(result['unassigned_requirements'], ['R2'])
        bad = json.loads(json.dumps(proposal))
        bad['profiles'][0]['max_decisions'] = 101
        with self.assertRaises(ValueError):
            showcase.validate_plan(bad, scenarios, requirements, 6)

    def test_final_export_uses_recorded_inputs_and_targeted_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'game'
            project.mkdir()
            (project / 'project.godot').write_text('[application]\n')
            requirements = [{'id': 'R1', 'dynamic': True}]
            scenarios = [{'id': 'entry', 'launch': 'default'}]
            plan = {'profiles': [{'id': 'core', 'goal': 'Show the result',
                                  'scenario': None, 'requirement_ids': ['R1'],
                                  'expected': 'Result remains visible',
                                  'max_decisions': 20}]}
            calls = []

            def factory(project_path, policy_path, **kwargs):
                calls.append(kwargs['playtest_context'])
                policy_path.write_text('def act(state, recent):\n    return {"action": "wait", "reason": "show"}\n')
                return lambda *args, **kw: {'action': 'wait', 'reason': 'show'}

            def rollout(project_path, directory, skill, decide, **kwargs):
                directory.mkdir(parents=True)
                attempt = len(calls)
                trace = directory / 'trace.json'
                write_json(trace, {'duration_frames': 30,
                                   'events': [{'frame': 1, 'type': 'key_press',
                                               'keycode': 'SPACE'}] if attempt == 1 else
                                             [{'frame': 2, 'type': 'key_press',
                                               'keycode': 'SPACE'}]})
                video = directory / 'video.mp4'
                video.write_bytes(b'video')
                return {'project_sha256': digest(project_path), 'replay_verified': True,
                        'decisions': [{'frame': 0, 'action': 'wait', 'reason': 'show'}],
                        'scenario': kwargs['scenario'],
                        'normal_launch': kwargs['normal_launch'],
                        'trace': str(trace), 'video': str(video)}

            def official(project_path, record, directory, godot, **kwargs):
                directory.mkdir(parents=True)
                trace = directory / 'demo.json'
                write_json(trace, showcase.public_trace(record))
                video = directory / 'official.mp4'
                video.write_bytes(b'official-video')
                frame_dir = directory / 'frames'
                frame_dir.mkdir()
                frame = frame_dir / 'frame_0001.png'
                frame.write_bytes(b'frame')
                return {'trace': str(trace), 'video': str(video),
                        'frames': [str(frame)], 'duration_seconds': 1.0}

            verdicts = iter([
                [{'id': 'R1', 'status': 'not_shown', 'reason': 'No result', 'evidence': []}],
                [{'id': 'R1', 'status': 'shown', 'reason': 'Result visible',
                  'evidence': ['frame_0001.png']}],
            ])
            with patch.object(showcase, '_official_record', side_effect=official):
                manifest = showcase.run(project, root / 'showcase',
                    adapter=SimpleNamespace(), scenarios=scenarios,
                    requirements=requirements, previous=[], skill='',
                    godot='godot', model='test', backend='local', step_frames=6,
                    policy_factory=factory, rollout=rollout,
                    planner=lambda *a, **kw: plan,
                    assessor=lambda *a, **kw: next(verdicts))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]['phase'], 'final_showcase')
            self.assertEqual(len(calls[1]['profiles']), 1)
            self.assertEqual(len(calls[1]['showcase_plan']['profiles']), 1)
            self.assertEqual(manifest['attempts'][0]['chosen_attempt'], 2)
            self.assertEqual(manifest['coverage'][0]['status'], 'shown')
            self.assertEqual(manifest['requirement_coverage']['R1']['status'], 'shown')
            exported = json.loads((root / 'showcase/demo_outputs/01_core.json').read_text())
            self.assertEqual(exported['events'][0]['frame'], 2)
            self.assertNotIn('scenario', exported)
            showcase.archive(root / 'showcase', root / 'archive', manifest)
            self.assertTrue((root / 'archive' / manifest['demos'][0]['archived_video']).is_file())

    def test_archive_gives_every_scored_video_a_unique_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'showcase'
            source.mkdir()
            (source / 'plan.json').write_text('{}')
            entries = []
            for index in (1, 2):
                directory = source / 'official' / f'{index:02d}-attempt-01'
                directory.mkdir(parents=True)
                video = directory / 'official.mp4'
                video.write_bytes(f'video-{index}'.encode())
                policy = source / f'policy-{index}.py'
                policy.write_text('def act(state, recent): return {}\n')
                entries.append({'profile': f'profile{index}',
                                'official_video': str(video),
                                'official_video_sha256': showcase._sha(video),
                                'source_policy': str(policy)})
            manifest = {'demos': entries}
            showcase.archive(source, root / 'package', manifest)
            names = [Path(entry['archived_video']).stem for entry in entries]
            self.assertEqual(len(names), len(set(names)))

    def test_invalid_frame_citation_cannot_count_as_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / 'frame_0001.png'
            frame.write_bytes(b'frame')
            answer = {'coverage': [{'id': 'R1', 'status': 'shown',
                                    'reason': 'Claimed visual effect',
                                    'evidence': ['frame_9999.png']}]}
            fake_llm = ModuleType('llm')
            fake_llm.img = lambda *a, **kw: {'type': 'input_image', 'image_url': 'data:'}
            with patch.dict(sys.modules, {'llm': fake_llm}), \
                 patch.object(showcase.models, 'call', return_value=answer):
                found = showcase.assess(
                    {'requirement_ids': ['R1']},
                    {'frames': [str(frame)], 'duration_seconds': 1},
                    backend='local', model='test', requirements=[{'id': 'R1'}])
            self.assertEqual(found[0]['status'], 'unverified')
            self.assertEqual(found[0]['evidence'], [])

    def test_bootstrap_comparison_requires_real_final_frame_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / 'bootstrap.mp4'
            baseline.write_bytes(b'video')
            before = root / 'before.png'
            after = root / 'after.png'
            before.write_bytes(b'frame')
            after.write_bytes(b'frame')
            group = {'id': 'group_one', 'goal': 'Push a crate',
                     'bootstrap_video': str(baseline)}
            official = {'video': str(root / 'final.mp4'),
                        'frames': [str(after)]}
            fake_llm = ModuleType('llm')
            fake_llm.img = lambda *a, **kw: {'type': 'input_image', 'image_url': 'data:'}
            benchmark = ModuleType('gamecraft_bench')
            benchmark.__path__ = []
            verifier = ModuleType('gamecraft_bench.verifier')
            verifier.__path__ = []
            score = ModuleType('gamecraft_bench.verifier.score')
            score._sample_frames = lambda *a, **kw: [before]
            answers = [
                {'facts': [{'id': 'B1', 'description': 'Crate moves',
                            'bootstrap_frame': 'B01', 'final_frame': 'F99',
                            'covered': True}]},
                {'facts': [{'id': 'B1', 'description': 'Changed description',
                            'bootstrap_frame': 'B01', 'final_frame': 'F01',
                            'covered': True}]},
            ]
            modules = {'llm': fake_llm, 'gamecraft_bench': benchmark,
                       'gamecraft_bench.verifier': verifier,
                       'gamecraft_bench.verifier.score': score}
            with patch.dict(sys.modules, modules), \
                 patch.object(showcase.subprocess, 'run', return_value=SimpleNamespace(
                     stdout='{"format":{"duration":"2"}}')), \
                 patch.object(showcase.models, 'call', side_effect=answers):
                first = showcase.compare_baseline(group, official, backend='local',
                    model='test', output=root / 'first')
                second = showcase.compare_baseline(group, official, backend='local',
                    model='test', output=root / 'second', facts=first['facts'])
            self.assertFalse(first['passed'])
            self.assertIsNone(first['facts'][0]['final_frame'])
            self.assertTrue(second['passed'])
            self.assertEqual(second['facts'][0]['description'], 'Crate moves')

    def test_bootstrap_comparison_retries_invalid_frame_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / 'bootstrap.mp4'
            baseline.write_bytes(b'video')
            before = root / 'before.png'
            after = root / 'after.png'
            before.write_bytes(b'frame')
            after.write_bytes(b'frame')
            fake_llm = ModuleType('llm')
            fake_llm.img = lambda *a, **kw: {'type': 'input_image', 'image_url': 'data:'}
            benchmark = ModuleType('gamecraft_bench')
            benchmark.__path__ = []
            verifier = ModuleType('gamecraft_bench.verifier')
            verifier.__path__ = []
            score = ModuleType('gamecraft_bench.verifier.score')
            score._sample_frames = lambda *a, **kw: [before]
            answers = [{'facts': [{'id': 'B1', 'description': 'Door opens',
                         'bootstrap_frame': label, 'final_frame': 'F01',
                         'covered': True}]} for label in ('B99', 'B01')]
            modules = {'llm': fake_llm, 'gamecraft_bench': benchmark,
                       'gamecraft_bench.verifier': verifier,
                       'gamecraft_bench.verifier.score': score}
            with patch.dict(sys.modules, modules), \
                 patch.object(showcase.subprocess, 'run', return_value=SimpleNamespace(
                     stdout='{"format":{"duration":"2"}}')), \
                 patch.object(showcase.models, 'call', side_effect=answers) as call:
                result = showcase.compare_baseline(
                    {'id': 'group', 'goal': 'Open the door',
                     'bootstrap_video': str(baseline)},
                    {'video': str(root / 'final.mp4'), 'frames': [str(after)]},
                    backend='local', model='test', output=root / 'comparison')
            self.assertTrue(result['passed'])
            self.assertEqual(call.call_count, 2)

    def test_bootstrap_scene_floor_keeps_best_clip_and_reports_missing_fact(self):
        for preserved in (True, False):
            with self.subTest(preserved=preserved), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / 'game'
                project.mkdir()
                (project / 'project.godot').write_text('[application]\n')

                def record(name):
                    path = root / name
                    path.mkdir()
                    trace = path / 'trace.json'
                    write_json(trace, {'duration_frames': 30, 'events': []})
                    video = path / 'video.mp4'
                    video.write_bytes(b'video')
                    policy = path / 'policy.py'
                    policy.write_text('def act(state, recent): return {}\n')
                    return {'scenario': 'entry', 'normal_launch': True,
                            'project_sha256': digest(project), 'replay_verified': True,
                            'trace': str(trace), 'video': str(video),
                            'player_policy': str(policy)}

                planned = record('planned')
                final = record('final')
                retry = record('retry')
                retry_two = record('retry_two')
                baseline_video = root / 'bootstrap.mp4'
                baseline_video.write_bytes(b'bootstrap-video')
                floor = [{'id': 'entry_group', 'goal': 'Show the visible entry flow',
                          'scenario': 'entry', 'normal_launch': True,
                          'bootstrap_video': str(baseline_video), 'final_record': final}]
                proposal = {'profiles': [{'id': 'extra', 'goal': 'Show another detail',
                    'scenario': None, 'requirement_ids': ['R1'],
                    'expected': 'Show detail', 'max_decisions': 20}]}
                limits = []

                def planner(*args, **kwargs):
                    limits.append(kwargs['max_demos'])
                    return proposal

                def official(_project, source, directory, _godot, **kwargs):
                    directory.mkdir(parents=True)
                    trace = directory / 'demo.json'
                    write_json(trace, showcase.public_trace(source))
                    video = directory / 'official.mp4'
                    video.write_bytes(b'official-video')
                    return {'trace': str(trace), 'video': str(video),
                            'frames': [], 'duration_seconds': 1.0}

                def comparator(group, video, **kwargs):
                    return {'group': group['id'], 'passed': preserved,
                            'facts': [{'id': 'B1', 'description': 'Entry visible',
                                       'bootstrap_frame': 'B01',
                                       'final_frame': 'F01' if preserved else None,
                                       'covered': preserved}]}

                with patch.object(showcase, 'play_version',
                                  side_effect=[[planned], [retry], [retry_two]]) as play, \
                     patch.object(showcase, '_official_record', side_effect=official):
                    options = dict(adapter=SimpleNamespace(),
                        scenarios=[{'id': 'entry', 'launch': 'default'}],
                        requirements=[{'id': 'R1', 'dynamic': True}], previous=[],
                        skill='', godot='godot', model='test', backend='local',
                        step_frames=6, policy_factory=None, rollout=None,
                        planner=planner, assessor=lambda *a, **kw: [
                            {'id': 'R1', 'status': 'shown', 'reason': 'visible',
                             'evidence': ['frame_0001.png']}],
                        baseline_groups=floor, baseline_comparator=comparator)
                    if preserved:
                        result = showcase.run(project, root / 'showcase', **options)
                        self.assertTrue(result['bootstrap_scene_floor_passed'])
                        self.assertEqual(len(result['demos']), 1)
                        self.assertEqual(limits, [])
                        self.assertEqual(play.call_count, 0)
                        self.assertEqual(result['demos'][0]['scenario'], 'entry')
                    else:
                        result = showcase.run(project, root / 'showcase', **options)
                        self.assertFalse(result['bootstrap_scene_floor_passed'])
                        self.assertEqual(len(result['demos']), 1)
                        self.assertEqual(play.call_count, 2)
                        self.assertEqual(result['bootstrap_coverage'][0]['missing_fact_ids'], ['B1'])

    def test_existing_video_limits_new_plan_to_uncovered_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'game'
            project.mkdir()
            (project / 'project.godot').write_text('[application]\n')
            trace = root / 'trace.json'
            write_json(trace, {'duration_frames': 30, 'events': []})
            video = root / 'existing.mp4'
            video.write_bytes(b'video')
            policy = root / 'policy.py'
            policy.write_text('def act(state, recent): return {}\n')
            existing = {'scenario': 'entry', 'normal_launch': True,
                        'project_sha256': digest(project), 'replay_verified': True,
                        'trace': str(trace), 'video': str(video),
                        'player_policy': str(policy)}
            planned = []

            def planner(_adapter, _project, _scenarios, requirements, previous, **kwargs):
                planned.append((requirements, previous, kwargs))
                return {'profiles': [{'id': 'missing', 'goal': 'Show R2',
                    'scenario': None, 'requirement_ids': ['R2'],
                    'expected': 'R2 appears', 'max_decisions': 20}]}

            def official(_project, source, directory, _godot, **kwargs):
                directory.mkdir(parents=True)
                demo = directory / 'demo.json'
                write_json(demo, showcase.public_trace(source))
                replay = directory / 'official.mp4'
                replay.write_bytes(b'official')
                return {'trace': str(demo), 'video': str(replay),
                        'frames': [], 'duration_seconds': 1.0}

            with patch.object(showcase, 'play_version', return_value=[existing]) as player, \
                 patch.object(showcase, '_official_record', side_effect=official):
                manifest = showcase.run(project, root / 'showcase',
                    adapter=SimpleNamespace(),
                    scenarios=[{'id': 'entry', 'launch': 'default'}],
                    requirements=[{'id': 'R1', 'dynamic': True},
                                  {'id': 'R2', 'dynamic': True}],
                    previous=[], skill='learned strategy', godot='godot',
                    model='test', backend='local', step_frames=6,
                    policy_factory=None, rollout=None, planner=planner,
                    assessor=lambda row, *_args, **_kwargs: [
                        {'id': rid,
                         'status': ('not_shown' if rid == 'R2' and row.get('bootstrap_group')
                                    else 'shown'),
                         'reason': 'visible', 'evidence': ['frame.png']}
                        for rid in row['requirement_ids']],
                    baseline_groups=[{'id': 'entry_group', 'goal': 'Show entry',
                        'scenario': 'entry', 'normal_launch': True,
                        'bootstrap_video': str(video), 'final_record': existing}],
                    baseline_comparator=lambda *_args, **_kwargs:
                        {'passed': True, 'facts': []})
            self.assertEqual([row['id'] for row in planned[0][0]], ['R2'])
            self.assertEqual(planned[0][2]['uncovered_requirements'], {'R2'})
            self.assertEqual(player.call_count, 1)
            self.assertEqual(player.call_args.kwargs['skill'], 'learned strategy')
            self.assertEqual(manifest['requirement_coverage']['R1']['status'], 'shown')
            self.assertEqual(manifest['requirement_coverage']['R2']['status'], 'shown')

    def test_complementary_baseline_clips_replace_one_extra_within_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'game'
            project.mkdir()
            (project / 'project.godot').write_text('[application]\n')

            def record(name):
                directory = root / name
                directory.mkdir()
                trace = directory / 'trace.json'
                write_json(trace, {'duration_frames': 30, 'events': []})
                video = directory / 'video.mp4'
                video.write_bytes(b'video')
                policy = directory / 'policy.py'
                policy.write_text('def act(state, recent): return {}\n')
                return {'scenario': 'entry', 'normal_launch': True,
                        'project_sha256': digest(project), 'replay_verified': True,
                        'trace': str(trace), 'video': str(video),
                        'player_policy': str(policy)}

            planned = [record(f'planned_{i}') for i in range(6)]
            first = record('first')
            second = record('second')
            third = record('third')
            fourth = record('fourth')
            baseline = root / 'bootstrap.mp4'
            baseline.write_bytes(b'bootstrap')
            profiles = [{'id': f'extra_{i}', 'goal': f'Goal {i}', 'scenario': None,
                         'requirement_ids': [f'R{i}'], 'expected': 'Show result',
                         'max_decisions': 20} for i in range(8)]

            def official(_project, source, directory, _godot, **kwargs):
                directory.mkdir(parents=True)
                trace = directory / 'demo.json'
                write_json(trace, showcase.public_trace(source))
                video = directory / 'official.mp4'
                video.write_bytes(b'official')
                return {'trace': str(trace), 'video': str(video),
                        'frames': [], 'duration_seconds': 1.0}

            def comparator(group, video, **kwargs):
                retry = kwargs.get('facts') is not None
                flags = (False, True) if retry else (True, False)
                return {'group': group['id'], 'passed': False,
                        'facts': [{'id': f'B{i}', 'description': f'Fact {i}',
                                   'bootstrap_frame': f'B0{i}',
                                   'final_frame': f'F0{i}' if covered else None,
                                   'covered': covered}
                                  for i, covered in enumerate(flags, 1)]}

            with patch.object(showcase, 'play_version', side_effect=[[second], [fourth], planned]), \
                 patch.object(showcase, '_official_record', side_effect=official):
                manifest = showcase.run(project, root / 'showcase',
                    adapter=SimpleNamespace(), scenarios=[{'id': 'entry', 'launch': 'default'}],
                    requirements=[{'id': f'R{i}', 'dynamic': True} for i in range(8)],
                    previous=[], skill='', godot='godot', model='test', backend='local',
                    step_frames=6, policy_factory=None, rollout=None,
                    planner=lambda *a, **kw: {'profiles': profiles[:kw['max_demos']]},
                    assessor=lambda row, *a, **kw: [
                        {'id': rid,
                         'status': 'not_shown' if row.get('bootstrap_group') else 'shown',
                         'reason': 'missing' if row.get('bootstrap_group') else 'visible',
                         'evidence': [] if row.get('bootstrap_group') else ['frame.png']}
                        for rid in row['requirement_ids']],
                    baseline_groups=[{'id': 'entry_group', 'goal': 'Show both facts',
                        'scenario': 'entry', 'normal_launch': True,
                        'bootstrap_video': str(baseline), 'final_record': first},
                        {'id': 'entry_group_two', 'goal': 'Show both facts',
                         'scenario': 'entry', 'normal_launch': True,
                         'bootstrap_video': str(baseline), 'final_record': third}],
                    baseline_comparator=comparator)
            names = [item['profile'] for item in manifest['demos']]
            self.assertEqual(len(names), 10)
            self.assertNotIn('extra_7', names)
            self.assertNotIn('extra_6', names)
            self.assertIn('bootstrap_entry_group_01', names)
            self.assertIn('bootstrap_entry_group_02', names)
            self.assertIn('bootstrap_entry_group_two_01', names)
            self.assertIn('bootstrap_entry_group_two_02', names)
            self.assertTrue(manifest['bootstrap_scene_floor_passed'])
            self.assertEqual(manifest['bootstrap_coverage'][0]['selected_attempts'], [1, 2])
            self.assertEqual(manifest['bootstrap_coverage'][1]['selected_attempts'], [1, 2])

    def test_three_baseline_clips_can_cover_distinct_visible_facts(self):
        candidates = []
        for index, visible in enumerate(('B1', 'B2', 'B3')):
            candidates.append({'record': {'index': index}, 'comparison': {'facts': [
                {'id': fact, 'covered': fact == visible}
                for fact in ('B1', 'B2', 'B3')]}})
        chosen = showcase._covering_baseline_candidates(candidates)
        self.assertEqual([item['record']['index'] for item in chosen], [0, 1, 2])

    def test_unreachable_baseline_fact_keeps_best_evidence(self):
        candidates = []
        for index, visible in enumerate(({'B1'}, {'B1', 'B2'}, {'B2'})):
            candidates.append({'record': {'index': index}, 'comparison': {'facts': [
                {'id': fact, 'covered': fact in visible}
                for fact in ('B1', 'B2', 'B3')]}})
        self.assertIsNone(showcase._covering_baseline_candidates(candidates))
        chosen = showcase._best_effort_baseline_candidates(candidates)
        self.assertEqual([item['record']['index'] for item in chosen], [1])


if __name__ == '__main__':
    unittest.main()
