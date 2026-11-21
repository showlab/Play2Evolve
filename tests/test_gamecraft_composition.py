import json
import tempfile
import unittest
from pathlib import Path

from gamecraft_composition.core import accept, apply_patch, optimize, validate_feedback


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'source'
        self.project.mkdir()
        (self.project / 'project.godot').write_text('config_version=5\n')
        (self.project / 'main.gd').write_text('var size = 20\n')
        (self.project / 'demo_outputs').mkdir()
        (self.project / 'demo_outputs/test.json').write_text('{}')
        self.feedback = {'problem': 'UI too small', 'target': 'readability',
                         'criteria': [{'id': 'readability', 'description': 'Readable UI'},
                                      {'id': 'gameplay', 'description': 'Controls work'}]}
        self.patch = {'reason': 'Increase UI size', 'edits': [{'path': 'main.gd', 'old': 'size = 20', 'new': 'size = 30'}]}

    def score(self, value, other=.8):
        return {'build_ok': True, 'num_demos': 1, 'errors': [],
                'scores': {'readability': value, 'gameplay': other}}

    def test_gate_regressions_and_failed_runs(self):
        a = self.score(.5)
        for b in [self.score(.5), self.score(.8, .5), {**self.score(.8), 'errors': ['judge failed']},
                  self.score(float('nan')), {**self.score(.8), 'num_demos': 0}]:
            self.assertFalse(accept(a, b, self.feedback)[0])
        self.assertTrue(accept(a, self.score(.8), self.feedback)[0])

    def test_visual_defect_fix_can_pass_without_aesthetic_gain(self):
        feedback = {
            'target': 'aesthetic_quality', 'min_gain': .05,
            'repair_criteria': ['artifact_free', 'visual_correctness'],
            'repair_min_gain': .05, 'regression_tolerance': .02,
            'criteria': [{'id': 'aesthetic_quality', 'description': 'Aesthetic quality'},
                         {'id': 'artifact_free', 'description': 'No artifacts'},
                         {'id': 'visual_correctness', 'description': 'Correct visuals'}]
        }
        before = {'build_ok': True, 'num_demos': 1, 'errors': [],
                  'scores': {'aesthetic_quality': .7, 'artifact_free': .4, 'visual_correctness': .8}}
        after = {'build_ok': True, 'num_demos': 1, 'errors': [],
                 'scores': {'aesthetic_quality': .7, 'artifact_free': .6, 'visual_correctness': .8}}
        self.assertTrue(accept(before, after, feedback)[0])
        no_change = {**after, 'scores': {**after['scores'], 'artifact_free': .42}}
        self.assertFalse(accept(before, no_change, feedback)[0])

    def test_patch_is_transactional(self):
        bad = {'edits': self.patch['edits'] + [{'path': '../outside.gd', 'old': 'a', 'new': 'b'}]}
        with self.assertRaises(ValueError): apply_patch(self.project, bad)
        self.assertEqual((self.project / 'main.gd').read_text(), 'var size = 20\n')
        for path in ['demo_outputs/test.json', '/tmp/evil.gd', '.git/config', 'tests/hack.gd']:
            with self.assertRaises(ValueError):
                apply_patch(self.project, {'edits': [{'path': path, 'old': 'a', 'new': 'b'}]})

    def test_symlink_rejected(self):
        (self.project / 'link.gd').symlink_to(self.project / 'main.gd')
        with self.assertRaises(ValueError):
            apply_patch(self.project, {'edits': [{'path': 'link.gd', 'old': '20', 'new': '30'}]})

    def test_accept_and_reject_preserve_original(self):
        for improves in [True, False]:
            def evaluate(project, *_):
                return self.score(.8 if improves and '30' in (project / 'main.gd').read_text() else .5)
            result = optimize(self.project, self.root / str(improves), self.feedback,
                              lambda *_: self.patch, evaluate)
            self.assertEqual(result['accepted_steps'], int(improves))
            expected = '30' if improves else '20'
            self.assertIn(expected, (Path(result['accepted_project']) / 'main.gd').read_text())
            self.assertIn('20', (self.project / 'main.gd').read_text())

    def test_execution_cannot_change_demos(self):
        def evaluate(project, *_):
            if '30' in (project / 'main.gd').read_text():
                (project / 'demo_outputs/test.json').write_text('changed')
            return self.score(.8 if '30' in (project / 'main.gd').read_text() else .5)
        r = optimize(self.project, self.root / 'run', self.feedback, lambda *_: self.patch, evaluate)
        self.assertEqual(r['accepted_steps'], 0)
        self.assertIn('protected', r['history'][0]['reason'].replace('assets or input traces', 'protected'))

    def test_proposer_error_is_reported_without_losing_baseline(self):
        def broken(*_):
            raise RuntimeError('model unavailable')
        r = optimize(self.project, self.root / 'failure', self.feedback, broken,
                     lambda *_: self.score(.5))
        self.assertEqual(r['failed_steps'], 1)
        self.assertEqual(r['accepted_steps'], 0)
        self.assertIn('20', (Path(r['accepted_project']) / 'main.gd').read_text())

    def test_feedback_and_output_validation(self):
        with self.assertRaises(ValueError): validate_feedback({**self.feedback, 'min_gain': 0})
        with self.assertRaises(ValueError):
            optimize(self.project, self.project / 'out', self.feedback, None, None)


if __name__ == '__main__':
    unittest.main()
