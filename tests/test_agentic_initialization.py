import tempfile,unittest,json,os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from game_initialization.agentic import __main__ as a
from game_initialization.batch.__main__ import read,write

class AgenticTests(unittest.TestCase):
 def test_shared_evolution_publishes_completed_package_without_absolute_gate(self):
  for state,errors in [('completed',[]),('completed_with_errors',[{'reason':'earlier repair failed'}])]:
   with self.subTest(state=state),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);package=root/'selected-package';package.mkdir()
    (package/'marker.txt').write_text('selected game')
    cfg={'codex':'unused','model':'unused','timeout':10,'godot':'unused',
         'asset_python':'unused','initialization_rounds':3}
    result={'state':state,'package':str(package),'errors':errors,'final_acceptance':'not_run'}
    with patch('gamecraft_training.package.loop.run',return_value=result), \
         patch('gamecraft_training.package.codex_edit.CodexEditor'), \
         patch.object(a,'service_environment',return_value=nullcontext()):
     self.assertEqual(a.evolve_bootstrap(root,cfg,{'package':'bootstrap'}),result)
    saved=read(root/'state.json')
    self.assertEqual(saved['stage'],'ready')
    self.assertEqual(saved['final_acceptance'],'not_run')
    self.assertEqual(saved['warnings'],len(errors))
    self.assertEqual((root/'initial-v0/marker.txt').read_text(),'selected game')

 def test_shared_evolution_rejects_missing_or_incomplete_package(self):
  for result in [{'state':'completed'}, {'state':'failed','package':'unused'}]:
   with self.subTest(result=result),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    cfg={'codex':'unused','model':'unused','timeout':10,'godot':'unused',
         'asset_python':'unused','initialization_rounds':3}
    with patch('gamecraft_training.package.loop.run',return_value=result), \
         patch('gamecraft_training.package.codex_edit.CodexEditor'), \
         patch.object(a,'service_environment',return_value=nullcontext()):
     with self.assertRaisesRegex(RuntimeError,'completed package'):
      a.evolve_bootstrap(root,cfg,{'package':'bootstrap'})
    self.assertEqual(read(root/'state.json')['stage'],'needs_attention')
    self.assertFalse((root/'initial-v0').exists())

 def test_bootstrap_smoke_uses_default_and_optional_core_scene(self):
  cases=[{'id':'menu','purpose':'initialization','launch':'default'},
         {'id':'drive','purpose':'initialization','bootstrap_smoke':True},
         {'id':'garage','purpose':'initialization'},
         {'id':'holdout','purpose':'test'}]
  self.assertEqual(a.bootstrap_smoke_ids(cases),['menu','drive'])
  cases[1].pop('bootstrap_smoke')
  self.assertEqual(a.bootstrap_smoke_ids(cases),['menu'])

 def test_shared_evolution_inherits_session_endpoints_and_restores_environment(self):
  cfg={'h3_url':'http://test-h3:3333','local_url':'http://test-vlm/v1','codex':'/test/codex'}
  with patch.dict(os.environ,{'H3_URL':'old','CODEX_BIN':'old-codex','GAMECRAFT_JUDGE_MODEL':'old-judge'}):
   with self.assertRaisesRegex(RuntimeError,'failed'):
    with a.service_environment(cfg):
     self.assertEqual(os.environ['H3_URL'],cfg['h3_url'])
     self.assertEqual(os.environ['LOCAL_URL'],cfg['local_url'])
     self.assertEqual(os.environ['CODEX_BIN'],cfg['codex'])
     os.environ['GAMECRAFT_JUDGE_MODEL']='new-judge'
     raise RuntimeError('failed')
   self.assertEqual(os.environ['H3_URL'],'old')
   self.assertEqual(os.environ['CODEX_BIN'],'old-codex')
   self.assertEqual(os.environ['GAMECRAFT_JUDGE_MODEL'],'old-judge')

 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  (self.root/'generation').mkdir();(self.root/'draft/game').mkdir(parents=True)
  self.cfg={'host':'test','max_requests':2,'asset_python':'python','h3_url':'http://localhost','timeout':10}
  self.cfg['task_id']='any-game'
  self.plan={'schema_version':1,'task_id':'any-game','style':'flat','assets':[]}
  write(self.root/'generation/asset_plan.json',self.plan);write(self.root/'generation/asset_manifest.json',{'assets':{}})
  self.item={'id':'environment','kind':'background','description':'environment only','requirements':'room with empty play area','prompt':'empty room','size':[32,32],'frames':1,'fps':1}
  self.request=self.root/'request.json';write(self.request,{'style':'flat','reason':'Need room for playable scene','assets':[self.item]})
 def test_generation_errors_return_without_aborting_code_agent(self):
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command',side_effect=RuntimeError('service down')):
   r=a.asset(self.root,self.request)
  self.assertEqual(r['status'],'error');self.assertIn('elapsed_seconds',r)
  self.assertEqual(len(read(self.root/'material_requests.json')),1)
 def test_request_budget_prevents_extra_generation(self):
  write(self.root/'material_requests.json',[{'asset_ids':['one','two']}])
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(RuntimeError,'budget'):a.asset(self.root,self.request)
   cmd.assert_not_called()
 def test_unresolved_requirement_ids_rejected(self):
  d=read(self.request);d['assets'][0]['requirements']=['R1'];write(self.request,d)
  with patch.object(a,'host',return_value=self.cfg):
   with self.assertRaisesRegex(ValueError,'prose'):a.asset(self.root,self.request)
 def test_shared_style_rejects_asset_production_instructions(self):
  d=read(self.request);d['style']='Painterly boxing art, isolated props on pure green';write(self.request,d)
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(ValueError,'asset-production instruction'):a.asset(self.root,self.request)
   cmd.assert_not_called()
 def test_successful_request_installs_real_material_without_judge(self):
  def fake(*args,**kwargs):
   import hashlib
   p=self.root/'generation/frame.png';Image.new('RGB',(32,32)).save(p)
   planned={**self.item,'style':'flat'}
   write(self.root/'generation/asset_manifest.json',{'assets':{'environment':{'files':['frame.png'],
    'plan_sha256':hashlib.sha256(json.dumps(planned,sort_keys=True).encode()).hexdigest()}}})
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command',side_effect=fake):r=a.asset(self.root,self.request)
  self.assertEqual(r['status'],'generated')
  self.assertTrue((self.root/'draft/game/assets/environment/frame0000.png').is_file())
 def test_failed_check_cannot_publish(self):
  write(self.root/'latest_check.json',{'passed':False})
  with self.assertRaises(ValueError):a.publish(self.root)
  self.assertFalse((self.root/'initial-v0').exists())

 def test_benchmark_preflight_requires_demo_traces(self):
  game=self.root/'draft/game'
  with self.assertRaisesRegex(ValueError,'demo traces'):
   a.benchmark_preflight(game,'openworld-bounty',self.root/'benchmark-check','godot')

 def test_benchmark_replay_failure_prevents_bootstrap_ready(self):
  (self.root/'prompt.txt').write_text('Build complete game.')
  cfg={'task_id':'any-game','bootstrap_repairs':0,'bootstrap_only':True,
       'godot':'godot','max_requests':64,'codex':'codex','model':'gpt-6-sol','timeout':10}
  with patch.object(a,'agentic_preflight'), \
       patch.object(a,'prepare_requirements',return_value={}), \
       patch.object(a,'codex'), patch.object(a,'assert_scope'), \
       patch.object(a,'check',return_value={'passed':True,'package':'checks/one/package'}), \
       patch.object(a,'benchmark_preflight',side_effect=RuntimeError('benchmark replay failed')):
   with self.assertRaisesRegex(RuntimeError,'benchmark replay failed'):
    a.run_profile(self.root,cfg)
  self.assertEqual(read(self.root/'state.json')['stage'],'needs_attention')
 def test_host_guard(self):
  write(self.root/'session.json',{'host':'training'})
  with patch.object(a.socket,'gethostname',return_value='developer'):
   with self.assertRaisesRegex(RuntimeError,'training host'):a.host(self.root)

 def test_shared_profile_stops_after_single_passing_bootstrap_build(self):
  (self.root/'prompt.txt').write_text('Build complete game.')
  cfg={'task_id':'any-game','bootstrap_repairs':1,'rounds':3,'godot':'godot','max_requests':64,
       'codex':'codex','model':'gpt-5.5','timeout':10}
  report={'passed':True,'package':'checks/one/package'}
  with patch.object(a,'agentic_preflight'), \
       patch.object(a,'prepare_requirements',return_value={}), \
       patch.object(a,'codex') as build, \
       patch.object(a,'assert_scope'), \
       patch.object(a,'check',return_value=report), \
       patch.object(a,'benchmark_preflight'), \
       patch.object(a,'evolve_bootstrap',return_value={'done':True}) as evolve:
   self.assertEqual(a.run_profile(self.root,cfg),{'done':True})
  self.assertEqual(build.call_count,1)
  self.assertIn('single construction pass',build.call_args.args[3])
  evolve.assert_called_once_with(self.root,cfg,report)

 def test_bootstrap_only_preserves_checked_package_without_evolution(self):
  (self.root/'prompt.txt').write_text('Build complete game.')
  cfg={'task_id':'any-game','bootstrap_repairs':0,'bootstrap_only':True,'godot':'godot','max_requests':64,
       'codex':'codex','model':'gpt-6-sol','timeout':10}
  report={'passed':True,'package':'checks/one/package'}
  with patch.object(a,'agentic_preflight'), \
       patch.object(a,'prepare_requirements',return_value={}), \
       patch.object(a,'codex') as build, \
       patch.object(a,'assert_scope'), \
       patch.object(a,'check',return_value=report), \
       patch.object(a,'benchmark_preflight'), \
       patch.object(a,'evolve_bootstrap') as evolve:
   result=a.run_profile(self.root,cfg)
  self.assertEqual(result,{'bootstrap_package':'checks/one/package','state':'bootstrap_ready'})
  self.assertEqual(build.call_count,1)
  self.assertEqual(read(self.root/'state.json')['stage'],'bootstrap_ready')
  evolve.assert_not_called()

 def test_shared_profile_uses_only_scoped_technical_repair_budget(self):
  (self.root/'prompt.txt').write_text('Build complete game.')
  cfg={'task_id':'any-game','bootstrap_repairs':1,'rounds':3,'godot':'godot','max_requests':64,
       'codex':'codex','model':'gpt-5.5','timeout':10}
  failed={'passed':False,'error':'project does not import'}
  passed={'passed':True,'package':'checks/two/package'}
  prompts=[]
  def capture(*args):prompts.append(args[3])
  with patch.object(a,'agentic_preflight'), \
       patch.object(a,'prepare_requirements',return_value={}), \
       patch.object(a,'codex',side_effect=capture), \
       patch.object(a,'assert_scope'), \
       patch.object(a,'check',side_effect=[failed,passed]), \
       patch.object(a,'benchmark_preflight'), \
       patch.object(a,'evolve_bootstrap',return_value={'done':True}):
   a.run_profile(self.root,cfg)
  self.assertEqual(len(prompts),2)
  self.assertIn('TECHNICAL REPAIR ONLY',prompts[1])
  self.assertEqual(read(self.root/'attempts/002/result.json')['phase'],'bootstrap_repair')

 def test_shared_profile_repairs_acceptance_coverage_inside_bootstrap_attempt(self):
  (self.root/'prompt.txt').write_text('Build complete game.')
  cfg={'task_id':'any-game','bootstrap_repairs':0,'rounds':1,'godot':'godot','max_requests':64,
       'codex':'codex','model':'gpt-5.5','timeout':10}
  missing={'passed':False,'error':'Acceptance plan needs default-launch coverage: R010'}
  passed={'passed':True,'package':'checks/two/package'}
  prompts=[]
  def capture(*args):prompts.append(args[3])
  with patch.object(a,'agentic_preflight'), \
       patch.object(a,'prepare_requirements',return_value={}), \
       patch.object(a,'codex',side_effect=capture), \
       patch.object(a,'assert_scope'), \
       patch.object(a,'check',side_effect=[missing,passed]), \
       patch.object(a,'benchmark_preflight'), \
       patch.object(a,'evolve_bootstrap',return_value={'done':True}):
   self.assertEqual(a.run_profile(self.root,cfg),{'done':True})
  self.assertEqual(len(prompts),2)
  self.assertIn('REPAIR ONLY THE INITIALIZATION ACCEPTANCE COVERAGE',prompts[1])
  result=read(self.root/'attempts/001/coverage-repairs/001/result.json')
  self.assertTrue(result['passed'])
  self.assertEqual(read(self.root/'attempts/001/result.json')['phase'],'bootstrap_build')

 def test_reference_prompt_object_is_rejected_before_h3(self):
  req=read(self.request);req['assets'][0].update(kind='animation',loop=True,reference_prompt={'desc':'single boxer'})
  write(self.request,req)
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(ValueError,'STRING'):a.asset(self.root,self.request)
   cmd.assert_not_called()
  self.assertFalse((self.root/'material_requests.json').exists())
 def test_bootstrap_rejects_unaccepted_reference_before_h3(self):
  req=read(self.request)
  req['assets'][0].update(generation_scope='scene',motion='loop',frames=4,fps=1,
      scope_reason='The room ambience needs intrinsic motion.',game_controlled=['UI'],
      reference_asset_id='missing_room')
  write(self.request,req)
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(ValueError,'previously accepted'):a.asset(self.root,self.request)
   cmd.assert_not_called()
  self.assertFalse((self.root/'material_requests.json').exists())
 def test_new_bootstrap_requires_explicit_material_scope(self):
  cfg={**self.cfg,'material_scope_planning':True}
  with patch.object(a,'host',return_value=cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(ValueError,'generation_scope and motion'):
    a.asset(self.root,self.request)
   cmd.assert_not_called()
  self.assertFalse((self.root/'material_requests.json').exists())
 def test_animation_cannot_be_downgraded_after_error(self):
  self.plan['assets']=[dict(self.item,kind='animation')];write(self.root/'generation/asset_plan.json',self.plan)
  with patch.object(a,'host',return_value=self.cfg),patch.object(a,'command') as cmd:
   with self.assertRaisesRegex(ValueError,'downgrade'):a.asset(self.root,self.request)
   cmd.assert_not_called()
