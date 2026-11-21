import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from gamecraft_behavior.objective import decision, previous_context
from gamecraft_behavior.program_policy import program_player
from gamecraft_behavior.frame_replay import validate_trace
from gamecraft_training.package.objective import create, plan, validate

sys.path.insert(0, str(Path(__file__).parent/'fixtures'))
from counter_adapter import CounterAdapter

GOAL = {'goal': 'Complete both stages', 'success_condition': 'Reach 14 points',
        'failure_condition': 'Definitive loss', 'continuation_rule': 'Continue after the first stage'}
ENTRY = {'entry_mode': 'default', 'entry_id': 'counter',
         'selection_reason': 'The normal entry reaches the core mechanic quickly.'}
POLICY = '''def act(state, recent):
    points = state.get('current_points', 0)
    if points >= 14:
        return {'action': None, 'reason': 'Both stages complete', 'done': True, 'outcome': 'success'}
    if points == 4:
        return {'action': 'block', 'reason': 'Continue to next stage', 'done': False, 'outcome': 'ongoing'}
    return {'action': 'jab', 'reason': 'Play stage', 'done': False, 'outcome': 'ongoing'}
'''


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.llm_patch=patch.dict(sys.modules,{'llm':SimpleNamespace(call=Mock(),img=Mock())})
        self.llm_patch.start();self.addCleanup(self.llm_patch.stop)


class ObjectiveTests(OfflineTest):
    def test_planner_receives_requirements_and_full_project_access(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'game';project.mkdir()
            (project/'rules.gd').write_text('rules')
            (root/'requirements.json').write_text(json.dumps({'requirements':[{'description':'two stages'}]}))
            (root/'instruction.md').write_text('Original game brief')
            with patch('llm.call',return_value={**GOAL, **ENTRY}) as invoke:
                goal,entry=create(CounterAdapter(),project,root,root,model='test',backend='codex',
                                  frame_budget=30)
            self.assertEqual(goal,GOAL)
            self.assertEqual(entry,ENTRY)
            self.assertEqual(invoke.call_count,1)
            args,kwargs=invoke.call_args
            data=json.loads(args[2][0]['text'])
            self.assertEqual(data['briefs']['instruction.md'],'Original game brief')
            self.assertEqual(data['frame_budget'],30)
            self.assertEqual(data['available_entries'][0]['entry_mode'],'default')
            self.assertEqual(kwargs['read_only_project'],str(project))
            saved=json.loads((root/'rsi_objective.json').read_text())
            self.assertEqual(saved['scope'],'all_rounds')
            self.assertEqual(saved['entry'],ENTRY)

    def test_planner_can_select_core_scenario_but_not_held_out_scenarios(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'game';project.mkdir()
            (project/'rules.gd').write_text('rules')
            (root/'requirements.json').write_text(json.dumps({'requirements':[]}))
            cases=[{'id':'fight','purpose':'initialization','launch':'scenario'},
                   {'id':'training','purpose':'train','launch':'scenario'},
                   {'id':'held_out','purpose':'test','launch':'scenario'}]
            selected={**GOAL,'entry_mode':'scenario','entry_id':'fight',
                      'selection_reason':'This fixture reaches core play within budget.'}
            with patch('llm.call',return_value=selected) as invoke:
                goal,entry=create(CounterAdapter(),project,root,root,model='test',backend='codex',
                                  cases=cases,frame_budget=90)
            self.assertEqual(goal,GOAL)
            self.assertEqual(entry['entry_id'],'fight')
            candidates=json.loads(invoke.call_args.args[2][0]['text'])['available_entries']
            self.assertEqual({x['entry_id'] for x in candidates},{'counter','fight','training'})
            with self.assertRaisesRegex(ValueError,'unavailable'):
                validate({**selected,'entry_id':'held_out'},candidates)

    def test_player_receives_skill_goal_previous_public_evidence_and_preserves_done(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'main.gd').write_text('rules')
            (root/'game_plan.json').write_text(json.dumps({'task_id':'test-counter'}))
            old={'scenario':'counter','completion':{'outcome':'truncated'},
                 'decisions':[{'frame':0,'action':'wait','reason':'observe',
                               'player_state':{'current_points':0},'evaluation_state':{'secret':42}}],
                 'replay':{'secret':42}}
            prior=previous_context([old])
            self.assertNotIn('secret',json.dumps(prior))
            with patch('llm.call',return_value={'source':POLICY}) as call, \
                 patch('llm.img',return_value={'type':'input_image'}):
                player=program_player(root,root/'policy.py',adapter=CounterAdapter(),
                    backend='codex',model='test',run_objective=GOAL,previous_playtest=prior)
                first=player('frame','new skill',[],6,current_state={'current_points':4})
                done=player('frame','new skill',[],6,current_state={'current_points':14})
            self.assertEqual(call.call_count,1)
            context=json.loads(call.call_args.args[2][0]['text'])
            self.assertEqual(context['behavior_skill'],'new skill')
            self.assertEqual(context['run_objective'],GOAL)
            self.assertEqual(context['previous_playtest'],prior)
            self.assertFalse(first['done'])
            self.assertTrue(done['done'])
            self.assertIsNone(done['action'])

    def test_player_retries_policy_that_reads_private_engine_field(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'main.gd').write_text('rules')
            (root/'game_plan.json').write_text(json.dumps({'task_id':'test-counter'}))
            private = "def act(state, recent):\n    return {'action': 'jab' if state.get('secret_phase', '') else 'block', 'reason': 'read', 'done': False, 'outcome': 'ongoing'}"
            with patch('llm.call',side_effect=[{'source':private},{'source':POLICY}]) as call, \
                 patch('llm.img',return_value={'type':'input_image'}):
                player=program_player(root,root/'policy.py',adapter=CounterAdapter(),
                                      backend='codex',model='test',run_objective=GOAL)
                result=player('frame','',[],6,current_state={'current_points':4})
            self.assertEqual(call.call_count,2)
            self.assertEqual(result['action'],'block')
            self.assertIn('secret_phase',json.loads(call.call_args.args[2][0]['text'])['previous_error'])
            self.assertNotIn('secret_phase',(root/'policy.py').read_text())

    def test_reject_inconsistent_stop_signals(self):
        adapter=CounterAdapter()
        for value in [
            {'action':'jab','reason':'done','done':True,'outcome':'success'},
            {'action':None,'reason':'done','done':'true','outcome':'success'},
            {'action':None,'reason':'done','done':True,'outcome':'ongoing'},
            {'action':'wait','reason':'wait','done':False,'outcome':'failure'},
        ]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                decision(value,adapter,GOAL)
        validate_trace({'duration_frames':0,'events':[],'stop_on_terminal':False},adapter)
        with self.assertRaises(ValueError):
            validate_trace({'duration_frames':0,'events':[]},adapter)

    def test_candidate_keeps_objective_without_behavior_skill(self):
        from gamecraft_training.package.playtests import replay_candidate
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'game';project.mkdir()
            (project/'rules.gd').write_text('rules')
            from gamecraft_behavior.core import digest
            def factory(project,path,**kwargs):
                self.assertEqual(kwargs['run_objective'],GOAL)
                self.assertEqual(kwargs['playtest_context']['run_objective'],GOAL)
                path.write_text(POLICY)
                return lambda *a,**kw: None
            def rollout(project,path,skill,decide,**kwargs):
                self.assertEqual(skill,'')
                path.mkdir()
                (path/'trace.json').write_text('{}');(path/'video.mp4').write_bytes(b'video')
                return {'scenario':'counter','normal_launch':True,'decisions':[{}],
                        'run_objective':GOAL,'replay_verified':True,'project_sha256':digest(project),
                        'trace':str(path/'trace.json'),'video':str(path/'video.mp4')}
            results=replay_candidate(project,[{'scenario':'counter','normal_launch':True,'run_objective':GOAL}],
                root/'replay',adapter=CounterAdapter(),diagnosis={},changes={},rollout=rollout,
                godot='unused',model='test',backend='codex',step_frames=6,max_decisions=5,
                player_state='engine',policy_factory=factory)
            self.assertEqual(results[0]['run_objective'],GOAL)


@unittest.skipUnless(os.environ.get('GAMECRAFT_TEST_GODOT'), 'Set GAMECRAFT_TEST_GODOT for engine tests')
class ObjectiveEngineTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'game'
        shutil.copytree(Path(__file__).parent/'fixtures/counter_game',self.project)
        source=self.project/'main.gd'
        source.write_text(source.read_text().replace(
            '    if finished or not event is InputEventKey',
            '    if event is InputEventKey and event.pressed and event.keycode == KEY_L and points == 4:\n'
            '        points = 10\n        finished = false\n        return\n'
            '    if finished or not event is InputEventKey').replace(
                'finished = points >= 3','finished = points == 4 or points >= 14'))
        self.adapter=CounterAdapter()
        self.adapter.viewport=(320,240)
        config=self.project/'project.godot'
        config.write_text(config.read_text().replace('1280','320').replace('720','240'))

    def play(self,source,name='run',budget=5,objective=GOAL):
        from gamecraft_behavior.runtime import rollout
        with patch('llm.call',return_value={'source':source}),patch('llm.img',return_value={'type':'input_image'}):
            decide=program_player(self.project,self.root/(name+'.py'),adapter=self.adapter,run_objective=objective)
            return rollout(self.project,self.root/name,'',decide,godot=os.environ['GAMECRAFT_TEST_GODOT'],
                adapter=self.adapter,normal_launch=True,step_frames=6,max_decisions=budget,player_state='engine')

    def test_intermediate_terminal_continues_and_recording_matches_last_frame_success(self):
        result=self.play(POLICY)
        self.assertEqual([d['action'] for d in result['decisions']],['jab','jab','block','jab','jab'])
        self.assertEqual(result['completion']['outcome'],'success')
        self.assertEqual(result['frames'],30)
        self.assertEqual(result['replay']['points'],14)
        self.assertEqual(result['replay']['physics_ticks'],60)
        self.assertTrue(result['replay_verified'])
        self.assertTrue(Path(result['video']).is_file())
        legacy=self.play("def act(state, recent):\n    return {'action':'jab','reason':'play'}",name='legacy',objective=None)
        self.assertEqual(len(legacy['decisions']),2)
        self.assertEqual(legacy['replay']['points'],4)
        self.assertTrue(legacy['replay_verified'])

    def test_failure_stops_without_executing_further_action(self):
        policy=POLICY.replace('points >= 14','points >= 2').replace("'success'","'failure'")
        result=self.play(policy)
        self.assertEqual(result['completion']['outcome'],'failure')
        self.assertEqual(len(result['decisions']),1)
        self.assertTrue(result['replay_verified'])

    def test_budget_is_truncation(self):
        result=self.play(POLICY,budget=3)
        self.assertEqual(result['completion']['outcome'],'truncated')
        self.assertEqual(result['frames'],18)
        self.assertEqual(result['replay']['points'],10)
        self.assertTrue(result['replay_verified'])

    def test_already_done_does_not_invent_an_action_or_crash_replay(self):
        result=self.play("def act(state, recent):\n    return {'action':None,'reason':'already achieved','done':True,'outcome':'success'}")
        self.assertEqual(result['frames'],0)
        self.assertEqual(result['decisions'],[])
        self.assertEqual(result['replay']['physics_ticks'],0)
        self.assertTrue(result['replay_verified'])

    def test_normal_launch_and_replay_use_separate_fresh_saves(self):
        source=self.project/'main.gd'
        source.write_text(source.read_text() + '\nfunc _ready() -> void:\n'
            '    if FileAccess.file_exists("user://previous_run.txt"):\n'
            '        points = 100\n'
            '    var file = FileAccess.open("user://previous_run.txt", FileAccess.WRITE)\n'
            '    file.store_string("played")\n'
            '    file.close()\n')
        first=self.play(POLICY,name='first')
        second=self.play(POLICY,name='second')
        self.assertEqual(first['frames'],30)
        self.assertEqual(second['frames'],30)
        self.assertTrue(first['replay_verified'])
        self.assertTrue(second['replay_verified'])
