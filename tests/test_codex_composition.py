import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import threading
import time
from gamecraft_behavior.core import write_json,digest
from gamecraft_training.package.codex_edit import DEFAULT_CODEX_MODEL,CodexEditor,audit,execute_job,policy,validate_project


class CodexCompositionTests(unittest.TestCase):
    def test_editor_history_window_limits_request(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root)
            history=[{'round':number} for number in range(1,5)]
            for window,expected in ((0,[]),(2,history[-2:])):
                out=root/f'trial-{window}';out.mkdir()
                def execute(job,**kwargs):
                    request=json.loads((job/'request.json').read_text())
                    self.assertEqual(request['previous_attempts'],expected)
                    (job/'project/main.gd').write_text(f'extends Node2D\n# {window}\n')
                    write_json(job/'response.json',{'status':'completed',
                               'answer':{'summary':'edited','tests':[]}})
                with self.subTest(window=window), \
                     patch('gamecraft_training.package.codex_edit.execute_job',side_effect=execute):
                    CodexEditor(history_window=window)(project,root/f'candidate-{window}',out,
                        diagnosis={},evaluation={},episodes=[],history=history,
                        adapter=SimpleNamespace(instructions='rules'),policy={'protected':[]})

    def test_default_code_editor_model(self):
        self.assertEqual(DEFAULT_CODEX_MODEL,'gpt-6-sol')

    def test_intermediate_state_change_is_rejected_even_if_final_matches(self):
        from gamecraft_training.package.loop import verify_episode
        adapter=SimpleNamespace(equivalent=lambda a,b:a==b)
        original={'replay':{'hp':100},'decisions':[{'evaluation_state':{'hp':90}}]}
        changed={'replay_verified':True,'replay':{'hp':100},'decisions':[{'evaluation_state':{'hp':80}}]}
        with self.assertRaisesRegex(RuntimeError,'intermediate'):
            verify_episode(adapter,original,changed)

    def test_shared_queue_publishes_complete_request_and_collects_edit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root);queue=root/'queue';queue.mkdir()
            out=root/'trial';out.mkdir();errors=[]
            def worker():
                try:
                    deadline=time.monotonic()+5
                    while not list(queue.glob('*/ready')):
                        if time.monotonic()>deadline:raise TimeoutError('No ready job')
                        time.sleep(.01)
                    job=next(queue.glob('*/ready')).parent
                    self.assertTrue((job/'request.json').exists())
                    self.assertTrue((job/'project/main.gd').exists())
                    (job/'project/main.gd').write_text('extends Node2D\n# presentation change\n')
                    write_json(job/'response.json',{'status':'completed','answer':{'summary':'queued edit','tests':[]}})
                except Exception as exc:errors.append(exc)
            thread=threading.Thread(target=worker);thread.start()
            report=CodexEditor(queue=queue,timeout=5)(project,root/'candidate',out,diagnosis={},evaluation={},episodes=[],
                  history=[],adapter=SimpleNamespace(instructions='rules'),policy={'protected':['rules.json']})
            thread.join();self.assertEqual(errors,[])
            self.assertEqual(report['changed_files'],['main.gd'])

    def setup_project(self,root):
        p=root/'original';p.mkdir()
        (p/'project.godot').write_text('config_version=5\n')
        (p/'main.gd').write_text('extends Node2D\n')
        write_json(p/'rules.json',{'damage':10})
        (p/'assets').mkdir();(p/'assets/frame.png').write_bytes(b'original pixels')
        return p

    def edit(self,root,project,mutation):
        out=root/'trial';out.mkdir()
        def execute(job,**kw):
            mutation(job/'project')
            write_json(job/'response.json',{'status':'completed','answer':{'summary':'Repair','tests':[]}})
        with patch('gamecraft_training.package.codex_edit.execute_job',side_effect=execute):
            return CodexEditor()(project,root/'candidate',out,diagnosis={'problem':'layout'},evaluation={},
                   episodes=[],history=[],adapter=SimpleNamespace(instructions='Fixed rules'),policy={'protected':['rules.json']})

    def test_whole_code_edit_can_add_script_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root);before=digest(project)
            def change(p):
                (p/'main.gd').write_text('extends Node2D\nvar offset = 2\n')
                (p/'presentation.gd').write_text('extends RefCounted\n')
            report=self.edit(root,project,change)
            self.assertEqual(report['changed_files'],['main.gd','presentation.gd'])
            self.assertEqual(digest(project),before)
            self.assertEqual(digest(root/'trial/before'),before)
            comparison=json.loads((root/'trial/comparison.json').read_text())
            self.assertEqual(comparison['before_sha256'],before)
            self.assertEqual(comparison['candidate_sha256'],digest(root/'candidate'))
            self.assertNotEqual(comparison['before_sha256'],comparison['candidate_sha256'])
            self.assertIn('var offset',(root/'trial/candidate.diff').read_text())

    def test_worker_scratch_is_discarded_before_source_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root)
            def change(p):
                (p/'main.gd').write_text('extends Node2D\n# repaired\n')
                (p/'.runtime').mkdir();(p/'.runtime'/'godot.log').write_text('runtime output')
                (p/'tests').mkdir();(p/'tests'/'scratch.gd').write_text('extends SceneTree\n')
            report=self.edit(root,project,change)
            self.assertEqual(report['changed_files'],['main.gd'])
            self.assertFalse((root/'candidate/.runtime').exists())
            self.assertFalse((root/'candidate/tests').exists())

    def test_direct_editor_passes_godot_to_worker_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root);out=root/'trial';out.mkdir()
            def execute(job,**kwargs):
                self.assertEqual(kwargs['godot'],'/opt/godot')
                (job/'project/main.gd').write_text('extends Node2D\n# validated edit\n')
                write_json(job/'response.json',{'status':'completed',
                           'answer':{'summary':'validated edit','tests':[]}})
            with patch('gamecraft_training.package.codex_edit.execute_job',side_effect=execute) as call:
                CodexEditor(godot='/opt/godot')(
                    project,root/'candidate',out,diagnosis={},evaluation={},episodes=[],
                    history=[],adapter=SimpleNamespace(instructions='rules'),policy={'protected':[]})
            self.assertEqual(call.call_count,1)

    def test_assets_import_metadata_and_game_rules_can_be_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root);before=digest(project)
            def change(p):
                (p/'assets/frame.png').write_bytes(b'\x89PNG\x00new')
                (p/'assets/frame.png.import').write_text('[remap]\n')
                (p/'rules.json').write_text('{"damage":11}')
            report=self.edit(root,project,change)
            self.assertIn('assets/frame.png.import',report['changed_files'])
            self.assertEqual(digest(project),before)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root)
            with self.assertRaises(ValueError):
                self.edit(root,project,lambda p:(p/'escape.gd').symlink_to('/tmp'))

    def test_no_change_is_reported_without_inventing_patch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root)
            self.assertEqual(self.edit(root,project,lambda p:None)['changed_files'],[])

    def test_scene_only_edit_does_not_require_gd_change(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root)
            report=self.edit(root,project,lambda p:(p/'Main.tscn').write_text('[gd_scene format=3]\n'))
            self.assertEqual(report['changed_files'],['Main.tscn'])

    def test_explicit_policy_required_for_old_parameter_packages(self):
        with self.assertRaises(ValueError):policy({'fixed':['main.gd']})
        with self.assertRaises(ValueError):policy({}, {'protected':['../rules.json']})

    def test_cli_uses_isolated_workspace_and_no_api_key(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_json(root/'job.json',{'expires':10**12});write_json(root/'request.json',{})
            def start(command,**kw):
                self.assertEqual(command[command.index('--sandbox')+1],'workspace-write')
                self.assertEqual(command[command.index('--model')+1],DEFAULT_CODEX_MODEL)
                self.assertNotIn('OPENAI_API_KEY',kw['env'])
                self.assertNotIn('CODEX_API_KEY',kw['env'])
                self.assertEqual(kw['cwd'],root)
                def communicate(prompt,timeout):
                    self.assertIn('Edit project/',prompt)
                    write_json(root/'answer.json',{'summary':'done','tests':['not run']})
                return SimpleNamespace(communicate=communicate,returncode=0)
            with patch.dict(os.environ,{'OPENAI_API_KEY':'not-a-real-key','CODEX_API_KEY':'not-a-real-key'}),patch('subprocess.Popen',side_effect=start):
                self.assertTrue(execute_job(root))
                self.assertFalse(execute_job(root))
            self.assertEqual(json.loads((root/'response.json').read_text())['status'],'completed')

    def test_worker_retries_once_when_codex_model_is_at_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_json(root/'job.json',{'expires':10**12});write_json(root/'request.json',{})
            prompts=[]
            # Set the return code after communicate, as subprocess does.
            def process(command,**kwargs):
                p=SimpleNamespace(returncode=None)
                def communicate(prompt,timeout):
                    prompts.append(prompt)
                    if len(prompts)==1:
                        kwargs['stdout'].write(json.dumps({'type':'error',
                            'message':'Selected model is at capacity. Please try a different model.'})+'\n')
                        p.returncode=1
                    else:
                        write_json(root/'answer.json',{'summary':'continued edit','tests':[]})
                        p.returncode=0
                p.communicate=communicate
                return p
            with patch('subprocess.Popen',side_effect=process):
                self.assertTrue(execute_job(root,capacity_retry_delay=0))
            self.assertEqual(len(prompts),2)
            self.assertIn('model was at capacity',prompts[1])
            self.assertTrue((root/'codex-capacity-retry-1.jsonl').exists())
            self.assertEqual(json.loads((root/'response.json').read_text())['status'],'completed')

    def test_worker_does_not_retry_unrelated_codex_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_json(root/'job.json',{'expires':10**12});write_json(root/'request.json',{})
            starts=[]
            def process(command,**kwargs):
                starts.append(1)
                def communicate(prompt,timeout):
                    kwargs['stdout'].write(json.dumps({'type':'error','message':'Invalid project input'})+'\n')
                return SimpleNamespace(communicate=communicate,returncode=1)
            with patch('subprocess.Popen',side_effect=process):
                self.assertTrue(execute_job(root,capacity_retry_delay=0))
            self.assertEqual(len(starts),1)
            self.assertIn('Codex exited 1',json.loads((root/'response.json').read_text())['reason'])

    def test_godot_validation_detects_script_errors_even_with_zero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=self.setup_project(root);log=root/'import.log'
            def run(command,**kwargs):
                kwargs['stdout'].write('SCRIPT ERROR: Parse Error: cannot infer type\n')
                kwargs['stdout'].flush()
                return SimpleNamespace(returncode=0)
            with patch('subprocess.run',side_effect=run):
                ok,reason=validate_project(project,'godot',log)
            self.assertFalse(ok)
            self.assertIn('Parse Error',reason)

    def test_worker_retries_codex_after_failed_godot_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_json(root/'job.json',{'expires':10**12});write_json(root/'request.json',{})
            (root/'project').mkdir();(root/'project/project.godot').write_text('config_version=5\n')
            prompts=[]
            def start(command,**kwargs):
                def communicate(prompt,timeout):
                    prompts.append(prompt)
                    write_json(root/'answer.json',{'summary':f'attempt {len(prompts)}','tests':[]})
                return SimpleNamespace(communicate=communicate,returncode=0)
            with patch('subprocess.Popen',side_effect=start), \
                 patch('gamecraft_training.package.codex_edit.validate_project',side_effect=[(False,'Parse Error: bad type'),(True,'')]):
                self.assertTrue(execute_job(root,godot='/opt/godot',validation_retries=2))
            self.assertEqual(len(prompts),2)
            self.assertIn('failed mandatory Godot validation',prompts[1])
            response=json.loads((root/'response.json').read_text())
            self.assertEqual(response['status'],'completed')
            self.assertTrue(json.loads((root/'godot-validation-001.json').read_text())['passed'] is False)
            self.assertTrue(json.loads((root/'godot-validation-002.json').read_text())['passed'] is True)
