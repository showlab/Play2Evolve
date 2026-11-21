import json
import tempfile
import unittest
from pathlib import Path

from gamecraft_video_skill.decisions import learn, retrieve, round_case
from gamecraft_video_skill import initialize, resolve
from gamecraft_assets.plans import write_json


class VideoSkillDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root/'shared'
        self.asset = {'kind': 'animation', 'generation_scope': 'coupled_action',
                      'description': 'subject and prop motion',
                      'requirements': 'contact and timing stay synchronized'}

    @staticmethod
    def caller(model, instructions, data, schema):
        assert 'H3 model parameters' in instructions
        return {'lessons': [{'kind': 'animation', 'scope': 'coupled_action',
            'trigger': 'Separately generated subject and prop motions lose contact timing.',
            'decision': 'Try one coupled foreground clip when the interaction is finite; leave input and scoring to code.',
            'reason': 'A matched game replay favored the integrated material boundary.'}]}

    def test_cross_game_retrieval_is_bounded_and_excludes_current_game(self):
        first = self.root/'first'
        ids = learn(first, task_id='game-a', phase='initialization',
                    problems=['contact timing mismatch'],
                    decisions=[{'scope': 'coupled_action', 'material': 'joint motion'}],
                    outcome='improved', evidence={'review': 'candidate preferred'},
                    global_store=self.store, caller=self.caller)
        self.assertEqual(len(ids), 1)
        local = retrieve(first, assets=[self.asset], problems=['contact timing mismatch'],
                         task_id='game-a', global_store=self.store)
        self.assertEqual(local[0]['transfer'], 'local')
        second = retrieve(self.root/'second', assets=[self.asset],
                          problems=['contact timing mismatch'], task_id='game-b',
                          global_store=self.store)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['transfer'], 'cross_game_hypothesis')
        self.assertEqual(second[0]['source_games'], ['game-a'])
        self.assertEqual(retrieve(self.root/'second', assets=[{'kind': 'background',
            'generation_scope': 'scene'}], task_id='game-b', global_store=self.store), [])
        self.assertLessEqual(len(retrieve(first, assets=[self.asset], limit=4)), 4)

    def test_failed_decision_is_local_counterexample_not_shared_success(self):
        learn(self.root, task_id='game-a', phase='rsi', problems=['timing mismatch'],
              decisions=[{'scope': 'coupled_action'}], outcome='failed',
              global_store=self.store, caller=self.caller)
        library = json.loads((self.root/'video_skill/decisions.json').read_text())
        self.assertEqual(library['lessons'][0]['negative'], 1)
        self.assertFalse((self.store/'decision_evidence.jsonl').exists())
        self.assertEqual(retrieve(self.root/'other', assets=[self.asset],
                                  global_store=self.store), [])

    def test_bootstrap_builder_receives_relevant_cross_game_decision(self):
        from game_initialization.agentic.__main__ import video_planning_guidance
        learn(self.root/'first', task_id='game-a', phase='initialization',
              problems=['contact timing mismatch'], decisions=[{'scope': 'coupled_action'}],
              outcome='improved', global_store=self.store, caller=self.caller)
        work = self.root/'builder'
        work.mkdir()
        (work/'instruction.md').write_text('Show subject and prop contact timing in one activity.')
        prompt = video_planning_guidance(work, {'video_skill': True, 'task_id': 'game-b',
                                                'global_video_skill_store': str(self.store)})
        self.assertIn('coupled foreground clip', prompt)
        saved = json.loads((work/'video_skill_planning_context.json').read_text())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['transfer'], 'cross_game_hypothesis')
        self.assertEqual(video_planning_guidance(work, {'video_skill': False}), '')

    def test_round_case_requires_replayed_h3_candidate(self):
        plan = {'issues': [{'problem': 'contact timing mismatch'}]}
        group = {'id': 'g1', 'plan': {'candidates': [{'kind': 'higher_level',
            'scope': 'coupled_action', 'materials': [{'asset': {'description': 'joint motion'}}],
            'video_controls': ['visible motion'], 'code_controls': ['input']} ]},
            'candidates': [{'kind': 'higher_level', 'action': 'accept',
                            'replay': 'path/to/replay', 'baseline_reviews': [{'preferred': 'candidate'}]}]}
        case = round_case(plan, {'groups': [group]})
        self.assertEqual(case[2], 'improved')
        self.assertEqual(case[1][0]['scope'], 'coupled_action')
        group['candidates'][0].pop('replay')
        self.assertIsNone(round_case(plan, {'groups': [group]}))

    def test_code_only_repair_can_teach_when_h3_is_unnecessary(self):
        plan = {'issues': [{'problem': 'effect appears at wrong time',
                            'route': 'composition'}]}
        case = round_case(plan, {'sides': {'composition': {
            'action': 'accept', 'pairwise_reviews': [{'preferred': 'candidate'}]}}})
        self.assertEqual(case[2], 'improved')
        self.assertFalse(case[1][0]['h3_used'])

    def test_prompt_rule_count_is_bounded_and_explicit_round_rule_survives(self):
        initialize(self.root)
        rules = [{'id': f'global_{i}', 'applies_when': {'kind': 'animation'},
                  'instruction': f'Keep visual detail {i} stable.', 'source': 'test'}
                 for i in range(9)]
        write_json(self.root/'video_skill/global.json', {'schema_version': 1, 'rules': rules})
        current = {'id': 'current', 'applies_when': {'kind': 'animation'},
                   'instruction': 'Keep the current subject centered.', 'source': 'test'}
        recipe = resolve(self.root, {**self.asset, 'id': 'moving', 'prompt': 'move',
                                     'frames': 4, 'size': [32, 32], 'loop': False},
                         'painted style', phase='rsi', extra_rules=[current])
        self.assertEqual(len(recipe['applied_rules']), 4)
        self.assertIn('current', recipe['applied_rules'])


if __name__ == '__main__':
    unittest.main()
