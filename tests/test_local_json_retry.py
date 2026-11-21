import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock


class LocalJsonRetryTests(unittest.TestCase):
    def run_call(self,responses):
        # Exercise the actual transport function without importing credential-bound clients.
        source=Path(__file__).resolve().parents[1]/'src/llm.py'
        tree=ast.parse(source.read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_local')
        create=Mock(side_effect=responses)
        scope={'json':json,'local':NS(chat=NS(completions=NS(create=create)))}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),scope)
        result=scope['_local']('local','rules',[],'action',{},None,350)
        return result,create

    def response(self,text,finish='stop',tokens=10):
        return NS(choices=[NS(message=NS(content=text),finish_reason=finish)],usage=NS(prompt_tokens=100,completion_tokens=tokens))

    def test_truncation_increases_budget_and_counts_retries(self):
        (value,usage),create=self.run_call([self.response('{"reason":"long','length',350),self.response('{"reason":"still long','length',700),self.response('{"action":"wait","reason":"Recover"}')])
        self.assertEqual(value['action'],'wait')
        self.assertEqual([c.kwargs['max_tokens'] for c in create.call_args_list],[350,700,1400])
        self.assertEqual(usage['input'],300)
        self.assertEqual(usage['output'],1060)

    def test_other_json_errors_retry_without_increasing_budget(self):
        (_,usage),create=self.run_call([self.response('not json'),self.response('{"action":"jab"}')])
        self.assertEqual([c.kwargs['max_tokens'] for c in create.call_args_list],[350,350])
        self.assertEqual(usage['output'],20)

    def test_exhaustion_does_not_fabricate_an_action(self):
        with self.assertRaises(RuntimeError):
            self.run_call([self.response('{','length',350)]*3)
