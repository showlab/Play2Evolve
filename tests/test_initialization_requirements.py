import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_initialization.agentic import requirements as r
from game_initialization.agentic import __main__ as runner
from game_initialization.batch.__main__ import read, write, sha


class PublicRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'instruction.md').write_text('Win matches against increasingly difficult opponents.')
        self.spec = {'requirements': [{
            'id': 'R001', 'quote': 'increasingly difficult opponents',
            'description': 'Progress to opponents with increasingly difficult behavior.',
            'verification': 'Win successive matches; observe changed opponents and behavior.',
            'asset_ids': [], 'dynamic': True}]}
        with patch.object(r, 'codex', return_value=self.spec):
            self.origin = r.prepare_requirements(self.root, {})
        (self.root/'draft/game').mkdir(parents=True)
        (self.root/'draft/game/rules.gd').write_text('opponent_speed += 1')
        self.case = 'checks/1/package/checks/case-000/result.json'
        write(self.root/self.case, {'replay_verified': True, 'decisions': [{'action': 'punch'}],
                                 'stepped': {'opponent': 'next', 'speed': 2}})
        self.runtime = {'passed': True, 'package': 'checks/1/package'}
        write(self.root/'latest_check.json', self.runtime)
        self.review = {'passed': True, 'missing_requirements': [], 'limitations': [],
                       'requirements': [{'id': 'R001', 'passed': True,
                            'code': ['draft/game/rules.gd'], 'runtime_checks': [self.case],
                            'reason': 'After the win, opponent changed and speed increased from 1 to 2.'}]}

    def accept(self):
        with patch.object(r, 'codex', return_value=self.review):
            r.review_requirements(self.root, {}, 'acceptance-0', self.runtime, self.origin)

    def test_parent_extracts_before_build_in_read_only_structured_call(self):
        with patch.object(r, 'codex') as model:
            r.prepare_requirements(self.root, {})
            model.assert_not_called()  # Resume retains the original scope.
        self.assertEqual(read(self.root/'requirements.json'), self.spec)
        self.assertEqual(self.origin['instruction_sha256'], sha(self.root/'instruction.md'))

    def test_quote_repair_retries_a_nonexact_first_correction(self):
        spec = copy.deepcopy(self.spec)
        spec['requirements'][0]['quote'] = 'increasingly difficulty opponents'
        corrections = [
            {'corrections': [{'id': 'R001', 'quote': 'increasingly difficulty opponents'}]},
            {'corrections': [{'id': 'R001', 'quote': 'increasingly difficult opponents'}]},
        ]
        with patch.object(r, 'codex', side_effect=corrections) as model:
            fixed = r.repair_invalid_quotes(self.root, {}, spec)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(fixed['requirements'][0]['quote'],
                         'increasingly difficult opponents')
        r.validate_spec(self.root, fixed)

    def test_builder_cannot_downgrade_progression_or_dynamic_evidence(self):
        weaker = copy.deepcopy(self.spec)
        weaker['requirements'][0].update(description='Track a rank counter; roster deferred.', dynamic=False)
        write(self.root/'requirements.json', weaker)
        with self.assertRaisesRegex(ValueError, 'scope'):
            r.assert_scope(self.root, self.origin)

    def test_spec_must_quote_actual_brief_not_hidden_opponent_count(self):
        self.spec['requirements'][0]['quote'] = 'four opponents'
        with self.assertRaisesRegex(ValueError, 'quote'):
            r.validate_spec(self.root, self.spec)

    def test_missing_original_requirement_blocks_even_when_overall_passed(self):
        self.review['requirements'] = []
        with self.assertRaisesRegex(ValueError, 'not satisfied'):
            self.accept()

    def test_independent_reviewer_detects_omissions_in_extracted_checklist(self):
        self.review['missing_requirements'] = ['Brief requires a championship ending, absent from checklist.']
        with self.assertRaisesRegex(ValueError, 'championship'):
            self.accept()

    def test_source_file_and_self_report_do_not_replace_execution(self):
        self.review['requirements'][0]['runtime_checks'] = []
        with self.assertRaisesRegex(ValueError, 'execution evidence'):
            self.accept()
        write(self.root/'draft/evidence/result.json', {'replay_verified': True})
        self.review['requirements'][0]['runtime_checks'] = ['draft/evidence/result.json']
        with self.assertRaisesRegex(ValueError, 'parent checks'):
            self.accept()

    def test_failed_runtime_result_is_not_acceptable_evidence(self):
        write(self.root/self.case, {'replay_verified': False, 'decisions': [1], 'stepped': {}})
        with self.assertRaisesRegex(ValueError, 'runtime observation'):
            self.accept()

    def test_acceptance_plan_requires_reachable_initialization_cases(self):
        write(self.root/'draft/scenarios.json', [{'id': 'menu', 'purpose': 'initialization'},
                                               {'id': 'career', 'purpose': 'train'}])
        plan = {'requirements': [{'id': 'R001', 'implementation': 'opponent progression', 'scenarios': ['career']}]}
        write(self.root/'acceptance_plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'initialization coverage'):
            r.validate_acceptance_plan(self.root)
        plan['requirements'][0]['scenarios'] = ['menu']
        write(self.root/'acceptance_plan.json', plan)
        r.validate_acceptance_plan(self.root)  # Structural pass still needs semantic review.

    def test_entry_screen_requirement_requires_a_real_default_launch(self):
        spec = read(self.root/'requirements.json')
        spec['requirements'][0].update(
            description='Normal launch starts from the title screen.',
            verification='On normal launch, the first screen is the title screen.')
        write(self.root/'requirements.json', spec)
        write(self.root/'draft/scenarios.json', [
            {'id':'title_flow','purpose':'initialization','actions':['tap']}])
        write(self.root/'acceptance_plan.json', {'requirements':[
            {'id':'R001','implementation':'title then career','scenarios':['title_flow']}]})
        with self.assertRaisesRegex(ValueError, 'default-launch coverage'):
            r.validate_acceptance_plan(self.root)
        write(self.root/'draft/scenarios.json', [
            {'id':'title_flow','purpose':'initialization','launch':'default','actions':['tap']}])
        r.validate_acceptance_plan(self.root)

    def test_current_review_accepts_and_any_candidate_or_evidence_edit_invalidates_it(self):
        self.accept()
        r.require_current_review(self.root, self.runtime)
        (self.root/'draft/game/rules.gd').write_text('opponent_speed = 1')
        with self.assertRaisesRegex(ValueError, 'current independent'):
            r.require_current_review(self.root, self.runtime)
        (self.root/'draft/game/rules.gd').write_text('opponent_speed += 1')
        write(self.root/self.case, {'replay_verified': True, 'decisions': [1], 'stepped': {'speed': 1}})
        with self.assertRaisesRegex(ValueError, 'current independent'):
            r.require_current_review(self.root, self.runtime)

    def test_runtime_check_alone_cannot_publish(self):
        from game_initialization.package import inventory
        self.runtime['draft_sha256'] = inventory(self.root/'draft')
        write(self.root/'latest_check.json', self.runtime)
        with self.assertRaisesRegex(ValueError, 'review is required'):
            runner.publish(self.root)
        self.assertFalse((self.root/'initial-v0').exists())

    def test_successful_publication_requires_matching_review_and_inspection(self):
        from game_initialization.package import inventory
        video = 'checks/1/package/checks/case-000/replay.mp4'
        image = 'checks/1/package/checks/case-000/frame_0000.png'
        (self.root/video).write_bytes(b'video fixture')
        (self.root/image).write_bytes(b'image fixture')
        self.runtime.update(draft_sha256=inventory(self.root/'draft'), videos=[video])
        write(self.root/'latest_check.json', self.runtime)
        write(self.root/'inspection.json', {'screens': [{'image': image, 'observation': 'Opponent changed.'}],
                  'videos': [{'path': video, 'observation': 'Win and next opponent.'}]})
        self.accept()
        with patch('game_initialization.package.inspect_package'), \
             patch('game_initialization.agentic.visual_v2.require_current_visual_review'):
            runner.publish(self.root)
        self.assertTrue((self.root/'initial-v0/checks/case-000/result.json').is_file())
        self.assertEqual(read(self.root/'state.json')['stage'], 'ready')

    def test_new_runtime_attempt_invalidates_previous_review(self):
        self.accept()
        write(self.root/'latest_check.json', dict(self.runtime, created_at=123))
        with self.assertRaisesRegex(ValueError, 'current independent'):
            r.require_current_review(self.root, self.runtime)

    def test_review_cannot_change_candidate_while_accepting_it(self):
        def mutate(*args, **kwargs):
            (self.root/'draft/game/rules.gd').write_text('opponent_speed = 1')
            return self.review
        with patch.object(r, 'codex', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed the candidate'):
                r.review_requirements(self.root, {}, 'acceptance-0', self.runtime, self.origin)

    def test_rejected_review_feeds_next_build_and_never_publishes(self):
        (self.root/'prompt.txt').write_text('Build complete game.')
        write(self.root/'inspection.json', {'videos': [{'path': self.case}]})
        self.runtime['draft_sha256'] = {}
        cfg = {'rounds': 2, 'godot': 'godot', 'max_requests': 64}
        with patch.object(runner, 'host', return_value=cfg), \
             patch.object(runner, 'agentic_preflight'), \
             patch.object(runner, 'prepare_requirements', return_value=self.origin), \
             patch.object(runner, 'codex') as build, \
             patch.object(runner, 'check', return_value=self.runtime), \
             patch('game_initialization.agentic.visual_v2.review_visual'), \
             patch.object(runner, 'review_requirements', side_effect=ValueError('Missing gameplay progression')), \
             patch.object(runner, 'publish') as publish:
            with self.assertRaisesRegex(RuntimeError, 'progression'):
                runner.run(self.root)
            self.assertEqual(build.call_count, 2)
            self.assertIn('Missing gameplay progression', build.call_args.args[3])
            publish.assert_not_called()
        self.assertEqual(read(self.root/'state.json')['stage'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
