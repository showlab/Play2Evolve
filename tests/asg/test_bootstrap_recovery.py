"""Regression cases for valid Bootstrap plans and playtest citations."""
import copy
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from gameasg import contracts as c
from gameasg.services import Services
from tests.asg.test_initialization import DOCS, plan, sprite


class BootstrapRecoveryTests(unittest.TestCase):
    def test_public_quote_allows_case_and_markdown_fence(self):
        docs = {**DOCS, 'tdd.md': DOCS['tdd.md'] + '\nThe API exposes:\n```javascript\nreset(): Snapshot\n```\n'}
        p = plan()
        p['requirements'][0]['source_quote'] = 'start THE game.'
        p['requirements'][1]['source_document'] = 'tdd.md'
        p['requirements'][1]['source_quote'] = 'The API exposes: reset(): Snapshot'
        c.validate_plan(p, docs, 3, 'garden-gulp')
        p['requirements'][1]['source_quote'] = 'The API exposes a fabricated endpoint'
        with self.assertRaisesRegex(ValueError, "'grow'.*'tdd.md'"):
            c.validate_plan(p, docs, 3, 'garden-gulp')

    def test_planner_repairs_two_independent_errors(self):
        p1 = plan()
        p1['requirements'][0]['id'] = 'START'
        p2 = plan()
        p2['assets'] = [sprite(), {**sprite(), 'id': 'bird2', 'reference_asset_id': 'bird'}]
        service = Services(SimpleNamespace(groups=3, task='garden-gulp'), DOCS)
        service.ask = Mock(side_effect=[p1, p2, plan()])
        with tempfile.TemporaryDirectory() as work:
            result = service.plan(work)
        self.assertEqual(result, plan())
        self.assertEqual([call.args[1] for call in service.ask.call_args_list],
                         ['plan', 'plan-retry', 'plan-retry-2'])
        self.assertIn('Static asset', service.ask.call_args_list[2].args[3]['validation_error'])

    def test_behavior_skill_accepts_labeled_real_attempt_only(self):
        answer = {'skill': 'Follow observed controls.', 'reason': 'Observed movement.',
                  'uncertainties': [], 'evidence': [{'attempt': 'attempt-1: movement works',
                    'frames': [0], 'actions': [], 'errors': []}]}
        attempts = [{'id': 'attempt-1', 'trace': {'frames': [{}], 'actions': [], 'errors': []}}]
        c.validate_behavior_skill(answer, attempts)
        self.assertEqual(answer['evidence'][0]['attempt'], 'attempt-1')
        bad = copy.deepcopy(answer)
        bad['evidence'][0]['attempt'] = 'attempt-9: invented'
        with self.assertRaisesRegex(ValueError, 'unknown attempt'):
            c.validate_behavior_skill(bad, attempts)
        bad['evidence'][0]['attempt'] = 'attempt-1: real'
        bad['evidence'][0]['frames'] = [5]
        with self.assertRaisesRegex(ValueError, 'missing frames'):
            c.validate_behavior_skill(bad, attempts)


if __name__ == '__main__':
    unittest.main()
