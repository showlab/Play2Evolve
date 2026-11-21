import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from gamecraft_behavior.core import write_json
from gamecraft_training.package.codex_edit import CodexEditor,serve_material_requests
from gamecraft_training.package.material_tool import invoke
from tests.test_boundary_candidates import asset


class JointExecutorTests(unittest.TestCase):
    def test_one_executor_calls_h3_once_then_edits_code_and_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'original';project.mkdir()
            (project/'project.godot').write_text('config_version=5')
            (project/'main.gd').write_text('extends Node')
            a=asset();a.pop('reference_asset_id')
            plan={'schema_version':1,'task_id':'test','style':'arcade','assets':[a]}
            bindings={'actor':['assets/a0.png','assets/a1.png']}
            cfg={'_package_root':str(root),'_profile':{'name':'initialization'},'assets':{'bindings':bindings}}
            ctx={'plan':plan,'config':cfg,'option':{'materials':[{'operation':'add','asset':a}]},
                 'python':'python','video_skill':False,'skill_source':str(root/'none')}
            calls=[]
            def generate(where,**kw):
                self.assertFalse(kw['judge']);self.assertFalse(kw['propose_video_patch'])
                calls.append(json.loads((where/'asset_plan.json').read_text())['assets'][0]['prompt'])
                files=[]
                generated=where/'generated/actor/v0001';generated.mkdir(parents=True,exist_ok=True)
                for i in range(2):
                    f=generated/f'frame{i}.png';im=Image.new('RGBA',(16,16));im.putpixel((8,8),(0,100,0,255));im.save(f)
                    files.append(str(f.relative_to(where)))
                write_json(where/'asset_manifest.json',{'assets':{'actor':{'files':files,'version':len(calls)}}})
                return []
            def execute(job,**kw):
                self.assertFalse((job/'project/assets/a0.png').exists())
                self.assertTrue((job/'material_tool.json').exists())
                invoke(job,'actor','First complete motion prompt',host=True)
                self.assertTrue((job/'project/assets/a0.png').exists())
                with self.assertRaisesRegex(RuntimeError,'budget exhausted'):
                    invoke(job,'actor','Forbidden retry',host=True)
                (job/'project/main.gd').write_text('extends Node\n# integrate actual animation')
                (job/'project/assets/a0.png.import').write_text('[remap]')
                write_json(job/'response.json',{'status':'completed','answer':{'summary':'joint edit','tests':[]}})
            out=root/'out';out.mkdir()
            with patch('gamecraft_training.package.codex_edit.execute_job',side_effect=execute) as editor_call, \
                 patch('gamecraft_training.package.material_tool.generate',side_effect=generate), \
                 patch('gamecraft_training.package.material_tool.validate_bound'):
                result=CodexEditor()(project,root/'candidate',out,diagnosis={},evaluation={},episodes=[],
                    history=[],adapter=SimpleNamespace(instructions='rules'),policy={'protected':['assets']},
                    material_execution=ctx)
            self.assertEqual(editor_call.call_count,1)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(result['generations']),1)
            self.assertEqual(result['material_plan']['assets'][0]['prompt'],calls[-1])
            self.assertTrue((root/'candidate/assets/a0.png').exists())
            self.assertFalse((project/'assets').exists())

    def test_failed_generation_does_not_install_stale_material(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'generation').mkdir();(root/'project').mkdir()
            a=asset();a.pop('reference_asset_id')
            write_json(root/'generation/asset_plan.json',{'schema_version':1,'task_id':'test','style':'arcade','assets':[a]})
            write_json(root/'generation/asset_manifest.json',{'assets':{}})
            write_json(root/'material_tool.json',{'asset_ids':['actor'],'bindings':{'actor':['a.png','b.png']},
                                                'video_skill':False,'phase':'initialization'})
            with patch('gamecraft_training.package.material_tool.generate',return_value=['actor']):
                with self.assertRaisesRegex(RuntimeError,'H3 failed'):invoke(root,'actor','Complete motion prompt')
                with self.assertRaisesRegex(RuntimeError,'budget exhausted'):invoke(root,'actor','Forbidden retry')
            self.assertEqual(list((root/'project').iterdir()),[])

    def test_codex_material_request_runs_on_parent_host(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            write_json(job/'material_tool.json',{'asset_ids':['actor'],'host_execution':True,
                                                'host_tool_timeout':5})
            received=[]
            def requester():
                received.append(invoke(job,'actor','airborne bird loop'))
            thread=threading.Thread(target=requester);thread.start()
            deadline=time.monotonic()+3
            while not list((job/'material-requests').glob('*.request.json')):
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
            with patch('gamecraft_training.package.material_tool.invoke',return_value={'asset':'actor','status':'generated'}) as run:
                self.assertEqual(serve_material_requests(job),1)
                self.assertEqual(serve_material_requests(job),0)
                run.assert_called_once_with(job.resolve(),'actor','airborne bird loop',host=True)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(received,[{'asset':'actor','status':'generated'}])

if __name__=='__main__':unittest.main()
