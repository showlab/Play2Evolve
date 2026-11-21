import json
import tempfile
import unittest
from pathlib import Path

from game_initialization.agentic.video_skill import (begin_round, finish_round,
                                                       rules_for_assets)
from gamecraft_assets.plans import write_json
from gamecraft_video_skill import initialize


class _LLM:
    @staticmethod
    def call(*args, **kwargs):
        return {'actionable': True,
                'instruction': 'Keep the subject centered with one stable silhouette.',
                'reason': 'The subject was cropped and changed identity.'}


class _Backend:
    llm = _LLM()


class AgenticVideoSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'generation').mkdir()
        (self.root/'draft').mkdir()
        self.asset = {'id': 'fighter', 'kind': 'animation', 'loop': True,
                      'description': 'one boxer', 'requirements': 'stable boxer identity',
                      'prompt': 'throw one punch', 'reference_prompt': '{desc}',
                      'size': [32, 32], 'frames': 2, 'fps': 2}
        write_json(self.root/'generation/asset_plan.json', {
            'schema_version': 1, 'task_id': 'boxing', 'style': 'arcade',
            'assets': [self.asset]})
        initialize(self.root/'generation')

    @staticmethod
    def issue(asset='fighter'):
        return {'passed': False, 'issues': [{
            'id': 'V1', 'blocking': True, 'repair_side': 'asset',
            'asset_ids': [asset], 'problem': 'The boxer changes identity.',
            'suggested_repair': 'Regenerate a stable single boxer.'}]}

    def test_candidate_is_applied_next_round_then_committed_after_visual_success(self):
        store=self.root/'global-store'
        write_json(self.root/'session.json',{'task_id':'boxing','global_video_skill_store':str(store)})
        first = finish_round(self.root, 1, 3, self.issue(), backend=_Backend())
        self.assertEqual(first['candidate_status'], 'pending')
        active = begin_round(self.root, 2)
        patch = active['patches'][0]['patch']
        self.assertEqual([r['id'] for r in rules_for_assets(self.root, ['fighter'])],
                         [patch['id']])
        self.assertEqual(rules_for_assets(self.root, ['arena']), [])
        write_json(self.root/'generation/asset_history.json', [{
            'asset': 'fighter', 'context': {'workflow': 'agentic_initialization', 'round': 2},
            'recipe': {'applied_rules': [patch['id']]}}])
        second = finish_round(self.root, 2, 3, {'passed': True, 'issues': []},
                              backend=_Backend())
        self.assertEqual(second['decisions'][0]['status'], 'accepted')
        local = json.loads((self.root/'generation/video_skill/local.json').read_text())
        self.assertIn(patch['id'], [r['id'] for r in local['rules']])
        self.assertTrue((self.root/'draft/video_skill/local.json').is_file())
        global_rows=(store/'local_evidence.jsonl').read_text().splitlines()
        self.assertEqual(len(global_rows),1)
        self.assertEqual(json.loads(global_rows[0])['task_id'],'boxing')

    def test_used_candidate_is_rejected_when_source_asset_issue_persists(self):
        finish_round(self.root, 1, 3, self.issue(), backend=_Backend())
        active = begin_round(self.root, 2)
        patch = active['patches'][0]['patch']
        write_json(self.root/'generation/asset_history.json', [{
            'asset': 'fighter', 'context': {'workflow': 'agentic_initialization', 'round': 2},
            'recipe': {'applied_rules': [patch['id']]}}])
        result = finish_round(self.root, 2, 3, self.issue(), backend=_Backend())
        self.assertEqual(result['decisions'][0]['status'], 'rejected')
        local = json.loads((self.root/'generation/video_skill/local.json').read_text())
        self.assertEqual(local['rules'], [])
        pending = json.loads((self.root/'generation/video_skill/pending.json').read_text())
        self.assertIn('previously applied candidate', pending['patches'][0]['failures'][-1])

    def test_final_round_candidate_remains_provisional(self):
        result = finish_round(self.root, 3, 3, self.issue(), backend=_Backend())
        self.assertEqual(result['candidate_status'], 'provisional_final')
        self.assertTrue((self.root/'generation/video_skill/provisional.json').is_file())
        self.assertFalse((self.root/'generation/video_skill/pending.json').exists())
        local = json.loads((self.root/'generation/video_skill/local.json').read_text())
        self.assertEqual(local['rules'], [])


if __name__ == '__main__':
    unittest.main()
