import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from gamecraft_behavior.core import digest,write_json
from gamecraft_assets.plans import validate_generic
from gamecraft_training.package.boundary_planner import validate,plan as boundary_plan
from gamecraft_training.package.candidates import run_groups,run_round_bundle,bundle_plan,material_state
from gamecraft_training.package.material_tool import invoke as invoke_material_tool
from gamecraft_training.package.edits import Generator


def asset(ident='actor',scope='single_subject'):
    return {'id':ident,'kind':'animation','size':[16,16],'frames':2,'fps':12,
            'description':'motion','requirements':'readable motion','prompt':'A jumping character.',
            'reference_prompt':'A full body character.','reference_asset_id':'','loop':True,
            'generation_scope':scope,'motion':'loop','scope_reason':'coherent motion',
            'includes':['actor','rope'] if scope=='coupled_action' else ['actor'],
            'game_controlled':['input','score']}


def issue(ident='I1'):
    return {'id':ident,'problem':'rope misses the hand','route':'both','asset_ids':['actor'],
            'rendered_acceptance':['rope connected to hand'],'evidence':[]}


def proposal():
    return {'groups':[{'id':'rope','issue_ids':['I1'],'root_cause':'separate rope and body timing',
        'grouping_reason':'shared motion boundary','current_scope':'single_subject','affected_assets':['actor'],
        'required_controls':['input','score'],'escalation_reason':'joint video can synchronize motion',
        'skip_reason':'','candidates':[
            {'kind':'same_level','scope':'single_subject','video_controls':['body'],
             'code_controls':['rope','input','score'],'code_instructions':'same_level',
             'materials':[{'operation':'replace','asset':asset()}]},
            {'kind':'higher_level','scope':'coupled_action','video_controls':['body','rope'],
             'code_controls':['input','score'],'code_instructions':'higher_level',
             'materials':[{'operation':'add','asset':asset('actor_rope','coupled_action')}]}]}]}


class BoundaryCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.plan={'schema_version':1,'task_id':'test','style':'arcade','assets':[asset()]}
        self.plan['assets'][0].pop('reference_asset_id')
        self.diagnosis={'coverage_passed':True,'issues':[issue()]}

    def test_grouped_plan_covers_issues_and_has_real_new_scope(self):
        result=validate(proposal(),self.diagnosis,self.plan)
        self.assertTrue(result['grouped'])
        self.assertEqual(len(result['groups'][0]['candidates']),2)
        self.assertNotIn('reference_asset_id',result['groups'][0]['candidates'][1]['materials'][0]['asset'])

    def test_planner_normalizes_static_reference_but_keeps_moving_reference(self):
        p=proposal()
        still=asset('badge')
        still.update(kind='sprite',frames=1,fps=1,motion='static',
                     reference_asset_id='unused_static_reference',prompt='A complete badge illustration.')
        still.pop('loop')
        p['groups'][0]['candidates'][0]['materials']=[{'operation':'add','asset':still}]
        p['groups'][0]['candidates'][1]['materials'][0]['asset']['reference_asset_id']='actor'
        with self.assertRaisesRegex(ValueError,"Static asset 'badge' cannot use reference_asset_id"):
            validate_generic({**self.plan,'assets':[self.plan['assets'][0],still]})
        result=validate(p,self.diagnosis,self.plan)
        candidates=result['groups'][0]['candidates']
        self.assertNotIn('reference_asset_id',candidates[0]['materials'][0]['asset'])
        self.assertEqual(candidates[1]['materials'][0]['asset']['reference_asset_id'],'actor')
        self.assertEqual(result['normalizations'][0]['asset_id'],'badge')
        with tempfile.TemporaryDirectory() as td, \
             patch('gamecraft_training.package.models.content',return_value=[]), \
             patch('gamecraft_training.package.models.material_content',return_value=[]), \
             patch('gamecraft_training.package.models.call',return_value=p) as invoke:
            planned=boundary_plan({'assets':{'bindings':{}}},SimpleNamespace(instructions='rules'),
                                  td,self.plan,{},self.diagnosis,[],[])
        self.assertEqual(invoke.call_count,1)
        self.assertEqual(planned['planning_retries'],0)
        self.assertEqual(len(planned['groups']),1)

    def test_static_subject_can_gain_animation_without_expanding_spatial_scope(self):
        still={'id':'actor','kind':'sprite','size':[16,16],'frames':1,'fps':12,
               'description':'standing actor','requirements':'recognizable actor',
               'prompt':'One standing actor.','generation_scope':'single_subject',
               'motion':'static','scope_reason':'One independently controlled actor',
               'includes':['actor'],'game_controlled':['position','collision']}
        plan={**self.plan,'assets':[still]}
        moving=asset('actor_walk')
        moving.update(reference_asset_id='actor',reference_prompt='')
        p=proposal();group=p['groups'][0]
        group.update(root_cause='static actor slides during observed movement',
                     grouping_reason='missing internal motion',
                     escalation_reason='chronological frames show one unchanged pose while moving')
        group['candidates'][0].update(video_controls=['still actor'],
             code_controls=['position','collision'],
             materials=[{'operation':'replace','asset':copy.deepcopy(still)}])
        group['candidates'][1].update(scope='single_subject',video_controls=['walking pose'],
             code_controls=['position','collision'],
             code_instructions='Play actor_walk while moving and show idle actor when stopped.',
             materials=[{'operation':'add','asset':moving}])
        result=validate(p,self.diagnosis,plan)
        upgrade=result['groups'][0]['candidates'][1]
        self.assertEqual(upgrade['upgrade_axis'],'motion')
        bundled=bundle_plan(result)['groups'][0]['candidates'][1]
        self.assertEqual(bundled['upgrade_axes'],['motion'])
        self.assertEqual(bundled['repair_units'][0]['upgrade_axis'],'motion')
        updated,config=material_state(plan,{'assets':{'bindings':{'actor':['assets/actor.png']} }},
                                      bundled,'motion_test')
        self.assertEqual(len(updated['assets']),2)
        self.assertEqual(len(config['assets']['bindings']['actor_walk']),2)
        for mutate in (
            lambda x:x['materials'][0]['asset'].update(reference_asset_id='missing'),
            lambda x:x.update(materials=[]),
        ):
            bad=copy.deepcopy(p);mutate(bad['groups'][0]['candidates'][1])
            with self.assertRaises(ValueError):validate(bad,self.diagnosis,plan)
        bad=copy.deepcopy(p)
        bad['groups'][0]['candidates'][0]['materials']=[
            {'operation':'add','asset':copy.deepcopy(moving)}]
        with self.assertRaisesRegex(ValueError,'belongs in higher_level'):
            validate(bad,self.diagnosis,plan)

    def test_same_level_can_add_authored_content_variant_to_integrated_plan(self):
        p=proposal();group=p['groups'][0]
        group.update(affected_assets=[],escalation_reason='',candidates=group['candidates'][:1])
        variant=asset('actor_variant')
        variant.update(reference_asset_id='actor',reference_prompt='')
        group['candidates'][0].update(code_instructions='Use the new variant in a distinct playable encounter.',
                                      materials=[{'operation':'add','asset':variant}])
        result=validate(p,self.diagnosis,self.plan)
        bundled=bundle_plan(result)['groups'][0]['candidates'][0]
        self.assertEqual([item['asset']['id'] for item in bundled['materials']],['actor_variant'])
        self.assertIn('distinct playable encounter',bundled['code_instructions'])

    def test_invalid_ownership_and_escalation_fail_before_generation(self):
        for mutation in (
            lambda p:p['groups'][0].update(issue_ids=[]),
            lambda p:p['groups'][0].update(escalation_reason=''),
            lambda p:p['groups'][0]['candidates'][1].update(scope='single_subject'),
            lambda p:p['groups'][0]['candidates'][1].update(materials=[]),
            lambda p:p['groups'][0]['candidates'][0]['materials'][0]['asset'].update(frames=4),
            lambda p:p['groups'][0]['candidates'][1]['materials'][0]['asset'].update(reference_asset_id='missing'),
            lambda p:p['groups'][0]['candidates'][1].update(code_controls=['body','score']),
        ):
            p=proposal();mutation(p)
            with self.subTest(p=p),self.assertRaises(ValueError):validate(p,self.diagnosis,self.plan)

    def test_groups_cannot_compete_for_same_asset(self):
        p=proposal();other=copy.deepcopy(p['groups'][0]);other.update(id='other',issue_ids=['I2'])
        p['groups'].append(other)
        with self.assertRaisesRegex(ValueError,'share assets'):
            validate(p,{'issues':[issue(),issue('I2')]},self.plan)

    def test_round_allows_independent_scope_experiments(self):
        p=proposal();other=copy.deepcopy(p['groups'][0])
        other.update(id='other',issue_ids=['I2'],affected_assets=[])
        other['candidates'][0]['materials']=[]
        other['candidates'][1]['materials'][0]['asset']['id']='other_coupled'
        p['groups'].append(other)
        result=validate(p,{'issues':[issue(),issue('I2')]},self.plan)
        self.assertEqual(sum(len(g['candidates'])==2 for g in result['groups']),2)

    def test_bundle_combines_two_escalations_without_more_code_branches(self):
        p=proposal();other=copy.deepcopy(p['groups'][0])
        other.update(id='second',issue_ids=['I2'],affected_assets=[])
        other['candidates'][0]['materials']=[]
        other['candidates'][1]['materials'][0]['asset']['id']='second_coupled'
        p['groups'].append(other)
        bundled=bundle_plan(validate(p,{'issues':[issue(),issue('I2')]},self.plan))
        self.assertEqual(len(bundled['groups'][0]['candidates']),2)
        self.assertEqual(bundled['groups'][0]['escalated_groups'],['rope','second'])
        higher=bundled['groups'][0]['candidates'][1]
        self.assertEqual([u['scope'] for u in higher['repair_units']],
                         ['coupled_action','coupled_action'])
        self.assertEqual({m['asset']['id'] for m in higher['materials']},
                         {'actor_rope','second_coupled'})

    def test_joint_h3_reuses_identical_request_across_serial_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);jobs=[root/'a',root/'b',root/'c']
            item=asset();plan={'style':'arcade','assets':[item]}
            for job in jobs:
                (job/'generation').mkdir(parents=True)
                write_json(job/'generation/asset_plan.json',plan)
                write_json(job/'generation/asset_manifest.json',{'assets':{}})
                write_json(job/'material_tool.json',{
                    'asset_ids':['actor'],'bindings':{'actor':['assets/actor/0.png','assets/actor/1.png']},
                    'video_skill':False,'phase':'initialization','shared_cache':str(root/'shared')})
            calls=[]
            def fake_generate(workspace,**kwargs):
                calls.append(str(workspace))
                where=Path(workspace);paths=[]
                for i in range(2):
                    name=f'generated/actor/v0001/frame{i:04d}.png';target=where/name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(b'frame'+bytes([i]));paths.append(name)
                write_json(where/'asset_manifest.json',{'assets':{'actor':
                    {'version':1,'files':paths}}})
                return []
            with patch('gamecraft_training.package.material_tool.generate',side_effect=fake_generate), \
                 patch('gamecraft_training.package.material_tool.validate_generic'), \
                 patch('gamecraft_training.package.material_tool.validate_bound'):
                first=invoke_material_tool(jobs[0],'actor','same prompt')
                second=invoke_material_tool(jobs[1],'actor','same prompt')
                third=invoke_material_tool(jobs[2],'actor','different prompt')
            self.assertEqual(len(calls),2)
            self.assertFalse(first['reused']);self.assertTrue(second['reused'])
            self.assertFalse(third['reused'])
            self.assertEqual((jobs[0]/'project/assets/actor/0.png').read_bytes(),
                             (jobs[1]/'project/assets/actor/0.png').read_bytes())

    def test_planner_retries_overlapping_asset_groups_without_regenerating_evidence(self):
        invalid=proposal();other=copy.deepcopy(invalid['groups'][0]);other.update(id='other',issue_ids=['I2'])
        invalid['groups'].append(other)
        corrected=proposal();corrected['groups'][0]['issue_ids']=['I1','I2']
        diagnosis={'coverage_passed':True,'issues':[issue(),issue('I2')]}
        with tempfile.TemporaryDirectory() as td, \
             patch('gamecraft_training.package.models.content',return_value=['evidence']) as content, \
             patch('gamecraft_training.package.models.material_content',return_value=[]) as materials, \
             patch('gamecraft_training.package.models.call',side_effect=[invalid,corrected]) as invoke:
            result=boundary_plan({'assets':{'bindings':{}}},SimpleNamespace(instructions='rules'),
                td,self.plan,{},diagnosis,[],[])
        self.assertEqual(result['planning_retries'],1)
        self.assertEqual(result['groups'][0]['issue_ids'],['I1','I2'])
        self.assertEqual(invoke.call_count,2)
        self.assertIn('ONE integrated round plan',invoke.call_args_list[0].args[0])
        self.assertIn('same_level repair may add a material',invoke.call_args_list[0].args[0])
        self.assertEqual(invoke.call_args.args[2],[])
        self.assertIn('share assets',invoke.call_args.args[1]['validation_error'])

    def test_repeated_invalid_plan_skips_edits_instead_of_crashing_run(self):
        invalid=proposal();invalid['groups'][0]['issue_ids']=[]
        with tempfile.TemporaryDirectory() as td, \
             patch('gamecraft_training.package.models.content',return_value=[]), \
             patch('gamecraft_training.package.models.material_content',return_value=[]), \
             patch('gamecraft_training.package.models.call',return_value=invalid) as invoke:
            result=boundary_plan({'assets':{'bindings':{}}},SimpleNamespace(instructions='rules'),
                td,self.plan,{},self.diagnosis,[],[])
        self.assertEqual(invoke.call_count,3)
        self.assertEqual(result['groups'],[])
        self.assertTrue(result['planning_errors'])

    def test_planner_can_replace_previously_protected_material(self):
        result=validate(proposal(),self.diagnosis,self.plan,['assets/actor'],{'actor':['assets/actor/0.png']})
        self.assertEqual(len(result['groups']),1)

    def test_replacement_self_reference_uses_frozen_incumbent(self):
        p=proposal()
        p['groups'][0]['candidates'][0]['materials'][0]['asset']['reference_asset_id']='actor'
        result=validate(p,self.diagnosis,self.plan)
        generated=result['groups'][0]['candidates'][0]['materials'][0]['asset']
        self.assertEqual(generated['reference_asset_id'],'actor')
        self.assertTrue(generated['incumbent_reference'])

    def test_new_material_cannot_reference_its_own_output(self):
        p=proposal()
        p['groups'][0]['candidates'][1]['materials'][0]['asset']['reference_asset_id']='actor_rope'
        with self.assertRaisesRegex(ValueError,'Reference must exist in incumbent'):
            validate(p,self.diagnosis,self.plan)

    def test_round_bundle_keeps_distinct_causes_but_substitutes_one_scope(self):
        p=proposal()
        p['groups'].insert(0,{'id':'menu','issue_ids':['I2'],'root_cause':'menu contrast',
            'required_controls':['menu_confirm'],'skip_reason':'','candidates':[
                {'kind':'same_level','scope':'single_subject','video_controls':[],
                 'code_controls':['menu_confirm'],'code_instructions':'brighten the opponent card',
                 'materials':[]}]})
        diagnosis={'issues':[issue(),issue('I2')]}
        planned=bundle_plan({**diagnosis,**p,'round_bundle':True})
        self.assertEqual(len(planned['groups']),1)
        bundled=planned['groups'][0]
        self.assertEqual(bundled['issue_ids'],['I2','I1'])
        self.assertEqual(len(bundled['candidates']),2)
        for candidate in bundled['candidates']:
            self.assertEqual([u['id'] for u in candidate['repair_units']],['menu','rope'])
            self.assertIn('brighten the opponent card',candidate['code_instructions'])
        self.assertEqual(bundled['candidates'][0]['repair_units'][1]['scope'],'single_subject')
        self.assertEqual(bundled['candidates'][1]['repair_units'][1]['scope'],'coupled_action')
        self.assertEqual([m['asset']['id'] for m in bundled['candidates'][1]['materials']],['actor_rope'])

    def test_round_bundle_edits_and_replays_multiple_issues_once(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);baseline=root/'baseline';baseline.mkdir()
            (baseline/'project.godot').write_text('config_version=5')
            (baseline/'main.gd').write_text('original')
            groups=[]
            for ident in ('menu','fight'):
                groups.append({'id':ident,'issue_ids':[ident],'root_cause':ident,
                    'required_controls':[ident+'_input'],'skip_reason':'','candidates':[
                        {'kind':'same_level','scope':'single_subject','video_controls':[],
                         'code_controls':[ident+'_input'],'code_instructions':'fix '+ident,
                         'materials':[]}]})
            repair={'issues':[{'id':ident,'problem':'broken '+ident,'rendered_acceptance':['fixed '+ident]}
                              for ident in ('menu','fight')], 'groups':groups,'round_bundle':True}
            calls={'edit':0,'replay':0,'selection':0}
            def editor(project,target,out,**kwargs):
                calls['edit']+=1
                d=kwargs['diagnosis']
                self.assertEqual([u['id'] for u in d['boundary_plan']['repair_units']],['menu','fight'])
                self.assertIn('fix menu',d['code_instructions'])
                self.assertIn('fix fight',d['code_instructions'])
                shutil.copytree(project,target)
                (target/'main.gd').write_text('both fixed')
                return {'changed_files':['main.gd']}
            editor.supports_material_tools=False
            def replay(project,runs,out,**kwargs):
                calls['replay']+=1
                return [{'variant':(project/'main.gd').read_text()}]
            def selection(cfg,adapter,diagnosis,before,after,**kwargs):
                calls['selection']+=1
                self.assertEqual([i['id'] for i in diagnosis['issues']],['menu','fight'])
                return [{'preferred_role':'candidate','evidence_valid':True,
                         'confidence':'high','critical_regression':False}]
            result=run_round_bundle(repair_plan=repair,project=baseline,plan={'assets':[]},
                config={'assets':{'bindings':{}},'_profile':{'name':'initialization','submit_global_video_skill':False}},
                generator=SimpleNamespace(root=root/'unused'),directory=root/'groups',adapter=SimpleNamespace(task_id='test'),
                evaluation={},episodes=[{'variant':'original'}],history=[],services=SimpleNamespace(selection=selection),
                code_editor=editor,edit_policy={'protected':[]},generator_factory=None,asset_python='unused',
                asset_candidates=1,video_skill=False,replay_same=replay)
            self.assertEqual(calls,{'edit':1,'replay':1,'selection':1})
            self.assertEqual(result['groups'][0]['winner'],'same_level')
            self.assertEqual((result['project']/'main.gd').read_text(),'both fixed')
            self.assertEqual((baseline/'main.gd').read_text(),'original')

    def exercise(self, td, votes=('candidate','candidate','candidate'),fail_high=False,
                 video_skill=False,joint=False,editor_barrier=None,own_trajectories=False):
        root=Path(td);baseline=root/'baseline';baseline.mkdir()
        (baseline/'main.gd').write_text('original')
        (baseline/'project.godot').write_text('config_version=5')
        (baseline/'assets/actor').mkdir(parents=True)
        for i in range(2):
            im=Image.new('RGBA',(16,16));im.putpixel((8,8),(255,0,0,255));im.save(baseline/f'assets/actor/{i}.png')
        config={'assets':{'bindings':{'actor':[f'assets/actor/{i}.png' for i in range(2)]}},
                '_package_root':str(root),'_profile':{'name':'initialization','submit_global_video_skill':False}}
        original=digest(baseline);seen=[];calls=[]
        class FakeGenerator:
            def __init__(self,where,plan,cfg,*args):
                self.root=Path(where);self.root.mkdir();self.plan=plan
                write_json(self.root/'asset_plan.json',plan)
                write_json(self.root/'asset_manifest.json',{'assets':{}})
                if video_skill:
                    from gamecraft_video_skill import initialize
                    initialize(self.root)
                seen.append((self.root,cfg))
            def __call__(self,aid,problem,out):
                out=Path(out);out.mkdir()
                if fail_high and aid=='actor_rope':return {'status':'rejected','reason':'bad alpha'}
                item=next(a for a in self.plan['assets'] if a['id']==aid)
                paths=[]
                for i in range(item['frames']):
                    path=self.root/f'{aid}_{i}.png'
                    im=Image.new('RGBA',item['size']);im.putpixel((4,4),(0,255,0,255));im.save(path)
                    paths.append(path.name)
                manifest=json.loads((self.root/'asset_manifest.json').read_text())
                manifest['assets'][aid]={'files':paths};write_json(self.root/'asset_manifest.json',manifest)
                return {'status':'generated'}
        def editor(project,target,out,**kwargs):
            if editor_barrier is not None:editor_barrier.wait(timeout=3)
            extra={}
            if joint:
                from gamecraft_training.package.candidates import install
                ctx=kwargs['material_execution']
                gen=FakeGenerator(out/'tool-generation',ctx['plan'],ctx['config'])
                records=[]
                for e in ctx['option']['materials']:
                    aid=e['asset']['id']
                    records.append({'asset':aid,**gen(aid,{},out/aid)})
                install(project,out/'prepared',gen,[r['asset'] for r in records],ctx['plan'],ctx['config'],[])
                project=out/'prepared'
                kwargs['diagnosis']['generated_materials']=[{'files':ctx['config']['assets']['bindings'][r['asset']]} for r in records]
                extra={'generations':records,'material_plan':ctx['plan'],'generation_workspace':str(gen.root)}
            self.assertEqual((project/'main.gd').read_text(),'original')
            d=kwargs['diagnosis'];kind=d['code_instructions']
            self.assertIn('generated_materials',d)
            for m in d['generated_materials']:
                self.assertTrue(all((project/f).exists() for f in m['files']))
            shutil.copytree(project,target);(target/'main.gd').write_text(kind)
            return {'changed_files':['main.gd'],**extra}
        editor.supports_material_tools=joint
        baseline_runs=[{'variant':'original','decisions':[{'action':'left'},{'action':'right'}]}]
        def replay(project,runs,out,**kwargs):
            self.assertEqual(runs,baseline_runs)
            self.assertFalse(kwargs['preserve_state'])
            self.assertEqual(digest(baseline),original)
            return [{'variant':(project/'main.gd').read_text(),'decisions':copy.deepcopy(runs[0]['decisions'])}]
        def candidate_replay(project,runs,out,**kwargs):
            self.assertEqual(runs,baseline_runs)
            self.assertIn('changes',kwargs)
            kind=(project/'main.gd').read_text()
            return [{'variant':kind,'decisions':[{'action':kind}],
                     'scripted_case':{'id':'fight','actions':[kind]}}]
        votes=list(votes)
        def selection(cfg,adapter,d,left,right,**kw):
            vote=votes[min(len(calls),len(votes)-1)]
            calls.append((left[0]['variant'],right[0]['variant']))
            if vote=='error':raise RuntimeError('Judge unavailable')
            return [{'preferred_role':vote,'evidence_valid':True,
                     'confidence':'high','critical_regression':False}]
        if video_skill:
            from gamecraft_video_skill import initialize
            initialize(root/'unused')
            write_json(root/'unused/video_skill/local.json',{'schema_version':1,'rules':[
                {'id':'retained','applies_when':{},'instruction':'Keep identity consistent.'}]})
        result=run_groups(repair_plan=validate(proposal(),self.diagnosis,self.plan),project=baseline,
            plan=self.plan,config=config,generator=SimpleNamespace(root=root/'unused'),directory=root/'groups',
            adapter=SimpleNamespace(task_id='test'),evaluation={},episodes=baseline_runs,history=[],
            services=SimpleNamespace(selection=selection),
            code_editor=editor,edit_policy={'protected':[]},
            generator_factory=FakeGenerator,asset_python='unused',asset_candidates=1,video_skill=video_skill,replay_same=replay,
            replay_candidate=candidate_replay if own_trajectories else None)
        self.assertEqual(digest(baseline),original)
        self.assertNotIn('actor_rope',config['assets']['bindings'])
        self.assertEqual(len(seen),2)
        self.assertNotEqual(seen[0][0],seen[1][0])
        return result,calls,root

    def test_selected_group_inherits_only_winners_own_trajectory(self):
        for votes,expected in ((('candidate','candidate'),'higher_level'),
                               (('candidate','incumbent'),'same_level'),
                               (('incumbent','incumbent'),None)):
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as td:
                result,_,_=self.exercise(td,votes=votes,own_trajectories=True)
                episode=result['episodes'][0]
                if expected:
                    self.assertEqual(episode['scripted_case']['actions'],[expected])
                else:
                    self.assertNotIn('scripted_case',episode)
                    self.assertEqual(episode['decisions'],[{'action':'left'},{'action':'right'}])

    def test_branch_inherits_existing_video_skill_without_directory_collision(self):
        with tempfile.TemporaryDirectory() as td:
            result,_,_=self.exercise(td,video_skill=True)
            skill=json.loads((result['generator'].root/'video_skill/local.json').read_text())
            self.assertEqual(skill['rules'][0]['id'],'retained')
            self.assertEqual(result['groups'][0]['winner'],'higher_level')

    def test_sibling_executors_are_parallel_and_tie_prefers_same_level(self):
        with tempfile.TemporaryDirectory() as td:
            result,_,_=self.exercise(td,joint=True,votes=('candidate','tie'),
                                     editor_barrier=Barrier(2))
            self.assertEqual(result['groups'][0]['winner'],'same_level')
            self.assertEqual(result['groups'][0]['candidate_parallelism'],2)

    def test_grouped_runner_delegates_generation_to_joint_executor(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,_=self.exercise(td,joint=True)
            self.assertEqual(result['groups'][0]['winner'],'higher_level')
            self.assertEqual(len(calls),2)
            self.assertIn('actor_rope',result['config']['assets']['bindings'])

    def test_both_variants_start_from_incumbent_and_winner_promotes_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,root=self.exercise(td)
            self.assertEqual(calls,[('original','same_level'),('same_level','higher_level')])
            self.assertEqual(result['groups'][0]['winner'],'higher_level')
            self.assertIn('actor_rope',result['config']['assets']['bindings'])
            self.assertEqual((result['project']/'main.gd').read_text(),'higher_level')
            self.assertTrue((root/'accepted_asset_plan.json').exists())

    def test_tie_prefers_same_level_and_does_not_promote_losing_assets(self):
        with tempfile.TemporaryDirectory() as td:
            result,_,_=self.exercise(td,votes=('candidate','tie'))
            self.assertEqual(result['groups'][0]['winner'],'same_level')
            self.assertNotIn('actor_rope',result['config']['assets']['bindings'])

    def test_second_candidate_compares_with_original_when_first_loses(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,_=self.exercise(td,votes=('incumbent','candidate'))
            self.assertEqual(calls,[('original','same_level'),('original','higher_level')])
            self.assertEqual(result['groups'][0]['winner'],'higher_level')

    def test_judge_failure_retains_current_winner_and_considers_next_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,_=self.exercise(td,votes=('error','candidate'))
            self.assertEqual(calls,[('original','same_level'),('original','higher_level')])
            self.assertEqual(result['groups'][0]['winner'],'higher_level')
            self.assertIn('error',result['groups'][0]['selection_reviews'][0])

    def test_failed_h3_branch_does_not_block_other_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,_=self.exercise(td,votes=('candidate',),fail_high=True)
            self.assertEqual(result['groups'][0]['winner'],'same_level')
            self.assertEqual(len(calls),1)
            self.assertEqual(result['groups'][0]['candidates'][1]['action'],'error')

    def test_both_rejected_keep_original_code_and_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            result,calls,_=self.exercise(td,votes=('incumbent','incumbent'))
            self.assertEqual(len(calls),2)
            self.assertEqual(result['groups'][0]['winner'],'incumbent')
            self.assertEqual((result['project']/'main.gd').read_text(),'original')
            self.assertNotIn('actor_rope',result['config']['assets']['bindings'])

    def test_reference_image_is_from_current_incumbent_not_initial_package(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'project';project.mkdir()
            image=Image.new('RGBA',(16,16),(255,0,0,255));image.save(project/'identity.png')
            identity={'id':'identity','kind':'sprite','size':[16,16],'frames':1,'fps':1,
                      'description':'identity','requirements':'identity','prompt':'identity'}
            animated=asset();animated['reference_asset_id']='identity'
            plan={**self.plan,'assets':[identity,animated]}
            cfg={'_reference_project':str(project),'_package_root':str(root/'nonexistent'),
                 'assets':{'bindings':{'identity':['identity.png'],'actor':['a.png','b.png']}}}
            gen=Generator(root/'generation',plan,cfg,'unused')
            self.assertEqual((gen.root/'baseline_references/identity/frame0000.png').read_bytes(),
                             (project/'identity.png').read_bytes())

    def test_replacement_reference_survives_prompt_revision(self):
        from gamecraft_assets.generate import _existing_reference
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);project=root/'project';project.mkdir()
            image=Image.new('RGBA',(16,16),(255,0,0,255))
            image.save(project/'actor.png')
            original=asset();original.pop('reference_asset_id')
            replacement={**original,'prompt':'New action.','reference_asset_id':'actor',
                         'incumbent_reference':True}
            plan={**self.plan,'assets':[replacement]}
            cfg={'_reference_project':str(project),
                 'assets':{'bindings':{'actor':['actor.png']}}}
            gen=Generator(root/'generation',plan,cfg,'unused')
            manifest=json.loads((gen.root/'asset_manifest.json').read_text())
            replacement['prompt']='Revised action.'
            replacement['revision']=1
            dest=root/'request';dest.mkdir()
            reference=_existing_reference(gen.root,plan,manifest,replacement,dest)
            with Image.open(reference) as actual:
                self.assertEqual(actual.getpixel((384,384))[:3],(255,0,0))


if __name__=='__main__':unittest.main()
