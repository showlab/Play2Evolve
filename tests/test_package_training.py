import json
import base64
import io
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from game_initialization.package import freeze,inspect_package
from gamecraft_training.package.config import load,better,aggregate
from gamecraft_training.package import models
from gamecraft_training.package.edits import apply_parameters, Generator, ASSET_REPAIR_CANDIDATES
from gamecraft_training.package.evidence import sample, content, outcome_content
from gamecraft_training.package.loop import (run,verify_behavior_non_regression,accepts_skill_execution,
                                             _batch_diagnosis,_asset_problem,
                                             _verify_repairs,_combine_parallel_edit)
from gamecraft_training.package.repair_verifier import state,attempted_outcome
from gamecraft_training.package.selection import promotion,label_review,needs_confirmation
from gamecraft_composition.core import accept_targeted
from gamecraft_behavior.core import write_json, digest, trace


class PackageTrainingTests(unittest.TestCase):
    def test_router_history_window_can_omit_or_limit_prior_rounds(self):
        history=[{'round':number} for number in range(1,5)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'instruction.md').write_text('Play the scene.')
            write_json(root/'requirements.json',{'requirements':[]})
            answer={'coverage_passed':False,'coverage_reason':'none',
                    'missing_evidence':[],'issues':[]}
            for window,expected in ((0,[]),(2,history[-2:]),(8,history)):
                config={'_profile':{'name':'initialization'},'_package_root':str(root),
                        '_history_windows':{'judge':window},'quality':{'criteria':[]}}
                with self.subTest(window=window), \
                     patch.object(models,'content',return_value=[]), \
                     patch.object(models,'outcome_content',return_value=[]), \
                     patch.object(models,'material_content',return_value=[]), \
                     patch.object(models,'judge_call',return_value=answer) as judge:
                    models.route(config,SimpleNamespace(instructions='controls'),root,
                                 {'assets':[]},{'parameters':{}},{},[],history)
                self.assertEqual(judge.call_args.args[1]['previous_attempts'],expected)

    def test_initialization_router_keeps_requirements_without_gate_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'instruction.md').write_text('Train, win a bout, and advance in rank.')
            requirements=[{'id':'R1','description':'Train'},{'id':'R2','description':'Win'}]
            write_json(root/'requirements.json',{'requirements':requirements})
            for scope,expected in [(None,requirements),
                                   ({'id':'training','requirements':requirements[:1]},requirements[:1]),
                                   ({'id':'unmapped','requirements':[]},[])]:
                config={'_profile':{'name':'initialization'},'_package_root':str(root),
                        'quality':{'criteria':[]}}
                if scope is not None:config['_initialization_scene']=scope
                answer={'coverage_passed':False,'coverage_reason':'not shown',
                        'missing_evidence':['Bout was not reached'],'issues':[]}
                with self.subTest(scope=scope), \
                     patch.object(models,'content',return_value=[]), \
                     patch.object(models,'outcome_content',return_value=[]), \
                     patch.object(models,'material_content',return_value=[]), \
                     patch.object(models,'judge_call',return_value=answer) as judge:
                    result=models.route(config,SimpleNamespace(instructions='controls'),root,
                                        {'assets':[]},{'parameters':{}},{},[],[])
                judge.assert_called_once()
                self.assertEqual(judge.call_args.args[3],'package_repair_route')
                context=judge.call_args.args[1]['public_requirements']
                self.assertEqual(context['requirements'],expected)
                self.assertEqual(context['original_brief'],'Train, win a bout, and advance in rank.')
                self.assertEqual(result['missing_evidence'],['Bout was not reached'])

    def test_pairwise_evidence_binds_both_versions_in_one_labeled_image(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.png';b=Path(td)/'b.png'
            Image.new('RGB',(32,24),'red').save(a)
            Image.new('RGB',(32,24),'blue').save(b)
            def result(path):
                return [{'scenario':'fight','decisions':[{'frame':0,'action':'jab','image':str(path)}]}]
            rows=models._paired_repair_content(result(a),result(b))
            self.assertEqual([row['type'] for row in rows],['input_text','input_image'])
            self.assertIn('VERSION A on the left and VERSION B on the right',rows[0]['text'])
            image_data=base64.b64decode(rows[1]['image_url'].split(',',1)[1])
            with Image.open(io.BytesIO(image_data)) as paired:
                self.assertEqual(paired.format,'JPEG')
                self.assertEqual(paired.size,(64,60))
                self.assertGreater(paired.getpixel((10,48))[0],200)
                self.assertGreater(paired.getpixel((50,48))[2],200)

    def test_world_judge_content_includes_decision_screenshot(self):
        results=[{'scenario':'fight','decisions':[{'frame':0,'action':'jab','reason':'contact',
                                                   'image':'frame_0000.png'}]}]
        fake=SimpleNamespace(img=lambda path,**kwargs:{'type':'input_image','path':path})
        with patch.dict(sys.modules,{'llm':fake}):
            evidence=content(results)
        self.assertEqual(evidence[1]['path'],'frame_0000.png')

    def test_world_judge_receives_post_action_result_frame_and_verified_state(self):
        with tempfile.TemporaryDirectory() as td:
            control=Path(td);(control/'frame_0002.png').write_bytes(b'png')
            result={'scenario':'ranked_fight','decision_frames':14,'replay_verified':True,
                    'replay':{'state':'result','outcome':'WIN','rank':1},
                    'decisions':[{'frame':0,'image':str(control/'frame_0000.png')},
                                 {'frame':14,'image':str(control/'frame_0001.png')}]}
            with patch('llm.img',side_effect=lambda path,**kwargs:{'type':'input_image','path':path}):
                evidence=outcome_content([result])
            summary=json.loads(evidence[0]['text'])
            self.assertEqual(summary['frame'],28)
            self.assertTrue(summary['replay_verified'])
            self.assertEqual(summary['final_state']['rank'],1)
            self.assertEqual(evidence[1]['path'],str(control/'frame_0002.png'))

    def test_router_keeps_observed_issues_with_partial_scene_coverage(self):
        def issue(ident,frame):
            return {'id':ident,'problem':ident,'route':'composition','asset_ids':[],
                    'source_observation':{'status':'clean','evidence':'source is sound'},
                    'rendered_observation':{'status':'defective','evidence':'visible defect'},
                    'evidence':[{'scenario':'fight','frame':frame,'observation':'visible defect'}],
                    'rendered_acceptance':['defect no longer visible']}
        raw={'coverage_passed':False,'coverage_reason':'later rounds were not played',
             'missing_evidence':['round break'],
             'issues':[issue('shown',14),issue('unshown',99)]}
        results=[{'scenario':'fight','decision_frames':14,'decisions':[
            {'frame':0,'action':'wait','reason':'start','image':'frame_0000.png'},
            {'frame':14,'action':'jab','reason':'contact','image':'frame_0001.png'}]}]
        config={'_profile':{'name':'initialization'},'quality':{'criteria':[]},
                '_initialization_scene':{'id':'fight','max_frames':4}}
        with patch.object(models,'content',return_value=[]), \
             patch.object(models,'outcome_content',return_value=[]), \
             patch.object(models,'material_content',return_value=[]), \
             patch.object(models,'judge_call',return_value=raw):
            answer=models.route(config,SimpleNamespace(instructions='rules'),Path('.'),
                                {'assets':[]},{'parameters':{}},{},results,[])
        self.assertTrue(answer['coverage_passed'])
        self.assertEqual([item['id'] for item in answer['issues']],['shown'])
        self.assertEqual(answer['missing_evidence'],['round break'])
        self.assertIn('Partial scene coverage',answer['coverage_reason'])

    def test_rsi_judge_and_router_consider_playtest_dimensions_without_category_priority(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            project=root/'game';(project/'scripts').mkdir(parents=True)
            (project/'scripts/rules.gd').write_text('const GAP = 240\n')
            package=root/'package';package.mkdir()
            write_json(package/'requirements.json',{'requirements':[
                {'id':'R1','description':'Difficulty progresses during play'}]})
            config={'_profile':{'name':'rsi'},'_package_root':str(package),
                    'quality':{'criteria':[{'id':'art','description':'Visual art'}]}}
            result={'scenario':'train','decision_frames':6,'replay':{'score':2},
                    'decisions':[{'frame':0,'action':'tap','reason':'start','image':'frame.png',
                                  'evaluation_state':{'score':0}}]}
            judged={'art':.5,'reason':'partial art','findings':{'art':'visible'},
                    'playtest_aspects':{
                        'rules_controls':'rules observed',
                        'challenge_strategy':'same strategy repeated',
                        'content_progression':'later content unverified',
                        'feedback_readability':'feedback observed',
                        'visual_presentation':'art visible',
                        'aesthetic_richness':'same background repeated',
                        'gameplay_richness':'same decision repeated'},
                    'playtest_findings':[{'kind':'progression_design','problem':'same challenge',
                                          'evidence':'two repeated hazards'}],
                    'playtest_limitations':['later stage not visited']}
            with patch.object(models,'content',return_value=[]), \
                 patch.object(models,'outcome_content',return_value=[]), \
                 patch.object(models,'judge_call',return_value=judged) as invoke:
                evaluation=models.judge(config,SimpleNamespace(instructions='rules'),project,[result])
            self.assertEqual(evaluation['playtest_findings'][0]['kind'],'progression_design')
            self.assertEqual(evaluation['playtest_aspects']['challenge_strategy'],'same strategy repeated')
            self.assertEqual(evaluation['playtest_aspects']['aesthetic_richness'],'same background repeated')
            self.assertEqual(evaluation['playtest_aspects']['gameplay_richness'],'same decision repeated')
            prompt=invoke.call_args.args[0]
            self.assertIn('observed game experience',prompt)
            self.assertIn('five equally considered aspects',prompt)
            self.assertIn('skilled play becomes trivial or repetitive',prompt)
            self.assertIn('gameplay_richness',prompt)
            self.assertIn('aesthetic_richness',prompt)
            context=invoke.call_args.args[1]['playtest_context']
            self.assertEqual(context['declared_requirements'][0]['id'],'R1')
            self.assertIn('const GAP',context['game_code'][0]['text'])
            def issue(ident,kind):
                return {'id':ident,'kind':kind,'problem':ident,'route':'composition','asset_ids':[],
                        'source_observation':{'status':'clean','evidence':'source inspected'},
                        'rendered_observation':{'status':'defective','evidence':'observed'},
                        'evidence':[{'scenario':'train','frame':0,'observation':'observed'}],
                        'rendered_acceptance':['observable improvement']}
            routed={'coverage_passed':True,'coverage_reason':'shown','missing_evidence':[],
                    'issues':[issue('polish','visual'),issue('repetition','content_depth')]}
            with patch.object(models,'content',return_value=[]), \
                 patch.object(models,'outcome_content',return_value=[]), \
                 patch.object(models,'material_content',return_value=[]), \
                 patch.object(models,'judge_call',return_value=routed) as invoke:
                answer=models.route(config,SimpleNamespace(instructions='rules'),project,
                                    {'assets':[]},{'parameters':{}},evaluation,[result],[])
            self.assertEqual([row['id'] for row in answer['issues']],['polish','repetition'])
            self.assertNotIn('deferred_visual_issues',answer)
            self.assertIn('without a fixed category priority',invoke.call_args.args[0])
            self.assertIn('a clean existing asset does not prove',invoke.call_args.args[0])
            self.assertIn('new authored content and gameplay integration',invoke.call_args.args[0])
            self.assertIn('playtest_context',invoke.call_args.args[1])

    def test_initialization_judge_does_not_require_rsi_playtest_fields(self):
        config={'_profile':{'name':'initialization'},
                'quality':{'criteria':[{'id':'art','description':'Visual art'}]}}
        judged={'art':1.0,'reason':'complete','findings':{'art':'complete'}}
        with patch.object(models,'content',return_value=[]), \
             patch.object(models,'outcome_content',return_value=[]), \
             patch.object(models,'judge_call',return_value=judged) as invoke:
            result=models.judge(config,SimpleNamespace(instructions='rules'),Path('.'),[])
        self.assertNotIn('playtest_findings',result)
        self.assertNotIn('playtest_context',invoke.call_args.args[1])

    def test_router_leaves_unobserved_scene_unrepaired(self):
        raw={'coverage_passed':False,'coverage_reason':'no action shown',
             'missing_evidence':['attack'],'issues':[]}
        with patch.object(models,'content',return_value=[]), \
             patch.object(models,'outcome_content',return_value=[]), \
             patch.object(models,'material_content',return_value=[]), \
             patch.object(models,'judge_call',return_value=raw):
            answer=models.route({'quality':{'criteria':[]}},SimpleNamespace(instructions='rules'),
                                Path('.'),{'assets':[]},{'parameters':{}},{},
                                [{'scenario':'fight','decisions':[]}],[])
        self.assertFalse(answer['coverage_passed'])
        self.assertEqual(answer['issues'],[])

    def test_prompt_agent_can_replace_wrong_original_instead_of_appending(self):
        from gamecraft_assets.prompt_repair import rewrite_prompt
        asset={'id':'fighter','kind':'animation','description':'fighter against a black background',
               'requirements':'visible boxer','prompt':'fighter standing still on black'}
        replacement='A boxer steps forward and throws one clear jab.'
        with patch('llm.call',return_value={'repairable':True,'prompt':replacement,
                                           'reason':'original motion and background were wrong'}) as invoke:
            result=rewrite_prompt(asset,style='arcade illustration',problem='fighter never approaches',
                                  task_brief='The player must box against an opponent.')
        self.assertEqual(result['prompt'],replacement)
        self.assertNotIn('standing still',result['prompt'])
        self.assertEqual(invoke.call_args.kwargs['backend'],'codex')
        context=json.loads(invoke.call_args.args[2][0]['text'])
        self.assertEqual(context['asset']['prompt'],asset['prompt'])
        self.assertIn('The player must box',context['task_brief'])

    def test_parallel_code_merge_keeps_asset_and_rejects_conflicting_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);baseline=root/'baseline';edited=root/'edited';current=root/'current'
            for directory in (baseline,edited,current):directory.mkdir()
            for directory in (baseline,edited,current):
                (directory/'main.gd').write_text('old code')
                (directory/'asset.png').write_bytes(b'old asset')
            (edited/'main.gd').write_text('new code')
            (current/'asset.png').write_bytes(b'new asset')
            merged=root/'merged'
            self.assertEqual(_combine_parallel_edit(baseline,edited,current,merged),['main.gd'])
            self.assertEqual((merged/'main.gd').read_text(),'new code')
            self.assertEqual((merged/'asset.png').read_bytes(),b'new asset')
            (current/'main.gd').write_text('other code')
            with self.assertRaisesRegex(ValueError,'conflicts'):
                _combine_parallel_edit(baseline,edited,current,root/'conflict')

    def test_repair_batches_every_routed_issue_without_primary_secondary_ranking(self):
        def issue(ident,route,assets):
            return {'id':ident,'problem':f'problem {ident}','route':route,'asset_ids':assets,
                    'source_observation':{'status':'defective' if assets else 'clean','evidence':f'source {ident}'},
                    'rendered_observation':{'status':'defective','evidence':f'render {ident}'},
                    'evidence':[{'scenario':'check','frame':0,'observation':ident}],
                    'rendered_acceptance':[f'accept {ident}']}
        rows=[issue('I1','both',['fighter']),issue('I2','asset',['arena']),
              issue('I3','composition',[])]
        asset=_batch_diagnosis([rows[1],rows[0]],'asset')
        composition=_batch_diagnosis([rows[2],rows[0]],'composition')
        self.assertEqual([x['id'] for x in asset['issues']],['I2','I1'])
        self.assertEqual(asset['asset_ids'],['arena','fighter'])
        self.assertEqual([x['id'] for x in composition['issues']],['I3','I1'])
        self.assertIn('[I1] source I1',_asset_problem(asset,'fighter'))
        self.assertIn('[I2] accept I2',asset['rendered_acceptance'])

    def test_unified_planner_owns_prompt_and_code_route(self):
        issue={'id':'rope','problem':'jump rope does not line up with boxer',
               'route':'composition','asset_ids':[],
               'source_observation':{'status':'clean','evidence':'isolated rope is intact'},
               'rendered_observation':{'status':'defective','evidence':'rope misses the hands'},
               'evidence':[{'scenario':'training','frame':4,'observation':'rope misses hands'}],
               'rendered_acceptance':['rope follows hands']}
        answer={'decisions':[{'issue_id':'rope','method':'both','layer':'actor_motion',
                              'reason':'replace rope motion and retime it',
                              'asset_prompts':[{'asset_id':'rope','prompt':'A complete jump rope swing.'}],
                              'code_instructions':'Use the new rope frames at the hand anchors.'}]}
        with tempfile.TemporaryDirectory() as td:
            project=Path(td);(project/'main.gd').write_text('extends Node\n')
            with patch.object(models,'content',return_value=[]), \
                 patch.object(models,'material_content',return_value=[]), \
                 patch.object(models,'call',return_value=answer) as invoke:
                result=models.plan_repair({'assets':{'bindings':{'rope':['rope.png']}}},
                    SimpleNamespace(instructions='jump rope'),project,
                    {'assets':[{'id':'rope','prompt':'old rope','kind':'animation'}]},
                    {'parameters':{}},{'coverage_passed':True,'issues':[issue]},[],[])
        self.assertEqual(result['issues'][0]['route'],'both')
        self.assertEqual(result['asset_prompts']['rope'],'A complete jump rope swing.')
        self.assertIn('hand anchors',result['issues'][0]['code_instructions'])
        self.assertEqual(invoke.call_args.kwargs['backend'],'codex')

    def test_unified_planner_downgrades_missing_h3_prompt_to_code(self):
        issue={'id':'rope','problem':'rope misses the boxer','route':'both','asset_ids':['rope'],
               'source_observation':{'status':'defective','evidence':'bad rope'},
               'rendered_observation':{'status':'defective','evidence':'bad scene'},
               'evidence':[{'scenario':'training','frame':1,'observation':'bad scene'}],
               'rendered_acceptance':['rope follows boxer']}
        answer={'decisions':[{'issue_id':'rope','method':'both','layer':'actor_motion',
                              'reason':'rope must be integrated','asset_prompts':[],
                              'code_instructions':'Animate a rope around the boxer.'}]}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(models,'content',return_value=[]), \
             patch.object(models,'material_content',return_value=[]), \
             patch.object(models,'call',return_value=answer):
            project=Path(td);(project/'main.gd').write_text('extends Node\n')
            result=models.plan_repair({'assets':{'bindings':{'rope':['rope.png']}}},
                SimpleNamespace(instructions='jump rope'),project,
                {'assets':[{'id':'rope','prompt':'old'}]},{'parameters':{}},
                {'coverage_passed':True,'issues':[issue]},[],[])
        self.assertEqual(result['issues'][0]['route'],'composition')
        self.assertEqual(result['issues'][0]['asset_ids'],[])
        self.assertEqual(result['decisions'][0]['effective_method'],'code')
        self.assertIn('No complete H3 prompt',result['decisions'][0]['fallback'])

    def test_asset_worker_uses_unified_prompt_without_rewriting_it(self):
        import sys
        from gamecraft_training.package import asset_worker
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            write_json(root/'asset_plan.json',{'style':'arcade','assets':[{'id':'rope','prompt':'old prompt'}]})
            write_json(root/'request.json',{'workspace':str(root),'asset':'rope','problem':'bad rope',
                'planned_prompt':'A complete clean rope swing.','result':str(root/'result.json'),
                'candidates':1})
            def generated(*args,**kwargs):
                write_json(root/'asset_history.json',[{'asset':'rope','version':'v1','eligible':True}])
                write_json(root/'asset_manifest.json',{'assets':{'rope':{'version':'v1'}}})
                return False
            with patch.object(sys,'argv',['asset_worker',str(root/'request.json')]), \
                 patch.object(asset_worker,'validate_generic'), \
                 patch.object(asset_worker,'generate',side_effect=generated), \
                 patch.object(asset_worker,'rewrite_prompt',side_effect=AssertionError('second prompt agent')):
                asset_worker.main()
            self.assertEqual(json.loads((root/'asset_plan.json').read_text())['assets'][0]['prompt'],
                             'A complete clean rope swing.')
            self.assertEqual(json.loads((root/'result.json').read_text())['status'],'generated')

    def test_batched_repair_keeps_safe_partial_progress(self):
        def issue(ident):
            return {'id':ident,'problem':ident,'route':'composition','asset_ids':[],
                    'source_observation':{'status':'clean','evidence':'clean'},
                    'rendered_observation':{'status':'defective','evidence':'bad'},
                    'evidence':[{'scenario':'check','frame':0,'observation':'bad'}],
                    'rendered_acceptance':[f'accept {ident}']}
        diagnosis=_batch_diagnosis([issue('fixed'),issue('remaining')],'composition')
        quality={'build_ok':True,'errors':[],'num_demos':1,'scores':{'art':.5}}
        feedback={'criteria':[{'id':'art'}],'target':'art'}
        def repair(config,adapter,item,*args):
            passed=item['id']=='fixed'
            return {'status':'gone' if passed else 'still_there','coverage_passed':True,
                    'coverage_reason':'shown','defect_fixed':passed,'regressions':[],
                    'acceptance_checks':[{'criterion':item['rendered_acceptance'][0],
                                          'passed':passed,'evidence':'frame'}]}
        verdict,accepted,reason=_verify_repairs(SimpleNamespace(repair=repair),{},None,diagnosis,
                                                quality,quality,[],[],feedback)
        self.assertTrue(accepted)
        self.assertEqual(verdict['fixed_issue_ids'],['fixed'])
        self.assertEqual(verdict['unresolved_issue_ids'],['remaining'])
        self.assertIn('accepted fixes for fixed',reason)


    def test_skill_gate_uses_execution_not_game_outcome(self):
        judgment={'status':'followed','critical_violation':False}
        self.assertTrue(accepts_skill_execution(judgment))
        judgment['status']='not_exercised'
        self.assertFalse(accepts_skill_execution(judgment))
        judgment.update(status='followed',critical_violation=True)
        self.assertFalse(accepts_skill_execution(judgment))

    def test_behavior_models_receive_only_player_visible_state(self):
        result={'scenario':'fight','replay':{'private_final':99},'decisions':[
            {'frame':6,'action':'jab','reason':'visible opening','image':'frame.png',
             'player_state':{'tell':None,'stamina':80},
             'evaluation_state':{'tell':None,'stamina':80,'last_punch_recovery':11}}]}
        visible=sample([result],player_visible=True)
        self.assertEqual(visible[0]['scenario'],'episode-000')
        self.assertEqual(visible[0]['state'],{'tell':None,'stamina':80})
        self.assertNotIn('last_punch_recovery',visible[0]['state'])
        trace=models._behavior_trace([result])
        self.assertEqual(trace[0]['scenario'],'episode-000')
        self.assertNotIn('final_state',trace[0])
        self.assertEqual(trace[0]['decisions'][0]['state'],{'tell':None,'stamina':80})

        adapter=SimpleNamespace(instructions='rules',actions={'wait':None,'jab':'J'},
                                observation_fields={'tell':'opponent_tell','stamina':'stamina'},
                                state_description='Only tell and stamina are public.')
        previous=[{'round':1,'sides':{'behavior':{'action':'reject','reason':'private value was 11',
                                                  'verification':{'status':'not_followed','reason':'private value was 11'}}},
                   'private_record':{'last_punch_recovery':11}}]
        proposal={'reason':'test','skill':'jab on a visible opening','acceptance':['visible opening -> jab']}
        verdict={'status':'followed','critical_violation':False,'reason':'followed','evidence':[]}
        with patch.object(models,'content',return_value=[]) as evidence, \
             patch.object(models,'judge_call',side_effect=[proposal,verdict]) as invoke:
            models.skill(adapter,'',[result],previous)
            models.behavior(adapter,proposal,[result])
        skill_data=invoke.call_args_list[0].args[1]
        self.assertEqual(skill_data['public_state_fields'],['tell','stamina'])
        self.assertEqual(skill_data['previous_attempts'],[
            {'round':1,'outcome':'reject','execution_status':'not_followed'}])
        behavior_data=invoke.call_args_list[1].args[1]
        self.assertNotIn('last_punch_recovery',json.dumps(behavior_data))
        self.assertNotIn('private_final',json.dumps(behavior_data))
        self.assertTrue(all(call.kwargs['player_visible'] for call in evidence.call_args_list))

    def test_targeted_world_gate_does_not_require_global_score_gain(self):
        quality={'build_ok':True,'errors':[],'num_demos':2,'scores':{'art':.5,'layout':.7}}
        diagnosis={'acceptance':['fighter remains grounded','scale remains stable']}
        verdict={'coverage_passed':True,'coverage_reason':'matched frames show the fighter',
                 'defect_fixed':True,'before_severity':.8,'after_severity':.2,'regressions':[],
                 'acceptance_checks':[{'criterion':x,'passed':True,'evidence':'visible'} for x in diagnosis['acceptance']]}
        feedback={'criteria':[{'id':'art'},{'id':'layout'}],'regression_tolerance':.02,'targeted_min_gain':.1}
        self.assertTrue(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        lower={'build_ok':True,'errors':[],'num_demos':2,'scores':{'art':.2,'layout':.2}}
        verdict.update(before_severity=.51,after_severity=.50)
        self.assertTrue(accept_targeted(quality,lower,feedback,diagnosis,verdict)[0])
        verdict['acceptance_checks'][0]['passed']=False
        self.assertTrue(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])

    def test_targeted_world_gate_keeps_partial_progress_but_blocks_critical_regression(self):
        quality={'build_ok':True,'errors':[],'num_demos':1,'scores':{'art':.5}}
        feedback={'criteria':[{'id':'art'}],'target':'art'}
        diagnosis={'rendered_acceptance':['rope passes under feet','boxer lands in sync']}
        verdict={'status':'still_there','coverage_passed':True,'defect_fixed':False,
                 'target_change':'better','before_severity':.8,'after_severity':.5,
                 'acceptance_checks':[{'criterion':x,'passed':False,'evidence':'not complete'}
                                      for x in diagnosis['rendered_acceptance']],
                 'regressions':[]}
        self.assertTrue(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        verdict['target_change']='same'
        self.assertFalse(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        verdict['target_change']='better'
        verdict['regressions']=[{'problem':'menu alignment','severity':'minor'}]
        self.assertTrue(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        verdict['regressions']=[{'problem':'cannot start game','severity':'critical'}]
        self.assertFalse(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        verdict.update(status='gone',defect_fixed=True,target_change='same',regressions=[])
        self.assertFalse(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])
        verdict['status']='not_exercised'
        self.assertFalse(accept_targeted(quality,quality,feedback,diagnosis,verdict)[0])

    def test_batched_partial_progress_stays_unresolved(self):
        issues=[{'id':key,'rendered_acceptance':[key]} for key in ('rope','bag')]
        diagnosis={'id':'batch','issues':issues}
        quality={'build_ok':True,'errors':[],'num_demos':1,'scores':{'art':.5}}
        feedback={'criteria':[{'id':'art'}],'target':'art'}
        def repair(_config,_adapter,issue,*_args):
            improved=issue['id']=='rope'
            return {'status':'still_there','coverage_passed':True,'defect_fixed':False,
                    'target_change':'better' if improved else 'same',
                    'before_severity':.8,'after_severity':.5 if improved else .8,
                    'acceptance_checks':[{'criterion':issue['id'],'passed':False,'evidence':'incomplete'}],
                    'regressions':[]}
        verdict,accepted,_=_verify_repairs(SimpleNamespace(repair=repair),{},None,diagnosis,
                                           quality,quality,[],[],feedback)
        self.assertTrue(accepted)
        self.assertEqual(verdict['fixed_issue_ids'],[])
        self.assertEqual(verdict['improved_issue_ids'],['rope'])
        self.assertEqual(verdict['unresolved_issue_ids'],['rope','bag'])

    def test_behavior_metrics_are_optional_reporting_only(self):
        results=[{'replay_verified':True,'replay':{'game_specific_value':99}}]
        self.assertEqual(aggregate(results,SimpleNamespace(),{'behavior':{}}),{})

    def test_judge_backend_is_independent_from_local_player(self):
        with patch.dict(os.environ,{'LLM_BACKEND':'local','OPTIMIZER_BACKEND':'local',
                                    'GAMECRAFT_JUDGE_BACKEND':'codex','GAMECRAFT_JUDGE_MODEL':'gpt-5.5'}), \
             patch('llm.call',return_value={'ok':True}) as invoke:
            answer=models.judge_call('judge',{'evidence':'x'},[],'test_judge',{'ok':{'type':'boolean'}})
        self.assertEqual(answer,{'ok':True})
        self.assertEqual(invoke.call_args.args[0],'gpt-5.5')
        self.assertEqual(invoke.call_args.kwargs['backend'],'codex')

    def test_codex_game_code_may_change_state_if_behavior_objective_does_not_regress(self):
        adapter=SimpleNamespace(behavior_metrics=lambda r:r['metrics'])
        config={'behavior':{'metrics':[{'id':'wins','direction':'max'},
                                      {'id':'stamina','direction':'max'}]}}
        before=[{'replay_verified':True,'metrics':{'wins':1.0,'stamina':80.0}}]
        improved=[{'replay_verified':True,'metrics':{'wins':2.0,'stamina':20.0}}]
        report=verify_behavior_non_regression(before,improved,adapter,config)
        self.assertIn('wins',report['improved_metrics'])
        regressed=[{'replay_verified':True,'metrics':{'wins':0.0,'stamina':100.0}}]
        report=verify_behavior_non_regression(before,regressed,adapter,config)
        self.assertIn('wins',report['regressed_metrics'])
        self.assertEqual(report['acceptance_role'],'report_only')

    def package(self,base,task,asset):
        fixture=Path(__file__).parent/'fixtures'
        draft=base/'draft';draft.mkdir()
        shutil.copytree(fixture/'counter_game',draft/'game')
        write_json(draft/'game/game_plan.json',{'task_id':task})
        (draft/'game/asset.png').write_bytes(b'old')
        write_json(draft/'game/visual.json',{'scale':1.0})
        (draft/'adapter').mkdir()
        code=(fixture/'counter_adapter.py').read_text().replace("'test-counter'",repr(task)).replace("Path(__file__).with_suffix('.gd')","Path(__file__).with_name('bridge.gd')")
        code+='\ndef create_adapter():\n    a=CounterAdapter()\n    a.behavior_metrics=lambda r: {"points":r["replay"]["points"]}\n    return a\n'
        (draft/'adapter/__init__.py').write_text(code)
        shutil.copy2(fixture/'counter_adapter.gd',draft/'adapter/bridge.gd')
        write_json(draft/'initialization.json',{'schema_version':1,'task_id':task,'state':'draft','version':'v1'})
        write_json(draft/'asset_plan.json',{'schema_version':1,'task_id':task,'style':'flat',
                   'assets':[{'id':asset,'kind':'sprite','size':[32,32],'frames':1,'fps':1,'description':'a test object','prompt':'one object','requirements':'clear'}]})
        write_json(draft/'composition_spec.json',{'mutable':{},'parameters':{'scale':{'file':'visual.json','path':['scale'],'type':'float','min':.8,'max':1.2}},'fixed':['main.gd']})
        write_json(draft/'scenarios.json',[{'id':p,'purpose':p,'actions':['jab']} for p in ['initialization','train','validation','confirmation','test']])
        write_json(draft/'requirements.json',{'requirements':[{'id':'R1','description':'The counter interaction works',
                   'verification':'Run the declared counter scenario','dynamic':True,'quote':'counter','asset_ids':[]}]})
        (draft/'instruction.md').write_text('Implement the counter interaction.')
        write_json(draft/'acceptance_plan.json',{'requirements':[{'id':'R1','implementation':'Counter game',
                   'scenarios':['initialization']}]})
        write_json(draft/'training.json',{'schema_version':1,'behavior':{'metrics':[{'id':'points','direction':'max','min_gain':1}]},
                   'assets':{'bindings':{asset:['asset.png']}},'quality':{'criteria':[{'id':k,'description':k} for k in ['art','layout']],
                   'targets':{'asset':'art','composition':'layout'},'min_gain':.05}})
        with patch('gamecraft_behavior.runtime.rollout',return_value={'replay_verified':True,'decisions':[1]}):
            freeze(draft,base/'frozen','godot')
        return base/'frozen'

    def test_initialization_profile_uses_scripts_without_behavior_training_and_freezes_result(self):
        for repair_needed in (False,True):
          with self.subTest(repair=repair_needed),tempfile.TemporaryDirectory() as td:
            b=Path(td);package=self.package(b,'init_game','hero');calls=[]
            def rollout(project,out,skill,decide,**kw):
                out=Path(out);out.mkdir(parents=True,exist_ok=True)
                action=decide(None,None,None,None)['action'];calls.append((kw['scenario'],action,skill))
                self.assertEqual(kw['step_frames'],6)  # package default, not caller's RSI cadence
                decisions=[{'frame':0,'action':action,'reason':'script','image':str(out/'image.png')}]
                write_json(out/'trace.json',trace(decisions,kw['step_frames'],kw['adapter']))
                (out/'video.mp4').write_bytes(b'fake video')
                return {'replay_verified':True,'replay':{'points':2},'scenario':kw['scenario'],
                        'decision_frames':kw['step_frames'],
                        'normal_launch':kw['normal_launch'],'project_sha256':digest(project),
                        'trace':str(out/'trace.json'),'video':str(out/'video.mp4'),'decisions':decisions}
            def gate(config,adapter,root,project,runs):
                raise AssertionError('Independent initialization Judge must not run')
            diagnosis={'id':'fix','route':'composition','asset_ids':[],
                       'problem':'missing interaction','rendered_acceptance':['interaction works']}
            service=SimpleNamespace(
                trajectory=lambda _config,_adapter,_project,tasks,*args:{'scenarios':[
                    {'id':case['id'],'goal':'show counter','reason':'valid inputs',
                     'step_frames':case.get('step_frames',6),'actions':case['actions']} for case in tasks]},
                skill=lambda *args: (_ for _ in ()).throw(AssertionError('Behavior Skill must be disabled')),
                judge=lambda config,adapter,project,results:{'build_ok':True,'errors':[],
                    'num_demos':len(results),'scores':{'art':1.0,'layout':1.0}},
                initialization=gate,
                repair=lambda *args:{'status':'gone','coverage_passed':True,'defect_fixed':True,
                    'regressions':[],'reason':'fixed','acceptance_checks':[
                        {'criterion':'interaction works','passed':True,'evidence':'runtime'}]},
                selection=lambda *args,**kwargs:[{'preferred_role':'candidate',
                    'evidence_valid':True,'confidence':'high','critical_regression':False,
                    'reason':'candidate is better'}],
                route=lambda *args:{'coverage_passed':True,'coverage_reason':'complete',
                    'missing_evidence':[],'issues':[diagnosis] if repair_needed else []})
            def editor(project,candidate,out,**kw):
                self.assertEqual(kw['evaluation']['execution_profile']['name'],'initialization')
                shutil.copytree(project,candidate)
                (candidate/'fixed.gd').write_text('extends Node\n')
                return {'changed_files':['fixed.gd']}
            class NoopGenerator:
                def __init__(self,root,*args):
                    from gamecraft_video_skill import initialize
                    self.root=Path(root);self.root.mkdir();initialize(self.root)
            layer=b/'global.json'
            write_json(layer,{'schema_version':1,'rules':[{'id':'test_rule','applies_when':{},
                       'instruction':'A test generation rule.'}]})
            with patch('gamecraft_behavior.runtime.rollout',side_effect=rollout), \
                 patch('gamecraft_video_skill.decisions.learn',return_value={}), \
                 patch('gamecraft_training.package.loop.player',side_effect=AssertionError('No Player')):
                result=run(package,b/'run',godot='godot',asset_python='unused',profile='initialization',
                           step_frames=3,video_skill=True,global_video_skill=layer,
                           services=service,rollout_fn=rollout,generator_factory=NoopGenerator,
                           composition_mode='codex',composition_policy={'protected':[]},code_editor=editor)
            self.assertEqual(result['state'],'completed',result['errors'])
            sides=json.loads((b/'run/history.json').read_text())[0]['sides']
            self.assertEqual(sides['behavior']['action'],'disabled')
            if repair_needed:self.assertEqual(sides['composition']['action'],'accept')
            self.assertTrue(all(skill=='' for _,_,skill in calls))
            inspect_package(result['package']);inspect_package(b/'run/initial');inspect_package(package)
            self.assertEqual(json.loads((Path(result['package'])/'video_skill/global.json').read_text())['rules'][0]['id'],'test_rule')


    def test_package_cli_keeps_rsi_errors_nonzero(self):
        import sys
        from gamecraft_training.package import __main__ as cli
        for profile,expected_error in [('rsi',True),('initialization',False)]:
            args=['runner','--game-package','unused','--output','unused','--asset-python','unused',
                  '--godot','unused','--profile',profile]
            with self.subTest(profile=profile),patch.object(sys,'argv',args), \
                 patch.object(cli,'run',return_value={'state':'completed_with_errors','errors':[{'reason':'failed'}]}):
                if expected_error:
                    with self.assertRaises(SystemExit) as exc:cli.main()
                    self.assertEqual(exc.exception.code,2)
                else:cli.main()

    def test_package_cli_passes_independent_codex_player_configuration(self):
        from gamecraft_training.package import __main__ as cli
        args=['runner','--game-package','unused','--output','unused','--asset-python','unused',
              '--godot','unused','--player-backend','codex','--player-model','gpt-6-luna']
        with patch.object(sys,'argv',args),patch.object(cli,'run',return_value={'state':'completed','errors':[]}) as call:
            cli.main()
        self.assertEqual(call.call_args.kwargs['player_backend'],'codex')
        self.assertEqual(call.call_args.kwargs['player_model'],'gpt-6-luna')
        self.assertEqual(call.call_args.kwargs['judge_backend'],'local')
        self.assertEqual(call.call_args.kwargs['max_decisions'],40)

    def test_profile_boundary_rejects_invalid_options_before_loading_package(self):
        for kwargs in ({'profile':'initialization','initial_behavior_skill':'play'},
                       {'global_video_skill':'global.json'},
                       {'video_skill':True,'global_video_skill_store':'store'},
                       {'player_backend':'codex'}):
            with self.subTest(options=kwargs),self.assertRaises(ValueError):
                run('missing-package','unused',godot='unused',asset_python='unused',**kwargs)

    def test_rsi_plans_once_for_three_rounds_and_uses_proposed_skill(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);package=self.package(base,'skill_game','hero');calls=[];plans=[]
            objective={'goal':'complete game','success_condition':'last stage complete',
                       'failure_condition':'definitive defeat','continuation_rule':'continue intermediate wins'}
            planned={**objective,'entry_mode':'scenario','entry_id':'train',
                     'selection_reason':'The training fixture reaches the core mechanic.'}
            class NoopGenerator:
                def __init__(self,root,*args):
                    self.root=Path(root);self.root.mkdir()
            def rollout(project,out,skill,decide,**kwargs):
                calls.append((kwargs['scenario'],skill))
                self.assertFalse(kwargs['normal_launch'])
                self.assertEqual(decide.run_objective,objective)
                return {'replay_verified':True,'replay':{'points':len(calls)},
                        'scenario':kwargs['scenario'],'decisions':[
                            {'frame':0,'action':'wait','reason':'observe','image':'frame.png'}]}
            service=SimpleNamespace(
                plan_rsi_objective=lambda *a,**k: plans.append(k) or planned,
                skill=lambda adapter,skill,training,history:{'skill':'tap on opening','reason':'Try tap'},
                behavior=lambda *args: (_ for _ in ()).throw(AssertionError('No candidate replay judge')),
                judge=lambda config,adapter,project,episodes:{'build_ok':True,'errors':[],
                    'num_demos':len(episodes),'scores':{'art':.5,'layout':.5}},
                route=lambda *args:{'coverage_passed':True,'coverage_reason':'observed',
                    'missing_evidence':[],'issues':[]})
            result=run(package,base/'run',godot='unused',asset_python='unused',rounds=3,
                       services=service,rollout_fn=rollout,generator_factory=NoopGenerator)
            self.assertEqual(len(plans),1)
            self.assertEqual(plans[0]['frame_budget'],240)
            self.assertEqual(calls[0][0],'train')
            self.assertEqual([s for _,s in calls],['','tap on opening','tap on opening'])
            self.assertEqual(json.loads((base/'run/rsi_objective.json').read_text())['objective'],objective)
            self.assertEqual(json.loads((base/'run/rsi_objective.json').read_text())['entry'],
                             {key:planned[key] for key in ('entry_mode','entry_id','selection_reason')})
            self.assertEqual(json.loads((base/'run/run.json').read_text())['rsi_entry']['entry_id'],'train')
            self.assertEqual(result['train']['points'],3)
            self.assertIsNone(result['test'])
            history=json.loads((base/'run/history.json').read_text())
            self.assertEqual([row['sides']['behavior']['action'] for row in history],
                             ['adopt_unverified','skip','skip'])
            self.assertFalse((base/'run/round-001/validation_candidate').exists())

    def test_shared_loop_acceptance_rejection_and_errors(self):
        cases=[('game_alpha','hero','accept'),('game_beta','vehicle','accept'),('game_alpha','hero','code'),
               ('game_alpha','hero','batch'),('game_alpha','hero','parallel'),
               ('game_alpha','hero','parallel_both'),('game_alpha','hero','unified'),('game_alpha','hero','grouped'),
               ('game_alpha','hero','reject'),('game_alpha','hero','physics'),('game_alpha','hero','error'),
               ('game_alpha','hero','coverage'),('game_alpha','hero','both'),('game_alpha','hero','handoff')]
        for task,asset,mode in cases:
            with self.subTest(task=task,mode=mode),tempfile.TemporaryDirectory() as td:
                b=Path(td);package=self.package(b,task,asset);calls=[]
                self_test=self
                generator_started=Event();editor_started=Event()
                def acceptance_policy(project, output, **kw):
                    self.assertIn('candidate_changes',kw['playtest_context'])
                    output.parent.mkdir(parents=True,exist_ok=True)
                    output.write_text('def act(state, recent): return {"action":"wait"}\n')
                    def policy(*args,**kwargs):return {'action':'wait','reason':'acceptance test'}
                    return policy
                def rollout(project,out,skill,decide,**kw):
                    out=Path(out);out.mkdir(parents=True)
                    calls.append((kw['scenario'],str(out)))
                    action=decide(None,None,None,None)['action'] if decide.__name__=='scripted' else ('jab' if skill else 'wait')
                    state={'points':2 if action=='jab' else 0,'ticks':12,'terminal':False}
                    if mode=='physics' and Path(project).name=='candidate':state['points']+=1
                    (out/'video.mp4').write_bytes(b'fake video')
                    (out/'trace.json').write_text('{}')
                    if '/candidate_playtest/' in str(out) or '/groups/' in str(out):
                        self.assertEqual(skill,'')
                    return {'replay_verified':True,'replay':state,'scenario':kw['scenario'],
                            'normal_launch':kw['normal_launch'],'project_sha256':digest(project),
                            'video':str(out/'video.mp4'),'trace':str(out/'trace.json'),
                            'decisions':[{'frame':0,'action':action,'reason':'test','image':str(out/'image.png')}]}
                class Generator:
                    def __init__(self,root,*args):
                        self.root=Path(root);self.root.mkdir()
                        write_json(self.root/'asset_plan.json',args[0])
                    def __call__(self,a,problem,out):
                        if mode=='unified':
                            self_test.assertEqual(problem['prompt'],'A clean single hero action.')
                        if mode in ('parallel','parallel_both'):
                            generator_started.set()
                            if not editor_started.wait(5):raise AssertionError('Codex did not overlap H3')
                        if mode=='error':return {'status':'error','reason':'H3 unavailable'}
                        if mode=='both':return {'status':'rejected','reason':'source candidate failed'}
                        from PIL import Image
                        image=Image.new('RGBA',(32,32),(0,0,0,0))
                        for x in range(8,24):
                            for y in range(8,24):image.putpixel((x,y),(5,5,5,255))
                        image.save(self.root/'new.png')
                        write_json(self.root/'asset_manifest.json',{'assets':{a:{'files':['new.png']}}})
                        return {'status':'generated'}
                def judge(config,adapter,project,results):
                    return {'build_ok':True,'num_demos':len(results),'errors':[],
                            'scores':{'art':.9 if mode!='reject' and (Path(project)/'asset.png').read_bytes()==b'new' else .5,
                                      'layout':.9 if mode!='reject' and json.loads((Path(project)/'visual.json').read_text())['scale']>1 else .5}}
                routed=[{'id':'source','problem':'observed asset defect','route':'both' if mode=='both' else 'asset','asset_ids':[asset],
                                   'source_observation':{'status':'defective','evidence':'bad source'},
                                   'rendered_observation':{'status':'defective','evidence':'bad render'},
                                   'evidence':[{'scenario':'validation','frame':0,'observation':'bad source'}],
                                   'rendered_acceptance':['clean render']},
                                  {'id':'layout','problem':'observed code defect','route':'composition','asset_ids':[],
                                   'source_observation':{'status':'clean','evidence':'source is intact'},
                                   'rendered_observation':{'status':'defective','evidence':'bad layout'},
                                   'evidence':[{'scenario':'validation','frame':0,'observation':'bad layout'}],
                                   'rendered_acceptance':['aligned']}]
                if mode in ('both','handoff','parallel_both','unified'):
                    routed=routed[:1]
                    if mode=='handoff':routed[0]['route']='asset'
                    elif mode in ('parallel_both','unified'):routed[0]['route']='both'
                if mode=='batch':
                    routed.append({'id':'hud','problem':'observed HUD defect','route':'composition','asset_ids':[],
                                   'source_observation':{'status':'clean','evidence':'source is intact'},
                                   'rendered_observation':{'status':'defective','evidence':'HUD overlaps playfield'},
                                   'evidence':[{'scenario':'validation','frame':0,'observation':'HUD overlap'}],
                                   'rendered_acceptance':['HUD is separated from playfield']})
                comparison_calls=[];repair_calls=[];editor_calls=[]
                def repair(config,adapter,diagnosis,before,after,before_runs,after_runs):
                    repair_calls.append(diagnosis['id'])
                    remains=mode in ('reject','handoff')
                    return {'status':'still_there' if remains else 'gone',
                        'coverage_passed':True,'coverage_reason':'matched evidence','defect_fixed':not remains,
                        'before_severity':.8,'after_severity':.8 if remains else .2,'regressions':[],
                        'acceptance_checks':[{'criterion':x,'passed':not remains,'evidence':'test'}
                                             for x in diagnosis['rendered_acceptance']],
                        'reason':'unchanged' if remains else 'fixed'}
                service=SimpleNamespace(skill=lambda *args:{'skill':'new','reason':'better'},judge=judge,
                    behavior=lambda *args:{'status':'followed','critical_violation':False,
                        'reason':'candidate follows the proposed skill','evidence':[]},
                    route=lambda *args:{'coverage_passed':mode!='coverage','coverage_reason':'no active interaction' if mode=='coverage' else 'covered',
                        'missing_evidence':['contact'] if mode=='coverage' else [],'issues':[] if mode=='coverage' else routed},
                    source_repair=lambda *args: (_ for _ in ()).throw(
                        AssertionError('RSI must judge rendered game evidence, not source files')),
                    repair=repair,
                    selection=lambda _config,_adapter,diagnosis,*args,**kwargs:
                        comparison_calls.append(diagnosis) or [{
                            'preferred_role':'incumbent' if mode in ('reject','handoff') else 'candidate',
                            'evidence_valid':True,'confidence':'high','critical_regression':False,
                            'reason':'matched replay comparison'}],
                    parameters=lambda *args:{'reason':'align','changes':[{'parameter':'scale','value':1.1}]})
                if mode=='unified':
                    routed[0]['route']='both'
                    service.plan_repair=lambda *args:{'coverage_passed':True,'coverage_reason':'covered',
                        'missing_evidence':[],'unified':True,
                        'asset_prompts':{asset:'A clean single hero action.'},
                        'issues':[dict(routed[0],code_instructions='Integrate the generated hero.',
                                       video_layer='actor_motion')]}
                if mode=='grouped':
                    def grouped_plan(*args):
                        from gamecraft_training.package.boundary_planner import validate
                        from gamecraft_assets.contract import material_intent
                        material=dict(args[3]['assets'][0])
                        scope,motion=material_intent(material)
                        material.update(generation_scope=scope,motion=motion,scope_reason="Preserve material role.",includes=[asset],game_controlled=["input"])
                        return validate({'groups':[{'id':'source','issue_ids':['source'],
                            'root_cause':'source placement','grouping_reason':'shared presentation integration',
                            'current_scope':scope,'affected_assets':[asset],'required_controls':['input'],
                            'escalation_reason':'','skip_reason':'','candidates':[{
                                'kind':'same_level','scope':scope,'video_controls':['appearance'],
                                'code_controls':['input','placement'],'code_instructions':'Integrate generated hero.',
                                'materials':[{'operation':'replace','asset':material}]}]},
                            {'id':'layout','issue_ids':['layout'],'root_cause':'layout placement',
                             'grouping_reason':'independent code placement','current_scope':scope,
                             'affected_assets':[],'required_controls':['input'],
                             'escalation_reason':'','skip_reason':'','candidates':[{
                                 'kind':'same_level','scope':scope,'video_controls':[],
                                 'code_controls':['input','layout'],'code_instructions':'Align the layout.',
                                 'materials':[]}]}]},args[5],args[3])
                    service.plan_candidates=grouped_plan
                def editor(project,candidate,out,**kw):
                    editor_calls.append(kw['diagnosis'])
                    self.assertEqual(kw['diagnosis']['route'],
                        'both' if mode in ('grouped','parallel_both','unified') else 'composition')
                    if mode=='grouped':
                        self.assertEqual([u['id'] for u in kw['diagnosis']['boundary_plan']['repair_units']],
                                         ['source','layout'])
                    if mode=='unified':
                        self.assertNotEqual((Path(project)/'asset.png').read_bytes(),b'old')
                        self.assertIn('generated hero',kw['diagnosis']['code_instructions'])
                    if mode in ('parallel','parallel_both'):
                        editor_started.set()
                        if not generator_started.wait(5):raise AssertionError('H3 did not overlap Codex')
                    shutil.copytree(project,candidate)
                    write_json(candidate/'visual.json',{'scale':1.1})
                    (candidate/'new_presentation.gd').write_text('extends Node\n')
                    return {'changed_files':['visual.json','new_presentation.gd'],'summary':'code repair','tests':[]}
                options={'composition_mode':'codex','composition_policy':{'protected':['game_plan.json']},'code_editor':editor} if mode in ('code','batch','parallel','parallel_both','unified','grouped') else {}
                if mode=='accept':options.update(player_backend='codex',player_model='gpt-6-luna')
                with patch('gamecraft_training.package.playtests.program_player',side_effect=acceptance_policy):
                    r=run(package,b/'run',godot='unused',asset_python='unused',services=service,rollout_fn=rollout,generator_factory=Generator,rsi_goal_mode='legacy',**options)
                if mode=='accept':
                    self.assertEqual(json.loads((b/'run/run.json').read_text())['player'],
                                     {'backend':'codex','model':'gpt-6-luna'})
                h=json.loads((b/'run/history.json').read_text())[0]
                expected={'accept':['adopt_unverified','accept','accept'],'code':['adopt_unverified','accept','accept'],
                          'batch':['adopt_unverified','accept','accept'],'parallel':['adopt_unverified','accept','accept'],
                          'parallel_both':['adopt_unverified','accept','accept'],'unified':['adopt_unverified','accept','accept'],'grouped':['adopt_unverified','accept','accept'],
                          'reject':['adopt_unverified','reject','reject'],
                          'physics':['adopt_unverified','accept','accept'],'error':['adopt_unverified','error','accept'],
                          'coverage':['adopt_unverified','skip','skip'],'both':['adopt_unverified','reject','skip'],
                          'handoff':['adopt_unverified','reject','skip']}[mode]
                self.assertEqual([s['action'] for s in h['sides'].values()],expected)
                self.assertEqual(r['state'],'completed_with_errors' if mode=='error' else 'completed')
                selected=json.loads((b/'run/current_playtest.json').read_text())
                self.assertEqual(selected['project'],r['project'])
                if mode in ('reject','coverage','both','handoff'):
                    self.assertNotIn('playtest',selected['episodes'][0])
                else:
                    self.assertEqual(selected['episodes'][0]['playtest']['purpose'],'candidate_acceptance')
                if mode=='grouped':
                    self.assertEqual([g['winner'] for g in h['groups']],['same_level'])
                    self.assertEqual(len(editor_calls),1)
                    self.assertEqual([d['id'] for d in editor_calls],['round_bundle'])
                    self.assertEqual(len(comparison_calls),1)
                    self.assertTrue((b/'run/current_asset_plan.json').exists())
                    self.assertEqual([i['id'] for i in comparison_calls[-1]['issues']],['source','layout'])
                if mode=='batch':
                    self.assertEqual([x['id'] for x in comparison_calls[0]['issues']],
                                     ['source','layout','hud'])
                if mode in ('parallel','parallel_both'):
                    self.assertTrue(h['sides']['composition']['parallel_edit'])
                    self.assertTrue((Path(h['project_after'])/'new_presentation.gd').exists())
                    self.assertNotEqual((Path(h['project_after'])/'asset.png').read_bytes(),b'old')
                    self.assertTrue(any('/composition/candidate_playtest/' in path for _,path in calls))
                    self.assertFalse(any('/asset/candidate_playtest/' in path for _,path in calls))
                    self.assertFalse((b/'run/round-001/asset/candidate_playtest').exists())
                    self.assertIn('source',[x['id'] for x in
                        comparison_calls[0].get('issues',[comparison_calls[0]])])
                    self.assertFalse(any('/rule_regression/' in path for _,path in calls))
                if mode in ('accept','code','batch'):
                    self.assertFalse((b/'run/round-001/asset/candidate_playtest').exists())
                    self.assertTrue((b/'run/round-001/composition/candidate_playtest').exists())
                self.assertEqual(repair_calls,[])
                self.assertIsNone(r['test'])
                self.assertFalse(r['final_project_evaluated'])
                self.assertEqual(sum('/round-001/train/' in path for _,path in calls),1)
                self.assertFalse(any(s in ('validation','confirmation','test') for s,_ in calls))
                inspect_package(package)

    def test_router_drops_unshown_issues_and_requires_valid_asset_ownership(self):
        result={'scenario':'validation','decisions':[{'frame':0,'action':'wait','reason':'idle','image':'frame.png'}]}
        common=({'quality':{'criteria':[{'id':'art','description':'art'}]}},
                SimpleNamespace(instructions='fight'),Path('/tmp/project'),
                {'assets':[{'id':'fighter'}]},{'parameters':{}},{'scores':{'art':.5}},[result],[])
        no_coverage={'coverage_passed':False,'coverage_reason':'no contact','missing_evidence':['contact'],
                     'issues':[{'id':'invented','problem':'guess','route':'asset','asset_ids':['fighter'],
                                'source_observation':{'status':'defective','evidence':'bad source'},
                                'rendered_observation':{'status':'unobserved','evidence':'not shown'},
                                'evidence':[{'scenario':'validation','frame':0,'observation':'idle'}],
                                'rendered_acceptance':['better render']}]}
        with patch.object(models,'content',return_value=[]),patch.object(models,'material_content',return_value=[]),patch.object(models,'call',return_value=no_coverage):
            verdict=models.route(*common)
        self.assertFalse(verdict['coverage_passed'])
        self.assertEqual(verdict['issues'],[])
        bad_owner={'coverage_passed':True,'coverage_reason':'covered','missing_evidence':[],
                   'issues':[{'id':'missing','problem':'bad actor','route':'asset','asset_ids':[],
                              'source_observation':{'status':'defective','evidence':'bad source'},
                              'rendered_observation':{'status':'defective','evidence':'bad render'},
                              'evidence':[{'scenario':'validation','frame':0,'observation':'bad'}],
                              'rendered_acceptance':['fixed render']}]}
        with patch.object(models,'content',return_value=[]),patch.object(models,'material_content',return_value=[]),patch.object(models,'call',return_value=bad_owner),self.assertRaisesRegex(ValueError,'identify'):
            models.route(*common)
        duplicate_owner={**bad_owner,'issues':[{**bad_owner['issues'][0],'asset_ids':['fighter','fighter']}]}
        with patch.object(models,'content',return_value=[]),patch.object(models,'material_content',return_value=[]),patch.object(models,'call',return_value=duplicate_owner),self.assertRaisesRegex(ValueError,'unique'):
            models.route(*common)

    def test_unobserved_is_not_a_failed_repair(self):
        verdict={'status':'not_exercised','coverage_passed':False,'defect_fixed':False}
        self.assertEqual(state(verdict),'not_exercised')
        self.assertEqual(attempted_outcome(verdict),'unobserved')

    def test_pairwise_promotion_uses_one_clear_review_or_confirmation(self):
        first=label_review({'preferred':'B','confidence':'high','evidence_valid':True,'critical_regression':False},
                           {'A':'incumbent','B':'candidate'})
        second=label_review({'preferred':'A','confidence':'high','evidence_valid':True,'critical_regression':False},
                            {'A':'candidate','B':'incumbent'})
        self.assertTrue(promotion([first])[0])
        self.assertTrue(promotion([first,second])[0])
        self.assertFalse(needs_confirmation(first))
        ambiguous={**first,'preferred_role':'tie','confidence':'low'}
        self.assertTrue(needs_confirmation(ambiguous))
        second['preferred_role']='tie'
        self.assertFalse(promotion([first,second])[0])

    def test_pairwise_model_reverses_only_an_ambiguous_first_review(self):
        config={'quality':{'criteria':[{'id':'art','description':'art'}]}}
        adapter=SimpleNamespace(instructions='rules')
        diagnosis={'problem':'artifact'}
        clear={'preferred':'B','confidence':'high','evidence_valid':True,
               'critical_regression':False,'reason':'B is clearly cleaner'}
        with patch.object(models,'_paired_repair_content',return_value=[]), \
             patch.object(models,'judge_call',return_value=clear) as call:
            reviews=models.selection(config,adapter,diagnosis,[],[])
        self.assertEqual(len(reviews),1);self.assertEqual(call.call_count,1)
        ambiguous={**clear,'preferred':'tie','confidence':'low'}
        confirmed={**clear,'preferred':'A','confidence':'high'}
        with patch.object(models,'_paired_repair_content',return_value=[]), \
             patch.object(models,'judge_call',side_effect=[ambiguous,confirmed]) as call:
            reviews=models.selection(config,adapter,diagnosis,[],[])
        self.assertEqual(len(reviews),2);self.assertEqual(call.call_count,2)
        self.assertTrue(promotion(reviews)[0])
        with patch.object(models,'_paired_repair_content',return_value=[]), \
             patch.object(models,'judge_call',return_value=ambiguous) as call:
            reviews=models.selection(config,adapter,diagnosis,[],[],max_reviews=1)
        self.assertEqual(len(reviews),1);self.assertEqual(call.call_count,1)
        self.assertIn('without a fixed category priority',call.call_args.args[0])
        self.assertEqual(call.call_args.args[1]['matched_outcomes'],{'A':[],'B':[]})
        self.assertFalse(promotion(reviews)[0])

    def test_router_accepts_multiple_asset_owners(self):
        result={'scenario':'validation','decisions':[{'frame':0,'action':'wait','reason':'','image':'frame.png'}]}
        config={'quality':{'criteria':[{'id':'art','description':'art'}]}}
        plan={'assets':[{'id':'left'},{'id':'right'}]}
        answer={'coverage_passed':True,'coverage_reason':'shown','missing_evidence':[],
                'issues':[{'id':'pair','problem':'identities mismatch','route':'asset','asset_ids':['left','right'],
                           'source_observation':{'status':'defective','evidence':'both sources shown'},
                           'rendered_observation':{'status':'defective','evidence':'mismatch visible'},
                           'evidence':[{'scenario':'validation','frame':0,'observation':'different identities'}],
                           'rendered_acceptance':['rendered identities match']} ]}
        args=(config,SimpleNamespace(instructions='rules'),Path('/tmp/project'),plan,
              {'parameters':{}},{'scores':{'art':.5}},[result],[])
        with patch.object(models,'content',return_value=[]),patch.object(models,'material_content',return_value=[]),patch.object(models,'call',return_value=answer):
            self.assertEqual(models.route(*args)['issues'][0]['asset_ids'],['left','right'])

    def test_router_resolves_composition_issue_with_context_asset_ids(self):
        result={'scenario':'validation','decisions':[{'frame':0,'action':'wait','reason':'','image':'frame.png'}]}
        config={'quality':{'criteria':[{'id':'art','description':'art'}]}}
        plan={'assets':[{'id':'bird'}]}
        args=(config,SimpleNamespace(instructions='rules'),Path('/tmp/project'),plan,
              {'parameters':{}},{'scores':{'art':.5}},[result],[])
        for source_status,expected_route,expected_ids in (
                ('clean','composition',[]),('defective','both',['bird'])):
            with self.subTest(source_status=source_status):
                issue={'id':'motion','problem':'bird motion is wrong','route':'composition',
                       'asset_ids':['bird'],
                       'source_observation':{'status':source_status,'evidence':'source inspected'},
                       'rendered_observation':{'status':'defective','evidence':'bad motion'},
                       'evidence':[{'scenario':'validation','frame':0,'observation':'bad motion'}],
                       'rendered_acceptance':['bird motion is correct']}
                answer={'coverage_passed':True,'coverage_reason':'shown','missing_evidence':[],
                        'issues':[issue]}
                with patch.object(models,'content',return_value=[]), \
                     patch.object(models,'material_content',return_value=[]), \
                     patch.object(models,'call',return_value=answer):
                    normalized=models.route(*args)['issues'][0]
                self.assertEqual(normalized['route'],expected_route)
                self.assertEqual(normalized['asset_ids'],expected_ids)

    def test_parameter_edit_is_bounded_and_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);file=b/'visual.json';write_json(file,{'scale':1})
            spec={'parameters':{'scale':{'file':'visual.json','path':['scale'],'min':.8,'max':1.2,'type':'float'}}}
            for changes in [[{'parameter':'damage','value':0}],[{'parameter':'scale','value':9}],
                            [{'parameter':'scale','value':1.1},{'parameter':'bad','value':0}]]:
                with self.assertRaises(ValueError):apply_parameters(b,spec,changes)
                self.assertEqual(json.loads(file.read_text()),{'scale':1})

    def test_evidence_covers_current_episodes_and_late_frames(self):
        results=[{'scenario':f'policy{i}','decisions':[{'frame':j,'action':'wait','reason':'','image':f'{i}-{j}'} for j in range(10)]} for i in range(3)]
        frames=sample(results)
        self.assertEqual({f['scenario'] for f in frames},{'policy0','policy1','policy2'})
        self.assertTrue(all(any(f['frame']==9 and f['scenario']==f'policy{i}' for f in frames) for i in range(3)))

    def test_primary_score_cannot_be_traded_for_survival(self):
        c={'behavior':{'metrics':[{'id':'win','direction':'max'},{'id':'time','direction':'max','min_gain':.25}]}}
        self.assertFalse(better({'win':0,'time':99},{'win':1,'time':1},c))
        self.assertFalse(better({'win':1,'time':1.1},{'win':1,'time':1},c))


class AssetHistoryTests(unittest.TestCase):
    def test_distinct_assets_generate_concurrently_without_losing_shared_state(self):
        from gamecraft_assets.generate import generate
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            assets=[{'id':name,'kind':'sprite','size':[32,32],'frames':1,'fps':1,
                     'description':name,'prompt':name,'requirements':'clear'} for name in ('left','right')]
            plan={'schema_version':1,'task_id':'game','style':'flat','assets':assets}
            write_json(root/'asset_plan.json',plan)
            write_json(root/'asset_manifest.json',{'assets':{}})
            def process(g,video,item,dest):
                p=dest/'frame0000.png';p.write_bytes(item['id'].encode())
                return [p],[],{'coverage':.2,'in_frame':1,'bbox_fill':.3}
            verdict={'usable':.9,'identity':.9,'matte':.9,'failure':''}
            fake=SimpleNamespace(h3_t2va=lambda *args,**kwargs:None)
            with patch('gamecraft_assets.generate._backend',return_value=fake), \
                 patch('gamecraft_assets.generate._process',side_effect=process), \
                 patch('gamecraft_assets.generate._judge',return_value=verdict):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending=[pool.submit(generate,root,only=[name],rounds=1,candidates=1,
                                         validator=lambda plan:None) for name in ('left','right')]
                    self.assertEqual([job.result() for job in pending],[[],[]])
            manifest=json.loads((root/'asset_manifest.json').read_text())['assets']
            history=json.loads((root/'asset_history.json').read_text())
            self.assertEqual(set(manifest),{'left','right'})
            self.assertEqual({row['asset'] for row in history},{'left','right'})

    def test_package_generator_requests_four_asset_candidates(self):
        for count in (ASSET_REPAIR_CANDIDATES,2):
          with self.subTest(candidates=count),tempfile.TemporaryDirectory() as td:
            root=Path(td);package=root/'package';package.mkdir()
            plan={'schema_version':1,'task_id':'game','style':'flat','assets':[]}
            config={'_package_root':str(package),'assets':{}}
            generator=Generator(root/'generation',plan,config,'python',candidates=count)
            with patch('gamecraft_training.package.edits.subprocess.run') as invoke:
                out=Path(td)/'request';out.mkdir()
                write_json(out/'generation.json',{'status':'generated'})
                generator('object','fix it',out)
            request=json.loads((out/'request.json').read_text())
            self.assertEqual(request['candidates'],count)
            invoke.assert_called_once()

    def test_asset_worker_judges_four_and_selects_highest_usable(self):
        import sys
        from gamecraft_training.package import asset_worker
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            asset={'id':'object','kind':'sprite','size':[32,32],'frames':1,'fps':1,
                   'description':'an object','prompt':'one object','requirements':'clear'}
            plan={'schema_version':1,'task_id':'game','style':'flat','assets':[asset]}
            for name in ('base_plan.json','asset_plan.json'):write_json(root/name,plan)
            write_json(root/'asset_manifest.json',{'assets':{}})
            def process(g,video,item,dest):
                p=dest/'frame0000.png';p.write_bytes(dest.name.encode())
                return [p],[dest/'judge.png'],{'coverage':.2,'in_frame':1,'bbox_fill':.3}
            def judge(g,item,frames,ref=None):
                version=int(frames[0].parent.name[1:])
                return {'usable':{1:.32,2:.48,3:.41,4:.36}[version],
                        'identity':.9,'matte':.25,'failure':''}
            request=root/'request.json';result=root/'result.json'
            write_json(request,{'workspace':str(root),'asset':'object','problem':'improve it',
                                'result':str(result),'candidates':4,
                                'quality':{'min_usable':.7,'min_matte':.7}})
            fake=SimpleNamespace(h3_t2va=lambda *a,**k:None)
            with patch('gamecraft_assets.generate._backend',return_value=fake), \
                 patch('gamecraft_assets.generate._process',side_effect=process), \
                 patch('gamecraft_assets.generate._judge',side_effect=judge), \
                 patch('gamecraft_training.package.asset_worker.rewrite_prompt',return_value={
                     'repairable':True,'prompt':'one repaired object','reason':'fix','model':'test'}), \
                 patch.object(sys,'argv',['worker',str(request)]):
                asset_worker.main()
            output=json.loads(result.read_text())
            self.assertEqual(output['selection']['generated_versions'],[1,2,3,4])
            self.assertEqual(output['selection']['selected_version'],2)
            self.assertEqual(output['candidate']['verdict']['usable'],.48)

    def test_seed_history_survives_rejection_and_identical_frames_are_rejected(self):
        import sys
        from gamecraft_training.package import asset_worker
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            plan={'schema_version':1,'task_id':'other-game','style':'flat','assets':[
                {'id':'object','kind':'sprite','size':[32,32],'frames':1,'fps':1,
                 'description':'an object','prompt':'one object','requirements':'clear'}]}
            for name in ['base_plan.json','asset_plan.json']:write_json(root/name,plan)
            write_json(root/'asset_manifest.json',{'assets':{}})
            def process(g,video,asset,dest):
                p=dest/'frame0000.png';p.write_bytes(b'identical output')
                return [p],[],{'coverage':.2,'in_frame':1,'bbox_fill':.3}
            fake=SimpleNamespace(h3_t2va=lambda *args,**kwargs:None)
            verdict={'usable':.9,'identity':.9,'matte':1,'failure':''}
            with patch('gamecraft_assets.generate._backend',return_value=fake), \
                 patch('gamecraft_assets.generate._process',side_effect=process), \
                 patch('gamecraft_assets.generate._judge',return_value=verdict), \
                 patch('gamecraft_training.package.asset_worker.rewrite_prompt',return_value={
                     'repairable':True,'prompt':'one repaired object','reason':'fix','model':'test'}):
                for i in range(2):
                    request=root/f'request{i}.json'
                    write_json(request,{'workspace':str(root),'asset':'object','problem':'fix outline','result':str(root/f'result{i}.json')})
                    with patch.object(sys,'argv',['worker',str(request)]):asset_worker.main()
            first=json.loads((root/'result0.json').read_text());second=json.loads((root/'result1.json').read_text())
            self.assertEqual(first['status'],'generated')
            self.assertEqual(second['status'],'duplicate')
            self.assertNotEqual(first['candidate']['seed'],second['candidate']['seed'])
            self.assertEqual(len(json.loads((root/'requests.json').read_text())),2)
