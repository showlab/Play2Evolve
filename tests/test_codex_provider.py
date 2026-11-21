import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import codex_provider
from codex_provider import child_environment, cli_args, validate
from game_initialization.batch import __main__ as batch
from gamecraft_behavior.core import write_json
from gamecraft_training.package.codex_edit import execute_job
from gamecraft_training.package import models


KLINK = {
    'codex_provider': 'klink',
    'codex_base_url': 'https://api.example.com/v1',
    'codex_api_key_env': 'KLINK_API_KEY',
    'codex_supports_websockets': False,
}


class CodexProviderTests(unittest.TestCase):
    def test_default_uses_logged_in_codex_without_gateway(self):
        with patch.dict(os.environ, {}, clear=True):
            config = codex_provider.from_environment()
            self.assertEqual(config, {'codex_provider': 'openai'})
            env = child_environment(config, {'CODEX_HOME': '/login',
                                             'GAMECRAFT_CODEX_API_KEY': 'unused',
                                             'OPENAI_API_KEY': 'unrelated'})
            self.assertEqual(env['CODEX_HOME'], '/login')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(cli_args(config), ['-c', 'model_provider="openai"'])

    def test_gateway_requires_explicit_route_and_key(self):
        with patch.dict(os.environ, {'GAMECRAFT_CODEX_PROVIDER': 'custom'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'codex-base-url'):
                codex_provider.from_environment()

    def test_custom_provider_builds_responses_config_without_secret(self):
        args = cli_args(KLINK)
        joined = '\n'.join(args)
        self.assertIn('model_provider="klink"', joined)
        self.assertIn('model_providers.klink.base_url="https://api.example.com/v1"', joined)
        self.assertIn('model_providers.klink.env_key="KLINK_API_KEY"', joined)
        self.assertIn('model_providers.klink.wire_api="responses"', joined)
        self.assertIn('model_providers.klink.supports_websockets=false', joined)
        self.assertNotIn('secret-value', joined)

    def test_custom_provider_keeps_only_explicit_credential(self):
        source = {'KLINK_API_KEY': 'secret-value', 'OPENAI_API_KEY': 'wrong',
                  'CODEX_API_KEY': 'wrong', 'OPENAI_BASE_URL': 'https://wrong.example/v1'}
        env = child_environment(KLINK, source)
        self.assertEqual(env['KLINK_API_KEY'], 'secret-value')
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertNotIn('OPENAI_BASE_URL', env)
        with self.assertRaisesRegex(RuntimeError, 'KLINK_API_KEY'):
            child_environment(KLINK, {})

    def test_invalid_custom_provider_is_rejected_before_codex_runs(self):
        with self.assertRaises(ValueError):
            validate({'codex_provider': 'bad.provider', 'codex_base_url': 'https://example.com',
                      'codex_api_key_env': 'KEY'})
        with self.assertRaises(ValueError):
            validate({'codex_provider': 'custom', 'codex_base_url': 'http://example.com',
                      'codex_api_key_env': 'KEY'})

    def test_initialization_codex_uses_custom_provider(self):
        with tempfile.TemporaryDirectory() as td:
            task = Path(td)
            cfg = {'codex': 'codex', 'model': 'gpt-6.1-sol', 'timeout': 10, **KLINK}
            with patch.dict(os.environ, {'KLINK_API_KEY': 'secret-value',
                                         'OPENAI_API_KEY': 'wrong'}, clear=False), \
                 patch.object(batch, 'command') as command:
                batch.codex(task, cfg, 'build', 'Do the work')
            argv = command.call_args.args[0]
            joined = '\n'.join(argv)
            self.assertIn('model_provider="klink"', joined)
            self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6.1-sol')
            env = command.call_args.kwargs['env']
            self.assertEqual(env['KLINK_API_KEY'], 'secret-value')
            self.assertNotIn('OPENAI_API_KEY', env)

    def test_composition_worker_uses_custom_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_json(root/'job.json', {'expires': 10**12})

            def start(command, **kwargs):
                joined = '\n'.join(command)
                self.assertIn('model_provider="klink"', joined)
                self.assertIn('model_reasoning_effort="medium"', joined)
                self.assertEqual(kwargs['env']['KLINK_API_KEY'], 'secret-value')
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])

                def communicate(prompt, timeout):
                    write_json(root/'answer.json', {'summary': 'done', 'tests': []})

                return SimpleNamespace(communicate=communicate, returncode=0)

            with patch.dict(os.environ, {'KLINK_API_KEY': 'secret-value',
                                         'GAMECRAFT_CODEX_REASONING_EFFORT': 'medium',
                                         'OPENAI_API_KEY': 'wrong'}, clear=False), \
                 patch('subprocess.Popen', side_effect=start):
                self.assertTrue(execute_job(root, model='gpt-6.1-sol', provider=KLINK))

    def test_judge_calls_receive_configured_reasoning_effort(self):
        fake_llm = SimpleNamespace(call=Mock())
        with patch.dict(os.environ, {'GAMECRAFT_CODEX_REASONING_EFFORT': 'medium'}), \
             patch.dict(sys.modules, {'llm': fake_llm}):
            models.call('instructions', {}, [], 'answer', {}, backend='codex', model='gpt-6-sol')
            self.assertEqual(fake_llm.call.call_args.kwargs['effort'], 'medium')


if __name__ == '__main__':
    unittest.main()
