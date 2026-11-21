import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from play2evolve import cli, runtime


class PortabilityTests(unittest.TestCase):
    def test_explicit_benchmark_and_loaded_checkout_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'gamecraft_bench/verifier').mkdir(parents=True)
            (root / 'gamecraft_bench/verifier/replay.py').write_text('')
            with patch.dict(os.environ, {'GAMECRAFT_BENCH_ROOT': str(root)}):
                self.assertEqual(runtime.benchmark_root(), root)
                other = SimpleNamespace(__file__='/different/checkout/gamecraft_bench/__init__.py')
                with patch.dict(sys.modules, {'gamecraft_bench': other}):
                    with self.assertRaisesRegex(RuntimeError, 'already imported'):
                        runtime.configure_benchmark(root)

    def test_config_does_not_override_environment_or_store_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'settings.toml'
            cfg.write_text('[environment]\nH3_URL = "http://configured:30010"\n')
            with patch.dict(os.environ, {'H3_URL': 'http://explicit:30010'}):
                cli.load_config(cfg)
                self.assertEqual(os.environ['H3_URL'], 'http://explicit:30010')
            cfg.write_text('[environment]\nOPENAI_API_KEY = "do-not-store"\n')
            with self.assertRaisesRegex(ValueError, 'in your shell'):
                cli.load_config(cfg)

    def test_launcher_keeps_stage_arguments_and_profile(self):
        expected = {
            'initialization': ('game_initialization.agentic', [], ['prepare', '--bootstrap-only']),
            'refinement': ('gamecraft_training.package', ['--profile', 'initialization'], ['--rounds', '3']),
            'evolution': ('gamecraft_training.package', ['--profile', 'rsi'], ['--rounds', '3']),
        }
        for command in [*expected, *cli.ALIASES]:
            canonical = cli.ALIASES.get(command, command)
            module, prefix, arguments = expected[canonical]
            with self.subTest(command=command), patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', []):
                with patch('play2evolve.cli.runpy.run_module') as run:
                    cli.main([command, *arguments])
                    run.assert_called_once_with(module, run_name='__main__')
                    self.assertEqual(sys.argv, ['play2evolve ' + canonical, *prefix, *arguments])

    def test_profile_cannot_override_refinement_or_evolution_stage(self):
        for command in ('refinement', 'evolution', 'initialize', 'rsi'):
            for flag in (['--profile', 'rsi'], ['--profile=initialization']):
                with self.subTest(command=command, flag=flag), patch.dict(os.environ, {}, clear=True):
                    with patch('play2evolve.cli.runpy.run_module') as run:
                        with self.assertRaises(SystemExit) as error:
                            cli.main([command, *flag])
                        self.assertEqual(error.exception.code, 2)
                        run.assert_not_called()

    def test_package_resources_and_framework_hash(self):
        from game_initialization.agentic import __main__ as bootstrap
        from game_initialization.batch.__main__ import framework_hash
        from gamecraft_behavior import runtime as behavior
        self.assertTrue((Path(bootstrap.__file__).parent / 'prompt.txt').is_file())
        self.assertTrue((Path(behavior.__file__).parent / 'bridge.gd').is_file())
        self.assertEqual(len(framework_hash()), 64)

    def test_imports_without_credentials_from_an_unrelated_working_directory(self):
        env = {k: v for k, v in os.environ.items()
               if k not in {'OPENAI_API_KEY', 'CODEX_API_KEY', 'PYTHONPATH'}}
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, '-c',
                'import llm, gamecraft_training.package.loop, gamecraft_assets.generate; '
                'assert llm.client is None'], cwd=tmp, env=env,
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
