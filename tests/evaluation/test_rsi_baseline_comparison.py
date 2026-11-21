"""RSI-only baseline comparison keeps bad evidence local to one route."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from gamecraft_evaluation import rsi_baseline_comparison as rsi_compare


class RSIComparisonTests(unittest.TestCase):
    def test_bootstrap_comparison_handles_empty_and_invalid_evidence(self):
        for case in ('empty', 'short_label', 'invalid_twice'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
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
                if case == 'empty':
                    answers = [{'facts': []}]
                else:
                    label = 'B1' if case == 'short_label' else 'B99'
                    answer = {'facts': [{'id': 'B1', 'description': 'Title menu visible',
                                        'bootstrap_frame': label, 'final_frame': 'F01',
                                        'covered': True}]}
                    answers = [answer] if case == 'short_label' else [answer, answer]
                modules = {'llm': fake_llm, 'gamecraft_bench': benchmark,
                           'gamecraft_bench.verifier': verifier,
                           'gamecraft_bench.verifier.score': score}
                with patch.dict(sys.modules, modules), \
                     patch.object(rsi_compare.subprocess, 'run', return_value=SimpleNamespace(
                         stdout='{"format":{"duration":"2"}}')), \
                     patch.object(rsi_compare.models, 'call', side_effect=answers) as call:
                    result = rsi_compare.compare_baseline(
                        {'id': 'title', 'goal': 'Preserve the visible title',
                         'bootstrap_video': str(baseline)},
                        {'video': str(root / 'final.mp4'), 'frames': [str(after)]},
                        backend='local', model='test', output=root / 'comparison')
                self.assertEqual(call.call_count, len(answers))
                if case == 'empty':
                    self.assertEqual(result['status'], 'no_baseline_facts')
                    self.assertEqual(result['facts'], [])
                elif case == 'short_label':
                    self.assertEqual(result['status'], 'verified')
                    self.assertEqual(result['facts'][0]['bootstrap_frame'], 'B01')
                    self.assertTrue(result['passed'])
                else:
                    self.assertEqual(result['status'], 'unverified')
                    self.assertEqual(result['facts'], [])
                    self.assertTrue((root / 'comparison/invalid_attempt_02.json').exists())
                if case != 'short_label':
                    self.assertFalse(result['passed'])

