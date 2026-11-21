import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter
from gamecraft_behavior.program_policy import compile_policy, program_player


def large_source():
    # Real statements as well as >6000 characters, not just padded comments.
    body = ''.join(f'    observed_signal_{i:03} = state.get("bird_center_y", 0)\n' for i in range(120))
    return ('def act(state, recent):\n' + body +
            '    if state.get("_profile") == "career":\n'
            '        return {"action": "tap", "reason": "start career"}\n'
            '    return {"action": "wait", "reason": "observe other profile"}')


class InitializationProgramLimitsTests(unittest.TestCase):
    def test_three_profile_program_authors_and_reloads_without_new_call(self):
        source = large_source()
        self.assertGreater(len(source), 6000)
        context = {'phase': 'initialization', 'profiles': [
            {'profile': name} for name in ('career', 'stamina', 'championship')]}
        calls = []
        def call(*args, **kwargs):
            calls.append((args, kwargs))
            return {'source': source}
        fake = SimpleNamespace(call=call, img=lambda *a, **kw: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(tmp)
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            author = program_player(root, root/'first/policy.py', adapter=DreadWingsAdapter(),
                                    playtest_context=context)
            author.set_profile('career')
            self.assertEqual(author('frame.png', '', [], 6, current_state={'phase': 'title'})['action'], 'tap')
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]['max_tokens'], 6600)
            self.assertNotIn('program limit', calls[0][0][1])
            self.assertTrue(json.loads((root/'first/policy-attempts/001.json').read_text())['accepted'])
            inherited = program_player(root, root/'next/policy.py', adapter=DreadWingsAdapter(),
                playtest_context={**context, 'reuse_source': (root/'first/policy.py').read_text()})
            inherited.set_profile('stamina')
            self.assertEqual(inherited('frame.png', '', [], 6, current_state={'phase': 'fight'})['action'], 'wait')
            self.assertEqual(len(calls), 1)

    def test_long_programs_are_allowed_but_syntax_restrictions_remain(self):
        compile_policy(large_source())
        source = ('def act(state, recent):\n' +
                  ''.join(f'    observed_signal_{i:03} = state.get("bird_center_y", 0)\n'
                          for i in range(500)) + '    return {}')
        self.assertGreater(len(source), 21000)
        compile_policy(source)
        for source in ('def act(state, recent):\n    import os\n    return {}',
                       'def act(state, recent):\n    while True: pass\n    return {}'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                compile_policy(source)

    def test_failed_attempt_preserves_source_and_size_for_diagnosis(self):
        source = ('def act(state, recent):\n' + ('    # padding\n' * 1500) +
                  '    import os\n    return {}')
        fake = SimpleNamespace(call=lambda *a, **kw: {'source': source},
                               img=lambda *a, **kw: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(tmp)
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            author = program_player(root, root/'policy.py', adapter=DreadWingsAdapter(),
                playtest_context={'phase': 'initialization', 'profiles': [{}, {}, {}]})
            with self.assertRaisesRegex(RuntimeError, 'Unsupported policy syntax: Import'):
                author('frame.png', '', [], 6, current_state={})
            report = json.loads((root/'policy-attempts/003.json').read_text())
            self.assertFalse(report['accepted'])
            self.assertEqual(report['source_chars'], len(source))
            self.assertEqual((root/'policy-attempts/003.py').read_text(), source + '\n')

    def test_initialization_rewrites_unobservable_state_reads(self):
        bad = ('def act(state, recent):\n'
               '    if state.get("inventory", ""):\n'
               '        return {"action": "tap", "reason": "use inventory"}\n'
               '    return {"action": "wait", "reason": "observe"}')
        good = ('def act(state, recent):\n'
                '    if state.get("phase") == "playing" and state.get("bird_center_y", 0) > 300:\n'
                '        return {"action": "tap", "reason": "rise"}\n'
                '    return {"action": "wait", "reason": "observe"}')
        requests = []
        sources = iter((bad, good))
        def call(*args, **kwargs):
            requests.append(json.loads(args[2][0]['text']))
            return {'source': next(sources)}
        fake = SimpleNamespace(call=call, img=lambda *a, **kw: {'type': 'input_image'})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(tmp)
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            context = {'phase': 'initialization', 'profiles': [{'profile': 'core'}]}
            state = {'phase': 'playing', 'bird_center_y': 320}
            author = program_player(root, root/'fresh/policy.py', adapter=DreadWingsAdapter(),
                                    playtest_context=context)
            self.assertEqual(author('frame.png', '', [], 6, current_state=state)['action'], 'tap')
            self.assertIn("'inventory' is not observable", requests[1]['previous_error'])
            self.assertFalse(json.loads((root/'fresh/policy-attempts/001.json').read_text())['accepted'])
            self.assertTrue(json.loads((root/'fresh/policy-attempts/002.json').read_text())['accepted'])
            self.assertEqual((root/'fresh/policy.py').read_text().strip(), good)

        requests.clear()
        sources = iter((good,))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'llm': fake}):
            root = Path(tmp)
            (root/'game_plan.json').write_text(json.dumps({'task_id': 'platformer-dread-wings'}))
            inherited = program_player(root, root/'reused/policy.py', adapter=DreadWingsAdapter(),
                playtest_context={**context, 'reuse_source': bad})
            self.assertEqual(inherited('frame.png', '', [], 6, current_state=state)['action'], 'tap')
            self.assertIn("'inventory' is not observable", requests[0]['previous_error'])
            self.assertEqual((root/'reused/policy.py').read_text().strip(), good)
