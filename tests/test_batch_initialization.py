import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from game_initialization.batch import __main__ as b


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root/'test-game'
        (self.task/'generation').mkdir(parents=True)
        (self.task/'draft/game/assets').mkdir(parents=True)
        (self.task/'instruction.md').write_text('Build a Godot 4 game. Include a title screen and punch animation.')
        b.write(self.task/'requirements.json', {'requirements':[{'id':'R1','quote':'punch animation',
            'description':'Punch', 'asset_ids':['punch'],'verification':'Play animation in bout'}]})
        b.write(self.task/'generation/asset_plan.json', {'schema_version':1,'task_id':'test-game','style':'pixel art',
            'assets':[{'id':'punch','kind':'animation','description':'boxer','requirements':'one punch','prompt':'Punch once',
                'size':[768,768],'frames':2,'fps':30,'reference_prompt':'Full body {desc}', 'loop':False}]})

    def test_prepare_copies_brief_without_models_and_rejects_overwrite(self):
        bench=self.root/'bench'; p=bench/'tasks/test-game';p.mkdir(parents=True)
        (p/'instruction.md').write_text((self.task/'instruction.md').read_text())
        b.write(self.root/'tasks.json',['test-game'])
        args=argparse.Namespace(tasks=self.root/'tasks.json',bench=bench,output=self.root/'batch',
            model='gpt-5.5',host='remote',codex='codex',asset_python='/python',godot='/godot',
            h3_url='http://localhost:1',local_url='http://localhost:2/v1',h3_rounds=2,candidates=1,repairs=2,timeout=99)
        with patch.object(b,'codex',side_effect=AssertionError('Unexpected model call')):
            b.prepare(args)
            self.assertEqual(b.read(args.output/'batch.json')['model'],'gpt-5.5')
            self.assertEqual((args.output/'test-game/instruction.md').read_text(),(p/'instruction.md').read_text())
            with self.assertRaises(FileExistsError):b.prepare(args)

    def test_plan_rejects_invented_source_quote(self):
        b.validate_plan(self.task)
        d=b.read(self.task/'requirements.json');d['requirements'][0]['quote']='made up requirement'
        b.write(self.task/'requirements.json',d)
        with self.assertRaises(ValueError):b.validate_plan(self.task)

    def test_plan_rejects_unmapped_asset(self):
        d=b.read(self.task/'requirements.json');d['requirements'][0]['asset_ids']=[]
        b.write(self.task/'requirements.json',d)
        with self.assertRaises(ValueError):b.validate_plan(self.task)

    def test_asset_install_records_hashes_and_does_not_use_legacy_assets(self):
        (self.task/'generation/frame.png').write_bytes(b'generated image')
        b.write(self.task/'generation/asset_manifest.json', {'assets':{'punch':{'files':['frame.png','frame.png']}}})
        b.install_assets(self.task)
        self.assertEqual((self.task/'draft/game/assets/punch/frame0000.png').read_bytes(),b'generated image')
        self.assertEqual(b.read(self.task/'immutable_inputs.json'),b.guard_files(self.task))
        (self.task/'draft/game/assets/punch/frame0000.png').write_bytes(b'replaced')
        self.assertNotEqual(b.read(self.task/'immutable_inputs.json'),b.guard_files(self.task))

    def test_missing_assets_preserve_plan_and_allow_code_stage(self):
        b.write(self.task/'generation/asset_manifest.json', {'assets':{}})
        b.install_assets(self.task)
        missing=b.read(self.task/'draft/missing_assets.json')
        self.assertEqual(missing['missing'],['punch'])
        self.assertFalse(missing['ready_for_training'])
        self.assertEqual(b.read(self.task/'draft/asset_bindings.json')['punch'],['assets/punch/frame0000.png','assets/punch/frame0001.png'])
        b.write(self.task/'state.json',{'stage':'assets_ready','next_attempt':0})
        cfg={'brief_sha256':{'test-game':b.sha(self.task/'instruction.md')},'godot':'godot','repairs':0}
        with patch.object(b,'codex') as invoke:
            b.initialize(self.task,cfg,'contract')
            self.assertIn('Continue implementing',invoke.call_args.args[3])
        self.assertEqual(b.read(self.task/'state.json')['stage'],'code_ready_missing_assets')
        self.assertFalse((self.task/'initial-v0').exists())

    def test_asset_path_escape_rejected(self):
        with self.assertRaises(ValueError):b.inside(self.task,'../secret')
        (self.task/'link').symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):b.inside(self.task,'link')

    def test_codex_exact_model_and_no_fallback(self):
        cfg={'codex':'codex','model':'gpt-5.5','timeout':10}
        with patch.object(b,'command') as cmd:
            b.codex(self.task,cfg,'plan','Read full brief')
            argv=cmd.call_args.args[0]
            self.assertEqual(argv[argv.index('--model')+1],'gpt-5.5')
            self.assertEqual(argv[argv.index('--sandbox')+1],'workspace-write')
        with patch.object(b,'command',side_effect=RuntimeError('model unavailable')) as cmd:
            with self.assertRaises(RuntimeError):b.codex(self.task,cfg,'plan','x')
            self.assertEqual(cmd.call_count,1)

    def test_independent_review_is_read_only(self):
        cfg={'codex':'codex','model':'gpt-5.5','timeout':10}
        (self.task/'logs').mkdir()
        b.write(self.task/'logs/review.answer.json', {'passed':False})
        with patch.object(b,'command') as cmd:
            b.codex(self.task,cfg,'review','Review',schema=b.REVIEW_SCHEMA)
            argv=cmd.call_args.args[0]
            self.assertEqual(argv[argv.index('--sandbox')+1],'read-only')

    def test_explicit_reasoning_effort_reaches_codex_without_changing_default(self):
        cfg={'codex':'codex','model':'gpt-6-astra','timeout':10}
        with patch.object(b,'command') as cmd:
            b.codex(self.task,cfg,'default','Build')
            self.assertFalse(any('model_reasoning_effort' in x for x in cmd.call_args.args[0]))
            b.codex(self.task,{**cfg,'reasoning_effort':'high'},'high','Build')
            self.assertIn('model_reasoning_effort="high"',cmd.call_args.args[0])
        with patch.object(b,'command') as cmd:
            with self.assertRaises(ValueError):
                b.codex(self.task,{**cfg,'reasoning_effort':'invalid'},'bad','Build')
            cmd.assert_not_called()

    def test_bootstrap_can_explicitly_share_h3_without_relaxing_default(self):
        cfg={'host':'remote','framework_sha256':'hash','asset_python':'python',
             'godot':'godot','codex':'codex','asset_judge':False,'bootstrap_only':True,
             'judge_backend':'codex','h3_url':'http://localhost:1'}
        with patch.object(b.socket,'gethostname',return_value='remote'), \
             patch.object(b,'framework_hash',return_value='hash'), \
             patch.object(b.shutil,'which',side_effect=lambda x:x), \
             patch.object(b,'command'),patch.object(b.Path,'glob',return_value=[]), \
             patch.object(b,'get_json',return_value=[{'id':'other-job','status':'processing'}]), \
             patch.object(b.subprocess,'check_output',return_value='GPU'):
            with self.assertRaisesRegex(RuntimeError,'Existing training/H3 work'):
                b.preflight(self.root,cfg)
            b.preflight(self.root,{**cfg,'allow_shared_services':True})

    def test_review_requires_coverage_and_real_evidence(self):
        report={'passed':True,'missing_requirements':[], 'requirements':[{'id':'R1','passed':True,'evidence':['instruction.md'],'reason':'ok'}]}
        self.assertTrue(b.review_passed(self.task,report))
        report['missing_requirements']=['menu omitted'];self.assertFalse(b.review_passed(self.task,report))
        report['missing_requirements']=[];report['requirements'][0]['evidence']=['nonexistent.png']
        self.assertFalse(b.review_passed(self.task,report))
        report['requirements']=[];self.assertFalse(b.review_passed(self.task,report))

    def test_host_guard_precedes_service_calls(self):
        with patch.object(b.socket,'gethostname',return_value='developer'),patch.object(b,'get_json') as req:
            with self.assertRaisesRegex(RuntimeError,'Wrong host'):b.preflight(self.root,{'host':'training'})
            req.assert_not_called()

    def test_local_service_is_optional_only_for_codex_bootstrap(self):
        bootstrap={'asset_judge':False,'bootstrap_only':True,'judge_backend':'codex'}
        self.assertFalse(b.needs_local_service(bootstrap))
        self.assertTrue(b.needs_local_service({**bootstrap,'judge_backend':'local'}))
        self.assertTrue(b.needs_local_service({**bootstrap,'asset_judge':True}))
        self.assertTrue(b.needs_local_service({**bootstrap,'bootstrap_only':False}))

    def test_resumed_implementation_rejects_changed_inputs(self):
        (self.task/'generation/frame.png').write_bytes(b'original')
        b.write(self.task/'generation/asset_manifest.json',{'assets':{'punch':{'files':['frame.png','frame.png']}}})
        b.install_assets(self.task)
        b.write(self.task/'state.json',{'stage':'assets_ready','next_attempt':0})
        (self.task/'draft/game/assets/punch/frame0000.png').write_bytes(b'modified')
        cfg={'brief_sha256':{'test-game':b.sha(self.task/'instruction.md')},'godot':'godot','repairs':2}
        with patch.object(b,'codex') as invoke:
            with self.assertRaisesRegex(RuntimeError,'Immutable inputs changed'):b.initialize(self.task,cfg,'contract')
            invoke.assert_not_called()

    def test_failure_never_publishes_ready_package(self):
        (self.task/'generation/frame.png').write_bytes(b'original')
        b.write(self.task/'generation/asset_manifest.json',{'assets':{'punch':{'files':['frame.png','frame.png']}}})
        b.install_assets(self.task)
        b.write(self.task/'state.json',{'stage':'assets_ready','next_attempt':0})
        cfg={'brief_sha256':{'test-game':b.sha(self.task/'instruction.md')},'godot':'godot','repairs':1}
        with patch.object(b,'codex',side_effect=RuntimeError('author failed')) as invoke:
            with self.assertRaisesRegex(RuntimeError,'budget exhausted'):b.initialize(self.task,cfg,'contract')
            self.assertEqual(invoke.call_count,2)
        self.assertFalse((self.task/'initial-v0').exists())
        with patch.object(b,'codex') as invoke:
            with self.assertRaisesRegex(RuntimeError,'budget exhausted'):b.initialize(self.task,cfg,'contract')
            invoke.assert_not_called()


if __name__=='__main__':unittest.main()
