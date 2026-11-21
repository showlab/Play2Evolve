import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h3_minimax_api as api


class H3MiniMaxApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / 'reference.png'
        self.image.write_bytes(b'png-image')
        self.body = {'task': 'fl2va', 'prompt': 'Boxer jumps rope.',
                     'seconds': 4, 'target': {'short_edge': 768, 'aspect_ratio': '1:1'},
                     'conditions': [
                         {'type': 'image', 'uri': str(self.image), 'role': 'keyframe', 'frame_index': 0},
                         {'type': 'image', 'uri': str(self.image), 'role': 'keyframe', 'frame_index': -1},
                     ]}

    def test_keyframes_become_hosted_content_without_seed(self):
        request = api.api_request({**self.body, 'seed': 123})
        self.assertEqual(request['model'], 'MiniMax-H3')
        self.assertEqual(request['duration'], 4)
        self.assertEqual(request['resolution'], '768P')
        self.assertEqual(request['ratio'], 'adaptive')
        self.assertEqual([i.get('role') for i in request['content']],
                         [None, 'first_frame', 'last_frame'])
        self.assertTrue(request['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertNotIn('seed', request)

    def test_text_request_keeps_explicit_ratio(self):
        request = api.api_request({'task': 't2va', 'prompt': 'Empty boxing arena',
                                   'seconds': 4, 'target': {'aspect_ratio': '16:9'}})
        self.assertEqual(request['ratio'], '16:9')
        self.assertEqual(len(request['content']), 1)

    def test_invalid_keyframe_fails_before_paid_submission(self):
        broken = {**self.body, 'conditions': [{**self.body['conditions'][0], 'uri': '/missing/ref.png'}]}
        with self.assertRaises(FileNotFoundError):
            api.api_request(broken)

    def test_task_is_persisted_and_reused_after_download_failure(self):
        calls = []

        def answer(method, path, key, body=None, timeout=60):
            calls.append((method, path))
            return ({'task_id': 'task-1'} if method == 'POST'
                    else {'task': {'status': 'succeeded',
                                   'content': {'url': 'https://cdn.example.test/video.mp4'}}})

        output = self.root / 'video.mp4'
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'test-key'}), \
             patch.object(api, '_json', side_effect=answer), \
             patch.object(api.urllib.request, 'urlopen', side_effect=OSError('download interrupted')):
            with self.assertRaisesRegex(OSError, 'download interrupted'):
                api.generate(self.body, output)
            self.assertFalse(output.exists())
            with self.assertRaisesRegex(OSError, 'download interrupted'):
                api.generate(self.body, output)
        self.assertEqual([method for method, _ in calls], ['POST', 'GET', 'GET'])
        record = json.loads((self.root / 'video.minimax_task.json').read_text())
        self.assertEqual(record['task_id'], 'task-1')
        self.assertNotIn('test-key', json.dumps(record))
        self.assertNotIn('Boxer jumps rope', json.dumps(record))

    def test_missing_key_never_submits(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(api, '_json') as submit:
            with self.assertRaisesRegex(RuntimeError, 'MINIMAX_API_KEY'):
                api.generate(self.body, self.root / 'video.mp4')
            submit.assert_not_called()

    def test_success_downloads_video_after_polling(self):
        replies = iter([
            {'task_id': 'task-2'},
            {'task': {'status': 'running'}},
            {'task': {'status': 'succeeded',
                      'content': {'url': 'https://cdn.example.test/video.mp4'}}},
        ])
        output = self.root / 'video.mp4'
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'test-key'}), \
             patch.object(api, '_json', side_effect=lambda *a, **k: next(replies)) as call, \
             patch.object(api.urllib.request, 'urlopen', return_value=io.BytesIO(b'video bytes')) as download, \
             patch.object(api.time, 'sleep'):
            api.generate(self.body, output)
        self.assertEqual(output.read_bytes(), b'video bytes')
        self.assertEqual([x.args[0] for x in call.call_args_list], ['POST', 'GET', 'GET'])
        download.assert_called_once_with('https://cdn.example.test/video.mp4', timeout=600)

    def test_existing_h3_entry_point_dispatches_by_backend(self):
        with patch.dict(os.environ, {'LLM_BACKEND': 'local', 'OPTIMIZER_BACKEND': 'local'}):
            import gen_assets
        output = self.root / 'video.mp4'
        with patch.dict(os.environ, {'H3_BACKEND': 'minimax'}), \
             patch.object(api, 'generate') as hosted:
            gen_assets.h3_video(self.body, str(output))
            hosted.assert_called_once_with(self.body, str(output), timeout=gen_assets.H3_TIMEOUT)
        with patch.dict(os.environ, {'H3_BACKEND': 'local'}), \
             patch.object(gen_assets, 'http', side_effect=[b'{"id":"local-1"}',
                                                            b'{"status":"completed"}', b'local video']), \
             patch.object(gen_assets.time, 'sleep'):
            gen_assets.h3_video(self.body, str(output))
        self.assertEqual(output.read_bytes(), b'local video')


if __name__ == '__main__':
    unittest.main()
