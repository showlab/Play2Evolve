import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from gamecraft_assets import reference_image
from gamecraft_assets.generate import generate
from gamecraft_assets.plans import initialize


class ReferenceImageTests(unittest.TestCase):
    def test_default_only_changes_wan_14b_and_explicit_opt_out(self):
        with patch.dict('os.environ', {'H3_BACKEND': 'wan',
                                      'WAN_MODEL_VARIANT': 'i2v_a14b_distill4'}, clear=False):
            self.assertEqual(reference_image.backend(), 'gpt_image')
            with patch.dict('os.environ', {'GAMECRAFT_REFERENCE_BACKEND': 'video'}):
                self.assertEqual(reference_image.backend(), 'video')
            with patch.dict('os.environ', {'WAN_MODEL_VARIANT': 'ti2v_5b'}):
                self.assertEqual(reference_image.backend(), 'video')
            self.assertEqual(reference_image.static_backend(), 'gpt_image')
            with patch.dict('os.environ', {'GAMECRAFT_STATIC_IMAGE_BACKEND': 'video'}):
                self.assertEqual(reference_image.static_backend(), 'video')

    def test_key_file_can_supply_workers_without_committed_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key'
            path.write_text('test-key\n')
            with patch.dict('os.environ', {'GAMECRAFT_IMAGE_API_KEY_FILE': str(path),
                                          'GAMECRAFT_IMAGE_API_BASE_URL': 'https://images.example/v1'}, clear=False), \
                 patch.dict('os.environ', {'GAMECRAFT_IMAGE_API_KEY': ''}, clear=False):
                self.assertEqual(reference_image.settings()['key'], 'test-key')

    def test_image_endpoint_has_no_implicit_gateway(self):
        with patch.dict('os.environ', {'GAMECRAFT_IMAGE_API_KEY': 'test-key'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'GAMECRAFT_IMAGE_API_BASE_URL'):
                reference_image.settings()

    def test_api_image_is_flattened_on_green_and_saved_atomically(self):
        source = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
        for y in range(8, 24):
            for x in range(8, 24):
                source.putpixel((x, y), (200, 30, 20, 255))
        output = io.BytesIO()
        source.save(output, format='PNG')
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(output.getvalue()).decode())])
        client = SimpleNamespace(images=SimpleNamespace(generate=lambda **kwargs: response))
        with tempfile.TemporaryDirectory() as tmp, patch('openai.OpenAI', return_value=client) as api:
            path = Path(tmp) / 'reference.png'
            reference_image.generate('one boxer, transparent background', path,
                                     {'key': 'test', 'base_url': 'https://example.test/v1',
                                      'model': 'gpt-image-2.5-flare', 'quality': 'medium'})
            self.assertEqual(Image.open(path).convert('RGB').getpixel((0, 0)), (0, 255, 0))
            self.assertEqual(Image.open(path).convert('RGB').getpixel((16, 16)), (200, 30, 20))
            self.assertFalse(path.with_name(path.name + '.tmp').exists())
            api.assert_called_once()

    def test_wan_shared_generator_uses_image_api_for_new_animation_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instruction = root / 'instruction.md'
            instruction.write_text('# Dread Wings\nOne-button flyer')
            workspace = initialize(instruction, root / 'workspace')
            plan = json.loads((workspace / 'asset_plan.json').read_text())
            bird = next(item for item in plan['assets'] if item['kind'] == 'animation')
            submitted = []
            fake = SimpleNamespace(h3_video=lambda body, out: submitted.append(body),
                                   h3_t2va=lambda *args, **kwargs: self.fail('unexpected reference video'),
                                   fit_ref=lambda path: None)
            def image_generation(prompt, path, config):
                self.assertIn('transparent background', prompt)
                Image.new('RGB', (32, 32), (0, 255, 0)).save(path)
            def process(_backend, _video, asset, dest):
                frame = dest / 'frame0000.png'
                Image.new('RGB', tuple(asset['size'])).save(frame)
                return [frame], [], {'coverage': 1, 'in_frame': 1}
            config = {'key': 'test', 'base_url': 'https://example.test/v1',
                      'model': 'gpt-image-2.5-flare', 'quality': 'medium'}
            with patch.dict('os.environ', {'H3_BACKEND': 'wan',
                                          'WAN_MODEL_VARIANT': 'i2v_a14b_distill4'}, clear=False), \
                 patch('gamecraft_assets.generate._backend', return_value=fake), \
                 patch('gamecraft_assets.generate._process', side_effect=process), \
                 patch('gamecraft_assets.reference_image.settings', return_value=config), \
                 patch('gamecraft_assets.reference_image.generate', side_effect=image_generation) as image_call:
                self.assertEqual(generate(workspace, only=[bird['id']], rounds=1, candidates=1,
                                          judge=False, phase='initialization'), [])
            self.assertEqual(image_call.call_count, 1)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0]['task'], 'fl2va')
            self.assertEqual(submitted[0]['conditions'][0]['frame_index'], 0)
            meta = json.loads((workspace / 'generated' / bird['id'] / 'reference.json').read_text())
            self.assertEqual(meta['backend'], 'gpt_image')
            self.assertNotIn('key', meta)

    def test_wan_14b_static_assets_use_image_api_without_video_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instruction = root / 'instruction.md'
            instruction.write_text('# Dread Wings\nOne-button flyer')
            workspace = initialize(instruction, root / 'workspace')
            fake = SimpleNamespace(h3_t2va=lambda *args, **kwargs: self.fail('unexpected video fallback'))
            config = {'key': 'test', 'base_url': 'https://example.test/v1',
                      'model': 'gpt-image-2.5-flare', 'quality': 'medium'}

            def make_still(prompt, path, *, kind, config):
                if kind == 'sprite':
                    self.assertIn('transparent background', prompt)
                    self.assertNotIn('pure green', prompt)
                    image = Image.new('RGBA', (64, 64))
                    image.paste((200, 40, 20, 255), (16, 8, 48, 56))
                else:
                    image = Image.new('RGBA', (64, 64), (30, 40, 50, 255))
                image.save(path)

            with patch.dict('os.environ', {'H3_BACKEND': 'wan',
                                          'WAN_MODEL_VARIANT': 'i2v_a14b_distill4'}, clear=False), \
                 patch('gamecraft_assets.generate._backend', return_value=fake), \
                 patch('gamecraft_assets.reference_image.settings', return_value=config), \
                 patch('gamecraft_assets.reference_image.generate_still', side_effect=make_still) as image_call:
                self.assertEqual(generate(workspace, only=['pipe', 'skyline'], rounds=1,
                                          candidates=1, judge=False), [])
            self.assertEqual(image_call.call_count, 2)
            for asset_id in ('pipe', 'skyline'):
                dest = workspace / 'generated' / asset_id / 'v0001'
                self.assertTrue((dest / 'raw.png').is_file())
                self.assertFalse((dest / 'raw.mp4').exists())
                self.assertEqual(json.loads((dest / 'result.json').read_text())['image_backend'], 'gpt_image')
            with Image.open(workspace / 'generated' / 'pipe' / 'v0001' / 'frame0000.png') as sprite:
                self.assertEqual(sprite.mode, 'RGBA')
                self.assertEqual(sprite.getchannel('A').getextrema(), (0, 255))


if __name__ == '__main__':
    unittest.main()
