"""Offline ASG RSI continuation/orchestration regression tests."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from gameasg import initialization as init
from gameasg.__main__ import read_json, write_json
from gameasg.rsi import RSIEngine, inherited_policy
from gameasg.rsi_services import validate_goal
from tests.asg import test_initialization as fixtures
from tests.asg.test_initialization import DOCS, FakeServices, args


def goal():
    return dict(existing_features=['start', 'growth', 'retry'], gameplay_upgrade='Add a branching garden',
                aesthetic_upgrade='Add animated feedback', novelty_reason='Branching is new',
                observable_outcomes=['Choose a branch and collect its reward'], requirement_ids=['grow'],
                instructions='Implement both tracks, preserve controls and original requirements', materials=[])


class FakeRSI(FakeServices):
    def __init__(self):
        super().__init__()
        self.goal_calls = []
        self.review_calls = 0
        self.reject_build = False
        self.break_candidate = False
        self.no_repairs = False
        self.fail_diagnosis = False

    def upgrade_goal(self, version, plan, baseline, output, history):
        self.goal_calls.append(copy.deepcopy(history))
        return goal()

    def review_build(self, goal, before, after, output):
        self.review_calls += 1
        return dict(core_playable=not self.reject_build, evidence_valid=True,
                    critical_regression=False, reason='playable, allow repair', missing_upgrade_evidence=['reward'])

    def diagnose(self, *arguments):
        if self.fail_diagnosis:
            raise RuntimeError('capacity unavailable')
        return super().diagnose(*arguments)

    def repair(self, *arguments):
        if self.no_repairs:
            return {'reason': 'No evidenced issue', 'candidates': []}
        return super().repair(*arguments)

    def build(self, version, *arguments):
        if self.break_candidate and version.name.startswith('round-'):
            raise RuntimeError('candidate build failed')
        return super().build(version, *arguments)


class RSITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'initialization'
        self.source.mkdir()
        # Helpers are unbound test methods; supply a dummy self.
        patch('gameasg.initialization.prepare',
              side_effect=lambda a: fixtures.InitializationTests.prepare_export(None, a)).start()
        patch('gameasg.initialization.freeze',
              side_effect=lambda a: fixtures.InitializationTests.freeze_export(None, a)).start()
        self.init_services = FakeServices()
        self.initial = init.Engine(args(self.source, skip_final_playtests=True), self.init_services, DOCS).run()
        self.source_hash = self.initial['current_hash']
        self.source_checkpoint = (self.source / 'initialization.json').read_bytes()

    def tearDown(self):
        patch.stopall()
        self.temp.cleanup()

    def engine(self, services=None, name='rsi', source=None, resume=False):
        out = self.root / name
        out.mkdir(exist_ok=True)
        return RSIEngine(args(out, source=source or self.source, resume=resume), services or FakeRSI(), DOCS)

    def test_skipped_final_exports_and_inherits_skill_then_three_rounds(self):
        self.assertEqual(self.initial['final_playtests'], 'skipped')
        self.assertFalse((self.source / 'final-playtests').exists())
        self.assertTrue((self.source / 'exports/final/game/index.html').exists())
        self.assertIn('Learned from', inherited_policy(self.initial)['skill'])
        services = FakeRSI()
        engine = self.engine(services)
        result = engine.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['rounds']), 3)  # not three rounds per source group
        self.assertEqual(result['full_build'], 'playable')
        self.assertEqual(services.review_calls, 1)
        self.assertIn('Learned from', services.player_priors[0]['skill'])
        self.assertFalse(result['final_judge_run'])
        self.assertFalse((engine.root / 'final-playtests').exists())
        self.assertTrue((engine.root / 'exports/final/game/index.html').exists())
        self.assertEqual(self.source_checkpoint, (self.source / 'initialization.json').read_bytes())
        self.assertEqual(self.source_hash, init.version_hash(self.source / self.initial['current']))
        # Every repair Player receives experience from the preceding selected game.
        for row in result['rounds']:
            self.assertEqual(row['selection']['winner'], 'a')

    def test_matching_final_entry_playtest_replaces_new_baseline_play(self):
        source = init.Engine(args(self.source, resume=True, skip_final_playtests=True),
                             FakeServices(), DOCS)
        entry = read_json(self.source / self.initial['current'] / 'plan.json')['groups'][0]
        evidence = source.play(self.source / self.initial['current'], entry,
                               self.source / 'final-playtests' / entry['id'])
        checkpoint = read_json(self.source / 'initialization.json')
        checkpoint['final_evidence'] = {entry['id']: evidence}
        write_json(self.source / 'initialization.json', checkpoint)
        services = FakeRSI()
        result = self.engine(services).run()
        self.assertEqual(result['baseline_source'], 'source_playtest')
        self.assertEqual(result['baseline']['version_hash'], self.source_hash)
        self.assertEqual(len(services.player_calls), 7)  # full build, three before/after repairs

    def test_missing_final_playtest_artifact_falls_back_to_fresh_play(self):
        source = init.Engine(args(self.source, resume=True, skip_final_playtests=True),
                             FakeServices(), DOCS)
        entry = read_json(self.source / self.initial['current'] / 'plan.json')['groups'][0]
        evidence = source.play(self.source / self.initial['current'], entry,
                               self.source / 'final-playtests' / entry['id'])
        checkpoint = read_json(self.source / 'initialization.json')
        checkpoint['final_evidence'] = {entry['id']: evidence}
        checkpoint['rounds'] = []  # no other same-version playtest can be reused
        write_json(self.source / 'initialization.json', checkpoint)
        (Path(evidence['path']) / 'replay/video.mp4').unlink()
        services = FakeRSI()
        result = self.engine(services).run()
        self.assertEqual(result['baseline_source'], 'new_playtest')
        self.assertEqual(len(services.player_calls), 8)

    def test_resume_does_not_repeat_finished_rounds_or_build(self):
        engine = self.engine()
        done = engine.run()
        services = FakeRSI()
        again = self.engine(services, resume=True).run()
        self.assertEqual(done['current_hash'], again['current_hash'])
        self.assertEqual(services.player_calls, [])
        self.assertEqual(services.goal_calls, [])

    def test_continue_next_cycle_uses_last_selected_version_and_goal(self):
        first = self.engine().run()
        services = FakeRSI()
        engine = self.engine(services, name='cycle2', source=self.root / 'rsi')
        self.assertEqual(engine.state['current_hash'], first['current_hash'])
        second = engine.run()
        self.assertEqual(services.goal_calls, [[first['goal']]])
        self.assertEqual(len(second['rounds']), 3)

    def test_candidate_errors_keep_playable_full_upgrade(self):
        services = FakeRSI(); services.break_candidate = True
        result = self.engine(services).run()
        self.assertEqual(result['status'], 'completed_with_errors')
        self.assertEqual(result['current'], 'versions/attempt-01')
        self.assertNotEqual(result['current_hash'], self.source_hash)
        self.assertEqual(len(result['rounds']), 3)
        self.assertTrue(all(r['failures']['a'] for r in result['rounds']))

    def test_full_build_rejection_is_bounded_and_exports_original(self):
        services = FakeRSI(); services.reject_build = True
        result = self.engine(services).run()
        self.assertEqual(services.review_calls, 2)
        self.assertEqual(result['status'], 'completed_with_errors')
        self.assertEqual(result['rounds'], [])
        self.assertEqual(result['current_hash'], self.source_hash)

    def test_no_code_fix_still_reauthors_player_each_round(self):
        services = FakeRSI(); services.no_repairs = True
        result = self.engine(services).run()
        self.assertEqual(len(services.player_calls), 4)  # source baseline, full build, three fresh policies
        self.assertEqual(len(result['rounds']), 3)
        self.assertEqual(services.select_calls, 0)

    def test_resume_after_interruption_preserves_accepted_build(self):
        services = FakeRSI()
        engine = self.engine(services)
        with patch.object(engine, 'repair_round', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.run()
        services2 = FakeRSI()
        done = self.engine(services2, resume=True).run()
        self.assertEqual(services2.review_calls, 0)
        self.assertEqual(len(done['rounds']), 3)

    def test_source_mutation_and_wrong_task_refused(self):
        with self.assertRaisesRegex(ValueError, 'task/public documents'):
            RSIEngine(args(self.root / 'wrong', source=self.source, task='other-game'), FakeRSI(), DOCS)
        selected = self.source / self.initial['current'] / 'game/index.html'
        selected.write_text(selected.read_text() + '<!-- changed -->')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.engine()

    def test_active_source_and_nested_output_refused(self):
        checkpoint = read_json(self.source / 'initialization.json')
        checkpoint['status'] = 'group-03-round-02'
        write_json(self.source / 'initialization.json', checkpoint)
        with self.assertRaisesRegex(ValueError, 'not finished'):
            self.engine()
        with self.assertRaisesRegex(ValueError, 'separate'):
            RSIEngine(args(self.source / 'rsi', source=self.source), FakeRSI(), DOCS)

    def test_material_skill_copy_is_private(self):
        original = self.source / self.initial['current'] / 'material/video_skill'
        original.mkdir(); (original / 'experience.json').write_text('{"skill":"existing"}')
        engine = self.engine()
        copied = engine.root / 'versions/source/material/video_skill/experience.json'
        self.assertEqual(copied.read_text(), (original / 'experience.json').read_text())
        copied.write_text('{}')
        self.assertNotEqual(copied.read_text(), (original / 'experience.json').read_text())

    def test_goal_requires_both_tracks_and_real_requirement_ids(self):
        plan = read_json(self.source / self.initial['current'] / 'plan.json')
        g = goal(); g['aesthetic_upgrade'] = ''
        with self.assertRaisesRegex(ValueError, 'both'):
            validate_goal(g, plan, DOCS, 'garden-gulp')
        g = goal(); g['requirement_ids'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'existing'):
            validate_goal(g, plan, DOCS, 'garden-gulp')

    def test_new_material_is_integrated_and_rejected_repairs_do_not_rollback_build(self):
        services = FakeRSI()
        proposal = goal(); proposal['materials'] = [fixtures.sprite()]
        services.upgrade_goal = lambda *a: proposal
        services.select = lambda *a: {'winner': 'current', 'reason': 'No evidenced improvement'}
        engine = self.engine(services)
        result = engine.run()
        selected = engine.root / result['current']
        self.assertEqual(result['current'], 'versions/attempt-01')
        self.assertIn('bird', read_json(selected / 'material/asset_manifest.json')['assets'])
        self.assertEqual(len(read_json(selected / 'plan.json')['assets']), 1)
        self.assertEqual(read_json(self.source / self.initial['current'] / 'plan.json')['assets'], [])
        self.assertNotEqual(init.version_hash(selected), self.source_hash)

    def test_score_submits_frozen_final_without_playing_again(self):
        engine = self.engine()
        engine.run()
        service = FakeRSI()
        resumed = self.engine(service, resume=True)
        resumed.args.score = True
        resumed.args.exp_id = 'test-final'
        resumed.args.docker = 'unused'; resumed.args.docker_host = ''; resumed.args.image = 'test'

        def score(a):
            self.assertEqual(a.workspace, resumed.root / 'exports/final')
            m = read_json(a.workspace / 'manifest.json')
            m['evaluation'] = {'test': 'receipt'}
            write_json(a.workspace / 'manifest.json', m)

        with patch('gameasg.rsi.score', side_effect=score) as scorer:
            result = resumed.run()
        self.assertEqual(scorer.call_count, 1)
        self.assertEqual(result['evaluation'], {'test': 'receipt'})
        self.assertEqual(service.player_calls, [])


if __name__ == '__main__':
    unittest.main()
