import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from game_initialization.batch.__main__ import needs_local_service
from gamecraft_assets.generate import _repair_prompt

class DeveloperInitializationTests(unittest.TestCase):
    def test_fixed_replay_initialization_without_local_model(self):
        cfg={'initialization_profile':'shared-rsi-v1','bootstrap_only':False,
             'judge_backend':'codex','asset_prompt_repair_backend':'codex','asset_judge':False}
        self.assertFalse(needs_local_service(cfg))
        self.assertTrue(needs_local_service({**cfg,'judge_backend':'local'}))
        self.assertTrue(needs_local_service({**cfg,'asset_prompt_repair_backend':'local'}))
        self.assertFalse(needs_local_service({**cfg,'bootstrap_only':True,'asset_prompt_repair_backend':'local'}))

    def test_legacy_checks_stay_conservative(self):
        self.assertTrue(needs_local_service({'bootstrap_only':False,'judge_backend':'codex','asset_judge':False}))

    def test_prompt_repair_selects_codex(self):
        call=Mock(return_value={'prompt':'A boxer jumps.'})
        with patch.dict(os.environ,{'GAMECRAFT_ASSET_PROMPT_REPAIR_BACKEND':'codex',
                                    'GAMECRAFT_ASSET_PROMPT_REPAIR_MODEL':'gpt-6-sol'}):
            self.assertEqual(_repair_prompt(SimpleNamespace(llm=SimpleNamespace(call=call)),{},'style',['failure']),
                             'A boxer jumps.')
        self.assertEqual(call.call_args.args[0],'gpt-6-sol')
        self.assertEqual(call.call_args.kwargs['backend'],'codex')

    def test_prompt_repair_default_remains_local(self):
        call=Mock(return_value={'prompt':'old behavior'})
        with patch.dict(os.environ,{},clear=True):
            _repair_prompt(SimpleNamespace(llm=SimpleNamespace(call=call)),{},'style',[])
        self.assertEqual(call.call_args.args[0],'local')
        self.assertEqual(call.call_args.kwargs['backend'],'local')

    def test_nonlocal_repair_requires_model(self):
        with patch.dict(os.environ,{'GAMECRAFT_ASSET_PROMPT_REPAIR_BACKEND':'codex',
                                    'GAMECRAFT_ASSET_PROMPT_REPAIR_MODEL':'local'}):
            with self.assertRaises(ValueError):_repair_prompt(None,{},'',[])
