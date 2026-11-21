"""Offline orchestration tests: no Codex, H3, Docker or benchmark tests are invoked."""
import argparse
import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gameasg import contracts as c
from gameasg.__main__ import write_json, read_json, digest
from gameasg.html_assets import stage_materials, bundle_html, verify_bundle
from gameasg.initialization import Engine, version_hash
from gameasg.services import sample_indices

DOCS = {'target.md': 'Build a game with start, growth and retry.',
        'game-spec.md': 'Start the game. Grow by collecting. Retry after failure.',
        'tdd.md': 'Use `fresh_level` as a legal precondition.'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhFAAAAAASUVORK5CYII=')


def plan():
    return dict(style='Bright painted garden.', procedural_visuals='Code draws UI and gameplay.', assets=[],
        requirements=[dict(id=k, description=k, source_document='game-spec.md', source_quote=q, acceptance=q)
                      for k, q in [('start', 'Start the game.'), ('grow', 'Grow by collecting.'), ('retry', 'Retry after failure.')]],
        groups=[dict(id=k, goal=k, requirement_ids=[k], launch=None) for k in ['start', 'grow', 'retry']])


def sprite():
    return dict(id='bird', kind='sprite', description='Bird', prompt='A blue bird.', requirements='Recognizable bird.',
        generation_scope='single_subject', motion='static', scope_reason='One actor', includes=['bird'],
        game_controlled=['movement'], frames=1, fps=1, size=[64, 64], reference_asset_id='',
        reference_prompt='', requirement_ids=['grow'], binding='Draw the actor')


def args(root, **values):
    d = dict(output=root, task='garden-gulp', bench=root / 'unused-bench', groups=3, rounds=3,
        model='fake-model', play_seconds=10, max_actions=20, material_rounds=2, material_candidates=1,
        h3_steps=16, expected_account_hash='test', resume=False, stage='all', score=False)
    d.update(values)
    return argparse.Namespace(**d)


class FakeServices:
    def __init__(self, template=None):
        self.template = template or plan()
        self.player_calls = []
        self.select_calls = 0
        self.retry_once = False
        self.new_material = False
        self.fail_next = False
        self.reflection_calls = []
        self.player_priors = []

    def plan(self, output):
        return copy.deepcopy(self.template)

    def materials(self, version, p, only=None, feedback=''):
        material = version / 'material'; material.mkdir(exist_ok=True)
        write_json(material / 'asset_plan.json', c.material_plan(p, 'garden-gulp'))
        manifest = {'assets': {}}
        for a in p['assets']:
            names = []
            for i in range(a['frames']):
                name = f'{a["id"]}-{i}.png'; (material / name).write_bytes(PNG); names.append(name)
            manifest['assets'][a['id']] = {'files': names}
        write_json(material / 'asset_manifest.json', manifest)

    def build(self, version, p, instructions, feedback=None):
        game = version / 'game'; game.mkdir(exist_ok=True)
        for name, text in DOCS.items(): (game / name).write_text(text)
        stage_materials(version, p)
        (game / 'index.html').write_text('<html><script src="gameasg-assets.js"></script><script>'
            'window.__gameTest={reset(){},input(){},getSnapshot(){},loadScenario(){}};'
            '</script><body>' + version.name + '</body></html>')
        return bundle_html(game)

    def probe(self, game, output, policy, group):
        output.mkdir(parents=True)
        (output / 'frame.jpg').write_bytes(b'fake frame')
        (output / 'video.mp4').write_bytes(b'fake movie')
        trace = {'frames': [{'image': 'frame.jpg', 'snapshot': {'phase': 'playing'},
                 'time_ms': 0, 'assets': {}}], 'errors': [], 'actions': [], 'stop_reason': 'done'}
        write_json(output / 'trace.json', trace)
        return trace

    def player(self, game, p, group, output, initial, prior=None, changes=None):
        self.player_calls.append((game.parent.name, group['id'], str(output), changes))
        self.player_priors.append(copy.deepcopy(prior))
        return {'policy': 'function step(){return {type:"done"};}', 'skill': 'React to current game.'}

    def summarize_skill(self, p, group, output, attempts, *, prior=None, changes=None):
        self.reflection_calls.append((str(output), copy.deepcopy(attempts), copy.deepcopy(prior)))
        selected = next(a for a in attempts if a['selected'])
        trace = selected['trace']
        return dict(skill=f'Learned from {output.parent}: check the visible state before acting.',
                    reason='Reflect on the observed outcome.', uncertainties=['Not yet independently tested.'],
                    evidence=[dict(attempt=selected['id'], frames=[0] if trace['frames'] else [],
                                   actions=[], errors=[] if trace['frames'] else [0])])

    def assess(self, group, p, output, trace):
        ok = not (self.retry_once and output.name == 'attempt-1')
        return {'requirements': [dict(requirement_id=rid, observed=ok, passed=ok,
                evidence='visible', frames=[0] if ok else []) for rid in group['requirement_ids']],
                'retry_needed': not ok, 'retry_reason': 'retry' if not ok else ''}

    def diagnose(self, version, p, group, before, output):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError('simulated interruption')
        return {'issues': [dict(id='fix', problem='feedback', evidence='frame 0',
            requirement_ids=group['requirement_ids'], route='code', asset_ids=[], acceptance=['clear'])],
            'missing_evidence': [], 'visual_richness': 'adequate', 'gameplay_richness': 'adequate'}

    def repair(self, version, p, diagnosis, output):
        materials = [sprite()] if self.new_material else []
        return {'reason': 'Improve observed feedback', 'candidates': [dict(id='a', instructions='Repair feedback',
            issue_ids=['fix'], upgrade_reason='', materials=materials)]}

    def select(self, group, diagnosis, options, output):
        self.select_calls += 1
        return {'winner': 'current' if self.new_material else 'a', 'reason': 'Test decision'}


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prepare = patch('gameasg.initialization.prepare', side_effect=self.prepare_export).start()
        self.freeze = patch('gameasg.initialization.freeze', side_effect=self.freeze_export).start()

    def tearDown(self):
        patch.stopall(); self.temp.cleanup()

    def prepare_export(self, a):
        a.output.mkdir(parents=True)
        (a.output / 'game').mkdir()
        write_json(a.output / 'manifest.json', {'generation': None})

    def freeze_export(self, a):
        shutil.copyfile(a.html, a.workspace / 'game/index.html')
        write_json(a.workspace / 'manifest.json', {'generation': {'html_sha256': digest(a.html)}})

    def play_baselines(self, services):
        engine = Engine(args(self.root, stage='bootstrap'), services, DOCS)
        engine.run()
        version = self.root / 'versions/bootstrap'
        evidence = {group['id']: engine.play(version, group,
                    self.root / 'bootstrap-playtests' / group['id'])
                    for group in plan()['groups']}
        return engine, evidence

    def test_three_groups_nine_rounds_and_final_version_owned_evidence(self):
        services = FakeServices()
        engine = Engine(args(self.root), services, DOCS)
        state = engine.run()
        self.assertEqual(len(state['rounds']), 9)
        self.assertEqual(services.select_calls, 9)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual({x['version_hash'] for x in state['final_evidence'].values()}, {state['current_hash']})
        self.assertEqual(len(state['bootstrap_evidence']), 3)
        # Each candidate gets a newly authored Player, not the incumbent's fixed actions.
        candidates = [x for x in services.player_calls if x[0].endswith('-a') and 'candidate-a' in x[2]]
        self.assertEqual(len(candidates), 9)
        # A round's selected evidence is the next round's unchanged incumbent.
        self.assertFalse(any('/playtests/' in x[2] and x[0] == 'group-01-round-01-a'
                             for x in services.player_calls))
        for x in candidates: self.assertIsNotNone(x[3]['changes'])
        self.assertNotEqual(state['bootstrap_hash'], state['current_hash'])
        before = len(services.player_calls)
        Engine(args(self.root, resume=True), services, DOCS).run()
        self.assertEqual(len(services.player_calls), before)
        self.assertEqual(services.select_calls, 9)

    def test_bootstrap_only_then_resume_optimization(self):
        services = FakeServices()
        first = Engine(args(self.root, stage='bootstrap'), services, DOCS).run()
        self.assertEqual(first['status'], 'bootstrapped')
        self.assertFalse(first['rounds'])
        self.assertFalse(first['bootstrap_evidence'])
        self.assertFalse(services.player_calls)
        self.assertFalse(services.reflection_calls)
        self.assertFalse((self.root / 'bootstrap-playtests').exists())
        second = Engine(args(self.root, resume=True, stage='optimize'), services, DOCS).run()
        self.assertEqual(len(second['rounds']), 9)
        self.assertEqual(first['bootstrap_hash'], second['bootstrap_hash'])
        self.assertEqual(len(second['bootstrap_evidence']), 3)

    def test_policy_failure_gets_one_retry_per_playtest(self):
        services = FakeServices(); services.retry_once = True
        _, evidence = self.play_baselines(services)
        self.assertEqual(len(services.player_calls), 6)
        self.assertTrue(all(x['path'].endswith('attempt-2') for x in evidence.values()))
        self.assertEqual(len(services.reflection_calls), 3)
        self.assertTrue(all(len(call[1]) == 2 for call in services.reflection_calls))

    def test_reflected_skill_reaches_next_author_without_rewriting_executed_policy(self):
        services = FakeServices()
        state = Engine(args(self.root, rounds=1), services, DOCS).run()
        for call, prior in zip(services.player_calls, services.player_priors):
            if 'candidate-a' in call[2]:
                self.assertEqual(prior['skill_source'], 'post_play_reflection')
                self.assertTrue(prior['skill'].startswith('Learned from'))
        evidence = state['bootstrap_evidence']['start']
        original = read_json(Path(evidence['path']) / 'policy.json')
        self.assertEqual(original['skill'], 'React to current game.')
        self.assertNotEqual(original['skill'], evidence['policy']['skill'])
        self.assertEqual(original['policy'], evidence['policy']['policy'])
        self.assertEqual(evidence['behavior_skill']['verification'], 'pending_next_playtest')
        final = read_json(self.root / 'behavior-skills-final.json')
        self.assertEqual(set(final['groups']), {'start', 'grow', 'retry'})
        count = len(services.reflection_calls)
        Engine(args(self.root, rounds=1, resume=True), services, DOCS).run()
        self.assertEqual(len(services.reflection_calls), count)

    def test_reflection_failure_resumes_without_replaying_saved_video(self):
        services = FakeServices()
        original = services.summarize_skill
        def unavailable(*a, **kw):
            raise RuntimeError('reflector temporarily unavailable')
        services.summarize_skill = unavailable
        engine = Engine(args(self.root, stage='bootstrap'), services, DOCS)
        engine.run()
        first = engine.play(self.root / 'versions/bootstrap', plan()['groups'][0],
                            self.root / 'bootstrap-playtests/start')
        self.assertEqual(first['behavior_skill']['verification'], 'unverified')
        self.assertEqual(first['policy']['skill_source'], 'executed_policy')
        self.assertEqual(len(services.player_calls), 1)
        self.assertTrue((self.root / 'bootstrap-playtests/start/evidence.json').exists())
        services.summarize_skill = original
        engine = Engine(args(self.root, resume=True, stage='bootstrap'), services, DOCS)
        evidence = [engine.play(self.root / 'versions/bootstrap', group,
                    self.root / 'bootstrap-playtests' / group['id']) for group in plan()['groups']]
        self.assertEqual(len(services.player_calls), 3)
        self.assertTrue(all(ev.get('behavior_skill') for ev in evidence))
        self.assertEqual(evidence[0]['behavior_skill']['verification'], 'unverified')

    def test_partial_repair_can_fix_one_issue_and_defer_another(self):
        services = FakeServices()
        original = services.diagnose
        def two_issues(*a):
            answer = original(*a)
            answer['issues'].append(dict(answer['issues'][0], id='later'))
            return answer
        services.diagnose = two_issues
        state = Engine(args(self.root, rounds=1), services, DOCS).run()
        self.assertEqual(len(state['rounds']), 3)
        self.assertTrue(all(row['deferred_issue_ids'] == ['later'] for row in state['rounds']))
        self.assertTrue(all(row['selection']['winner'] == 'a' for row in state['rounds']))

    def test_invalid_repair_and_selection_keep_incumbent(self):
        services = FakeServices()
        def invalid(*a):
            return {'reason': 'bad reference', 'candidates': [dict(id='a', issue_ids=['unknown'],
                instructions='fix', upgrade_reason='', materials=[])]}
        services.repair = invalid
        state = Engine(args(self.root, rounds=1), services, DOCS).run()
        self.assertEqual(len(state['rounds']), 3)
        self.assertTrue(all('repair_plan' in row['failures'] for row in state['rounds']))
        self.assertTrue(all(row['selection']['winner'] == 'current' for row in state['rounds']))

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        services = FakeServices()
        services.select = lambda *a: (_ for _ in ()).throw(ValueError('invalid frame reference'))
        state = Engine(args(Path(other.name), rounds=1), services, DOCS).run()
        self.assertTrue(all('selection' in row['failures'] for row in state['rounds']))
        self.assertTrue(all(row['selection']['winner'] == 'current' for row in state['rounds']))

    def test_assessment_error_is_unverified_and_retried(self):
        services = FakeServices()
        services.assess = lambda *a: (_ for _ in ()).throw(ValueError('invalid frame reference'))
        _, evidence = self.play_baselines(services)
        self.assertEqual(len(services.player_calls), 6)
        self.assertTrue(all(not ev['assessment']['requirements'][0]['observed'] for ev in evidence.values()))
        self.assertTrue(all((Path(ev['path']) / 'assessment-error.json').exists() for ev in evidence.values()))

    def test_failed_second_attempt_can_inform_skill_without_replacing_better_video(self):
        services = FakeServices()
        original = services.assess
        def worsening(group, p, output, trace):
            answer = original(group, p, output, trace)
            answer['retry_needed'] = True
            if output.name == 'attempt-2':
                answer['requirements'][0].update(observed=False, passed=False, frames=[])
            return answer
        services.assess = worsening
        _, evidence = self.play_baselines(services)
        for item in evidence.values():
            self.assertTrue(item['path'].endswith('attempt-1'))
            self.assertEqual(item['behavior_skill']['selected_attempt'], 'attempt-1')
        for _, attempts, _ in services.reflection_calls:
            self.assertEqual(len(attempts), 2)
            self.assertFalse(attempts[1]['assessment']['requirements'][0]['observed'])

    def test_skill_citations_and_cached_summary_are_validated(self):
        services = FakeServices()
        _, baselines = self.play_baselines(services)
        evidence = baselines['start']
        summary = evidence['behavior_skill']
        attempts = services.reflection_calls[0][1]
        bad = copy.deepcopy(summary)
        bad['evidence'][0]['frames'] = [999]
        with self.assertRaisesRegex(ValueError, 'missing frames'):
            c.validate_behavior_skill(bad, attempts)
        bad['evidence'][0]['frames'] = [0]
        bad['evidence'][0]['attempt'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'unknown attempt'):
            c.validate_behavior_skill(bad, attempts)
        path = self.root / 'bootstrap-playtests/start/skill-reflection/behavior_skill.md'
        path.write_text('unrecorded modification')
        engine = Engine(args(self.root, stage='bootstrap', resume=True), services, DOCS)
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            engine.play(self.root / 'versions/bootstrap', plan()['groups'][0],
                        self.root / 'bootstrap-playtests/start')

    def test_browser_failure_without_trace_is_retried(self):
        services = FakeServices()
        original = services.probe
        def failing(game, output, policy, group):
            if output.name == 'replay' and output.parent.name == 'attempt-1':
                raise RuntimeError('invalid policy syntax')
            return original(game, output, policy, group)
        services.probe = failing
        _, evidence = self.play_baselines(services)
        self.assertEqual(len(services.player_calls), 6)
        self.assertTrue(all(not x['trace']['errors'] for x in evidence.values()))

    def test_interrupted_export_recovers_without_repeating_playtests(self):
        services = FakeServices()
        engine = Engine(args(self.root, stage='bootstrap'), services, DOCS)
        self.freeze.side_effect = RuntimeError('freeze interrupted')
        with self.assertRaisesRegex(RuntimeError, 'freeze interrupted'):
            engine.run()
        before = len(services.player_calls)
        self.freeze.side_effect = self.freeze_export
        Engine(args(self.root, stage='bootstrap', resume=True), services, DOCS).run()
        self.assertEqual(len(services.player_calls), before)
        self.assertTrue(read_json(self.root / 'exports/bootstrap/manifest.json')['generation'])

    def test_reference_order_and_upgrade_contract(self):
        still = sprite()
        moving = {**still, 'id': 'bird_flap', 'kind': 'animation', 'motion': 'loop',
                  'frames': 4, 'fps': 8, 'reference_asset_id': 'bird'}
        p = plan(); p['assets'] = [moving, still]
        c.validate_plan(p, DOCS, 3, 'garden-gulp')
        self.assertEqual([a['id'] for a in c.material_plan(p, 'garden-gulp')['assets']], ['bird', 'bird_flap'])
        p['assets'] = [still]
        candidate = dict(id='b', materials=[moving], upgrade_reason='Articulated flight')
        updated = c.apply_candidate(p, candidate, DOCS, 'garden-gulp')
        self.assertEqual(len(updated['assets']), 2)
        cyclic = {**moving, 'id': 'bird', 'reference_asset_id': 'bird_flap'}
        p['assets'] = [moving, cyclic]
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            c.material_plan(p, 'garden-gulp')

    def test_rejected_materials_do_not_replace_incumbent(self):
        services = FakeServices(); services.new_material = True
        state = Engine(args(self.root, rounds=1), services, DOCS).run()
        self.assertEqual(state['current'], 'versions/bootstrap')
        self.assertEqual(read_json(self.root / state['current'] / 'plan.json')['assets'], [])
        candidate = self.root / 'versions/group-01-round-01-a'
        self.assertEqual(read_json(candidate / 'plan.json')['assets'][0]['id'], 'bird')
        # Rejected candidates' reflected skills are archived, never fed forward.
        for prior in services.player_priors:
            if prior:
                self.assertNotIn('candidate-a', prior['skill'])

    def test_resume_rejects_changed_selected_html(self):
        services = FakeServices()
        state = Engine(args(self.root, stage='bootstrap'), services, DOCS).run()
        with (self.root / state['current'] / 'game/index.html').open('a') as f: f.write('changed')
        with self.assertRaisesRegex(ValueError, 'changed since checkpoint'):
            Engine(args(self.root, resume=True), services, DOCS)

    def test_interruption_preserves_selected_game_and_resumes(self):
        services = FakeServices(); services.fail_next = True
        engine = Engine(args(self.root), services, DOCS)
        with self.assertRaisesRegex(RuntimeError, 'interruption'): engine.run()
        state = read_json(self.root / 'initialization.json')
        self.assertEqual(state['current'], 'versions/bootstrap')
        result = Engine(args(self.root, resume=True), services, DOCS).run()
        self.assertEqual(len(result['rounds']), 9)

    def test_missing_coverage_is_reported_but_result_is_exported(self):
        services = FakeServices()
        original = services.assess
        def missing(group, p, output, trace):
            a = original(group, p, output, trace)
            if 'final-playtests' in str(output):
                a['requirements'][0].update(observed=False, passed=False, frames=[])
            return a
        services.assess = missing
        state = Engine(args(self.root, rounds=1), services, DOCS).run()
        self.assertEqual(state['status'], 'completed_with_coverage_gaps')
        self.assertEqual(state['coverage']['lost_from_bootstrap']['grow'], ['grow'])
        self.assertTrue((self.root / 'exports/final/game/index.html').is_file())

    def test_material_frames_inline_and_changes_detected(self):
        version = self.root / 'v'; version.mkdir()
        p = plan(); p['assets'] = [sprite()]
        services = FakeServices(p)
        services.materials(version, p); services.build(version, p, '')
        self.assertEqual(verify_bundle(version / 'game'), digest(version / 'game/index.html'))
        html = (version / 'game/index.html').read_text()
        self.assertIn('data:image/png;base64,', html)
        self.assertNotIn('src="gameasg-assets.js"', html)
        (version / 'game/gameasg-assets.js').write_text('changed')
        with self.assertRaises(ValueError): verify_bundle(version / 'game')

    def test_unknown_launch_missing_coverage_and_fabricated_evidence_rejected(self):
        p = plan(); p['groups'][0]['launch'] = 'cheat_win'
        with self.assertRaises(ValueError): c.validate_plan(p, DOCS, 3, 'garden-gulp')
        p = plan(); p['groups'][2]['requirement_ids'] = ['start']
        with self.assertRaises(ValueError): c.validate_plan(p, DOCS, 3, 'garden-gulp')
        answer = {'requirements': [dict(requirement_id='start', observed=True, passed=True,
                                       frames=[20], evidence='invented')]}
        with self.assertRaises(ValueError): c.validate_assessment(answer, plan()['groups'][0], {'frames': [{}]})

    def test_brief_outcome_is_retained_in_judge_samples(self):
        frames = [{'snapshot': {'phase': 'playing', 'x': i}} for i in range(100)]
        frames[37]['snapshot']['phase'] = 'win'
        selected = sample_indices(frames, 10)
        self.assertIn(37, selected)
        self.assertIn(0, selected)
        self.assertIn(99, selected)
        self.assertLessEqual(len(selected), 10)

    @unittest.skipUnless(shutil.which('node'), 'node required')
    def test_reactive_policy_memory_action_validation_and_execution_bound(self):
        player = Path(__import__('gameasg').__file__).parent / 'browser_player.js'
        script = '''const assert=require('assert'); const p=require(process.argv[1]);
          const step=p.compilePolicy('function step(o,m){m.n=(m.n||0)+1;return {type:"key",value:o.key,duration_ms:m.n*10}}');
          assert.equal(step({key:'ArrowRight'}).duration_ms,10);
          assert.equal(step({key:'ArrowLeft'}).value,'ArrowLeft');
          assert.throws(()=>p.validateAction({type:'click',value:'2,0'}));
          assert.throws(()=>p.validateAction({type:'eval',value:'win()'}));
          assert.throws(()=>p.compilePolicy('function step(){while(true){}}')({}));
          assert.throws(()=>p.compilePolicy('function step(){return require("fs")}')({}));
        '''
        subprocess.run(['node', '-e', script, str(player)], check=True, timeout=5)


if __name__ == '__main__': unittest.main()
