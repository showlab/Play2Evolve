import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameasg import environment as env


def args(**values):
    base = dict(bench=env.DEFAULT_BENCH, docker='docker', docker_host=None,
                image=env.IMAGE, cache=None, start_daemon=False,
                dockerd='dockerd', state_dir=Path('/tmp/gameasg-docker'),
                bip='192.168.251.1/24', address_pool='192.168.252.0/24')
    return argparse.Namespace(**{**base, **values})


class PortableEnvironmentTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('docker_test.sh', 'Dockerfile'):
            (root / name).write_text('fixture')
        self.enterContext(patch.object(env, 'benchmark', return_value=root))

    def test_cache_requires_matching_sha_and_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'image.tar.gz'
            archive.write_bytes(b'fake compressed image')
            manifest = archive.with_name(archive.name + '.json')
            data = {'format': env.FORMAT, 'image': env.IMAGE,
                    'image_id': 'sha256:example', 'benchmark_commit': env.BENCH_COMMIT,
                    'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
            manifest.write_text(json.dumps(data))
            self.assertEqual(env.cache_manifest(archive), data)
            archive.write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                env.cache_manifest(archive)

    def test_preflight_checks_daemon_image_and_pinned_checkout(self):
        with patch.object(env, 'daemon_version', return_value='28.5.2'), \
             patch.object(env, 'image_id', return_value='sha256:example'), \
             patch.object(env.shutil, 'which', return_value='/usr/bin/docker'):
            state = env.preflight(args())
        self.assertEqual(state['benchmark_commit'], env.BENCH_COMMIT)
        self.assertEqual(state['image_id'], 'sha256:example')
        with patch.object(env, 'daemon_version', return_value='28.5.2'), \
             patch.object(env, 'image_id', return_value=None), \
             patch.object(env.shutil, 'which', return_value='/usr/bin/docker'):
            with self.assertRaisesRegex(RuntimeError, 'Missing evaluator image'):
                env.preflight(args())

    def test_private_daemon_requires_explicit_socket_before_any_launch(self):
        with patch.object(env.subprocess, 'Popen', side_effect=AssertionError('started')):
            with self.assertRaisesRegex(ValueError, 'unix://'):
                env.start_daemon(args())
            with self.assertRaisesRegex(ValueError, 'directly inside'):
                env.start_daemon(args(docker_host='unix:///tmp/elsewhere/docker.sock'))


if __name__ == '__main__':
    unittest.main()
