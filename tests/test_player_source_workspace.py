"""Exercise Player-to-Codex workspace handoff without a paid model call."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter
from gamecraft_behavior.core import digest
from gamecraft_behavior.program_policy import program_player


class PlayerSourceWorkspaceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'workspace_test_llm', Path(__file__).resolve().parents[1] / 'src/llm.py')
        self.llm = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'LLM_BACKEND': 'codex', 'OPTIMIZER_BACKEND': 'codex'}, clear=True):
            spec.loader.exec_module(self.llm)

    def tearDown(self):
        self.llm.USAGE['calls'] = 0

    def test_player_can_read_late_code_and_nested_dependencies_in_its_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'candidate'
            (project / 'scripts').mkdir(parents=True)
            (project / 'mechanics').mkdir()
            (project / 'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            late_code = 'const TIMING_WINDOW = 0.6\n'
            (project / 'scripts/main.gd').write_text('# padding\n' * 2000 + late_code)
            (project / 'mechanics/timing.gd').write_text('const TRACE_FPS = 30\n')
            original = digest(project)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                workspace = Path(command[command.index('--cd') + 1])
                self.assertEqual(workspace, project.resolve())
                self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
                self.assertNotIn('--add-dir', command)
                self.assertTrue((workspace / 'scripts/main.gd').read_text().endswith(late_code))
                self.assertIn('TRACE_FPS', (workspace / 'mechanics/timing.gd').read_text())
                prompt = kwargs['input']
                self.assertIn('0.2 seconds, 12 physics ticks', prompt)
                self.assertIn('game_source_workspace', prompt)
                self.assertNotIn('"game_source":', prompt)
                self.assertNotIn('# padding', prompt)
                response = Path(command[command.index('--output-last-message') + 1])
                self.assertNotIn(project, response.parents)
                response.write_text(json.dumps({'source':
                    'def act(state, recent):\n    return {"action": "wait", "reason": "observe"}'}))
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch.dict(sys.modules, {'llm': self.llm}), \
                 patch.object(self.llm, 'img', return_value={'type': 'input_text', 'text': 'image fixture'}), \
                 patch.object(self.llm.subprocess, 'run', side_effect=run), \
                 patch.dict(os.environ, {'GAMECRAFT_CODEX_PROVIDER': 'openai'}):
                player = program_player(project, root / 'replay/policy.py', model='test', backend='codex',
                    adapter=DreadWingsAdapter(), playtest_context={'phase': 'initialization',
                        'candidate_changes': {'timing': 'changed'}, 'profiles': [{'profile': 'core'}]})
                self.assertEqual(player('unused.png', '', [], 6, current_state={'phase': 'ready'})['action'], 'wait')
            self.assertEqual(len(calls), 1)
            self.assertEqual(digest(project), original)
            self.assertTrue((root / 'replay/policy.py').is_file())

    def test_ordinary_codex_calls_keep_temporary_workspace(self):
        def run(command, **kwargs):
            workspace = Path(command[command.index('--cd') + 1])
            schema = Path(command[command.index('--output-schema') + 1])
            self.assertEqual(workspace, schema.parent)
            Path(command[command.index('--output-last-message') + 1]).write_text('{"ok": true}')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(self.llm.subprocess, 'run', side_effect=run), \
             patch.dict(os.environ, {'GAMECRAFT_CODEX_PROVIDER': 'openai'}):
            result = self.llm.call('test', 'inspect', [], 'test', {}, backend='codex')
        self.assertEqual(result, {'ok': True})

    def test_non_tool_backend_rejects_workspace_instead_of_silently_ignoring_it(self):
        with self.assertRaisesRegex(ValueError, 'requires the Codex backend'):
            self.llm.call('test', '', [], 'test', {}, backend='local', read_only_project='/unused')


if __name__ == '__main__':
    unittest.main()
