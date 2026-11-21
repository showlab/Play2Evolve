"""Exercise Bootstrap material generation without H3 or model requests."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from gameasg.material import main
from gameasg.contracts import material_plan
from tests.asg.test_initialization import plan, sprite


class BootstrapMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = plan()
        bird = sprite()
        flap = dict(bird, id='flap', kind='animation', motion='event', frames=4,
                    reference_asset_id='bird')
        p['assets'] = [bird, flap]
        (self.root / 'asset_plan.json').write_text(json.dumps(material_plan(p, 'test')))
        (self.root / 'asset_manifest.json').write_text(json.dumps({'assets': {}}))
        # A retry of a previously rejected Bootstrap must not ask for a new skill.
        (self.root / 'asset_history.json').write_text(json.dumps([
            {'asset': 'bird', 'eligible': False, 'verdict': {'failure': 'bad target'}}]))

    def process(self, backend, video, asset, dest):
        paths = []
        for i in range(asset['frames']):
            p = dest / f'frame{i:04d}.png'
            Image.new('RGBA', (16, 16), (255, 0, 0, 255)).save(p)
            paths.append(p)
        # These would fail quality gating, but are usable generated files.
        return paths, paths[:1], {'coverage': 0.001, 'in_frame': 0, 'bbox_fill': 1}

    def test_bootstrap_skips_judges_and_skill_repair_and_allows_reference(self):
        backend = SimpleNamespace(h3_t2va=Mock(), h3_video=Mock())
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('gamecraft_assets.generate._backend', return_value=backend))
            stack.enter_context(patch('gamecraft_assets.generate._process', side_effect=self.process))
            stack.enter_context(patch('gamecraft_assets.reference_image.static_backend', return_value='video'))
            for name in ('gamecraft_assets.generate._judge',
                         'gamecraft_assets.generate._reference_valid',
                         'gamecraft_video_skill.propose_patch',
                         'gamecraft_video_skill.commit_patch'):
                stack.enter_context(patch(name, side_effect=AssertionError('Bootstrap called ' + name)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            main([str(self.root), '--rounds', '3', '--candidates', '4', '--commit-local-skill'])
            manifest = json.loads((self.root / 'asset_manifest.json').read_text())
            self.assertEqual(set(manifest['assets']), {'bird', 'flap'})
            for item in manifest['assets'].values():
                self.assertEqual(item['quality_status'], 'not_evaluated')
                self.assertEqual(item['verdict'], {})
                self.assertTrue(all((self.root / f).is_file() for f in item['files']))
            self.assertEqual(backend.h3_t2va.call_count, 1)
            self.assertEqual(backend.h3_video.call_count, 1)
            # An unchanged successful Bootstrap reuses the generated versions.
            main([str(self.root)])
            self.assertEqual(backend.h3_t2va.call_count, 1)
            self.assertEqual(backend.h3_video.call_count, 1)

    def test_generation_errors_still_fail(self):
        backend = SimpleNamespace(h3_t2va=Mock(side_effect=RuntimeError('transport failed')))
        with patch('gamecraft_assets.generate._backend', return_value=backend), \
             patch('gamecraft_assets.reference_image.static_backend', return_value='video'), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'H3 material generation failed'):
                main([str(self.root)])
        self.assertEqual(backend.h3_t2va.call_count, 1)

    def test_feedback_material_reaches_game_review_without_asset_judge(self):
        backend = SimpleNamespace(h3_t2va=Mock(), h3_video=Mock())
        with patch('gamecraft_assets.generate._backend', return_value=backend), \
             patch('gamecraft_assets.generate._process', side_effect=self.process), \
             patch('gamecraft_assets.generate._judge', side_effect=AssertionError('asset judge ran')), \
             patch('gamecraft_video_skill.propose_patch', side_effect=AssertionError('skill patch ran')), \
             contextlib.redirect_stdout(io.StringIO()):
            main([str(self.root), '--only', 'bird', '--feedback', 'Use a clear silhouette'])
        manifest = json.loads((self.root / 'asset_manifest.json').read_text())
        self.assertEqual(manifest['assets']['bird']['quality_status'], 'not_evaluated')
        self.assertEqual(backend.h3_t2va.call_count, 1)

    def test_explicit_feedback_waits_for_integrated_review(self):
        with patch('gamecraft_assets.generate.generate', return_value=['bird']) as generate:
            with self.assertRaisesRegex(RuntimeError, 'H3 material generation failed'):
                main([str(self.root), '--feedback', 'Fix edge artifacts',
                      '--rounds', '3', '--candidates', '4', '--commit-local-skill'])
        options = generate.call_args.kwargs
        self.assertFalse(options['judge'])
        self.assertFalse(options['propose_video_patch'])
        self.assertFalse(options['auto_commit'])
        self.assertEqual(options['rounds'], 1)
        self.assertEqual(options['candidates'], 1)
        self.assertEqual(options['feedback'], 'Fix edge artifacts')


if __name__ == '__main__':
    unittest.main()
