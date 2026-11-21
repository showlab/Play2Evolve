"""Capacity failures may be retried once without masking other Codex errors."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameasg.method import codex


class CodexCapacityRetryTests(unittest.TestCase):
    def invoke(self, responses):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            (home / 'auth.json').write_text('{}')
            binary = root / 'codex'
            binary.touch()
            args = argparse.Namespace(codex_home=home, expected_account_hash='',
                                      codex=binary, model='test-model', codex_timeout=10)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                code, output = responses[len(calls) - 1]
                kwargs['stdout'].write(output)
                if code == 0:
                    Path(command[command.index('--output-last-message') + 1]).write_text('done')
                return argparse.Namespace(returncode=code)

            log = root / 'player.log'
            with patch('gameasg.method.from_args', return_value={}), \
                 patch('gameasg.method.child_environment', return_value={}), \
                 patch('gameasg.method.cli_args', return_value=[]), \
                 patch('gameasg.method.subprocess.run', side_effect=run), \
                 patch('gameasg.method.time.sleep') as sleep:
                if responses[-1][0] == 0:
                    self.assertEqual(codex(args, root, 'play', log), 'done')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Codex exited 1'):
                        codex(args, root, 'play', log)
            return len(calls), sleep.call_count, log.read_text(), (
                (root / 'player.capacity-attempt-1.log').read_text()
                if (root / 'player.capacity-attempt-1.log').exists() else None)

    def test_capacity_failure_retries_once_and_preserves_first_log(self):
        count, sleeps, final, first = self.invoke([
            (1, 'ERROR: Selected model is at capacity. Please try a different model.'),
            (0, 'success')])
        self.assertEqual((count, sleeps, final), (2, 1, 'success'))
        self.assertIn('at capacity', first)

    def test_capacity_failure_retries_only_once(self):
        count, sleeps, final, first = self.invoke([
            (1, 'ERROR: Selected model is at capacity.'),
            (1, 'ERROR: Selected model is at capacity.')])
        self.assertEqual((count, sleeps), (2, 1))
        self.assertIn('at capacity', final)
        self.assertIn('at capacity', first)

    def test_unrelated_failure_is_not_retried(self):
        count, sleeps, final, first = self.invoke([(1, 'Invalid project input')])
        self.assertEqual((count, sleeps, first), (1, 0, None))
        self.assertEqual(final, 'Invalid project input')


if __name__ == '__main__':
    unittest.main()
