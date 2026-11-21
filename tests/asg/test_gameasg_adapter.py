import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gameasg import __main__ as asg


HTML = '''<!doctype html><html><head><style>body{color:white}</style></head>
<body><canvas></canvas><script>
window.__gameTest = {reset(){return {}}, input(){return {}},
getSnapshot(){return {}}, loadScenario(){return {}}};
</script></body></html>'''


class GameASGAdapterTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        task = root / 'task/capys-flight-adventure'
        task.mkdir(parents=True)
        for name in asg.DOCS:
            (task / name).write_text('Public fixture game requirements.')
        self.enterContext(patch.object(asg, 'benchmark', return_value=root))

    def test_prepare_copies_only_public_documents_and_freeze_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.html'
            source.write_text(HTML)
            work = root / 'work'
            asg.prepare(argparse.Namespace(bench=asg.DEFAULT_BENCH,
                                           task='capys-flight-adventure', output=work))
            self.assertEqual({p.name for p in work.iterdir()},
                             {'target.md', 'game-spec.md', 'tdd.md', 'game', 'manifest.json'})
            self.assertEqual(json.loads((work / 'manifest.json').read_text())['benchmark_commit'],
                             asg.BENCH_COMMIT)
            asg.freeze(argparse.Namespace(workspace=work, html=source))
            self.assertEqual((work / 'game/index.html').read_text(), HTML)
            with self.assertRaisesRegex(ValueError, 'already frozen'):
                asg.freeze(argparse.Namespace(workspace=work, html=source))
            (work / 'game/index.html').write_text(HTML.replace('color:white', 'color:red'))
            with self.assertRaisesRegex(ValueError, 'Frozen HTML has changed'):
                asg.score(argparse.Namespace(workspace=work, exp_id='unit-test', image=None, timeout=1))

    def test_rejects_local_companion_assets_and_incomplete_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.html'
            path.write_text(HTML.replace('<canvas>', '<img src="assets/sprite.png"><canvas>'))
            with self.assertRaisesRegex(ValueError, 'Local companion resource'):
                asg.validate_html(path)
            path.write_text(HTML.replace('loadScenario(){return {}}', 'other(){return {}}'))
            with self.assertRaisesRegex(ValueError, 'loadScenario'):
                asg.validate_html(path)

    def test_invalid_task_and_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'Invalid GameASG task'):
                asg.prepare(argparse.Namespace(bench=asg.DEFAULT_BENCH,
                                               task='../tests', output=root / 'work'))
            (root / 'work').mkdir()
            with self.assertRaises(FileExistsError):
                asg.prepare(argparse.Namespace(bench=asg.DEFAULT_BENCH,
                                               task='capys-flight-adventure', output=root / 'work'))


if __name__ == '__main__':
    unittest.main()
