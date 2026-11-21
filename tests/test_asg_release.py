import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from play2evolve import asg, cli


class ASGReleaseTests(unittest.TestCase):
    def test_each_stage_forwards_without_changing_pipeline(self):
        cases = {
            'initialization': ('gameasg.initialization', ['--stage', 'bootstrap']),
            'refinement': ('gameasg.initialization', ['--stage', 'optimize', '--resume']),
            'evolution': ('gameasg.rsi', []),
            'evaluate': ('gameasg', ['score']),
            'environment': ('gameasg.environment', []),
        }
        for command, (module, prefix) in cases.items():
            with self.subTest(command=command), patch.object(sys, 'argv', []):
                with patch.object(asg.runpy, 'run_module') as run:
                    asg.main([command, '--help'])
                    run.assert_called_once_with(module, run_name='__main__')
                    self.assertEqual(sys.argv[1:], [*prefix, '--help'])

    def test_stage_override_is_rejected_before_execution(self):
        for command in ('initialization', 'refinement', 'evolution'):
            for argument in (['--stage', 'all'], ['--stage=all']):
                with self.subTest(command=command), patch.object(asg.runpy, 'run_module') as run:
                    with self.assertRaises(SystemExit) as error:
                        asg.main([command, *argument])
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()

    def test_asg_does_not_require_the_craft_benchmark(self):
        with patch.dict(os.environ, {'GAMECRAFT_BENCH_ROOT': '/missing/craft'}), patch.object(sys, 'argv', []):
            with patch.object(cli, 'configure_benchmark', side_effect=AssertionError('Craft dependency')):
                with patch.object(cli.runpy, 'run_module') as run:
                    cli.main(['asg', 'initialization', '--help'])
                    run.assert_called_once_with('play2evolve.asg', run_name='__main__')

    def test_browser_resources_and_no_private_account_default(self):
        import gameasg
        from gameasg.initialization import parser
        root = Path(gameasg.__file__).parent
        for name in ('asset_runtime.js', 'browser_player.js', 'browser_probe.js'):
            self.assertTrue((root / name).is_file(), name)
        self.assertEqual(parser().get_default('expected_account_hash'), '')
