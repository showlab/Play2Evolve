import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_assets.generate import generate
from gamecraft_assets.plans import write_json, validate_generic
from gamecraft_video_skill import (initialize, resolve, commit_patch, record_patch,
    initialize_global_store, submit_global_candidate, merge_global_candidates,
    record_global_validation, promote_global_candidates, propose_patch)


class VideoSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.asset={'id':'fighter','kind':'animation','size':[32,32],'frames':2,'fps':2,
                    'description':'one fighter','prompt':'perform one punch','requirements':'one readable punch',
                    'reference_prompt':'Full body {desc} on green','loop':True}
        self.plan={'schema_version':1,'task_id':'test-game','style':'flat arcade art','assets':[self.asset]}

    def test_default_resolver_starts_without_preseeded_rules(self):
        self.assertFalse((self.root/'video_skill').exists())
        initialize(self.root)
        recipe=resolve(self.root,self.asset,self.plan['style'],phase='initialization')
        self.assertEqual(recipe['method'],'fl2va')
        self.assertEqual(recipe['reference_policy'],'first_and_last')
        self.assertEqual(recipe['applied_rules'], [])
        self.assertIn('perform one punch',recipe['prompt'])
        self.assertEqual(recipe['output_contract']['representation'],'isolated_sequence')

    def test_asset_specific_patch_matches_plan_id_and_only_that_asset(self):
        initialize(self.root)
        rule={'id':'exact_fighter','applies_when':{'kind':'animation','asset_id':'fighter'},
              'instruction':'Keep the subject stable and fully visible.','source':'codex'}
        fighter=resolve(self.root,self.asset,self.plan['style'],phase='rsi',extra_rules=[rule])
        other=resolve(self.root,{**self.asset,'id':'other'},self.plan['style'],phase='rsi',extra_rules=[rule])
        self.assertEqual(fighter['applied_rules'],['exact_fighter'])
        self.assertIn(rule['instruction'],fighter['prompt'])
        self.assertEqual(other['applied_rules'],[])

    def test_skill_rule_can_target_generation_scope_and_motion(self):
        initialize(self.root)
        rule={'id':'moving_scene','applies_when':{'generation_scope':'scene','motion':'loop'},
              'instruction':'Keep the camera locked while the ambience moves.','source':'codex'}
        scene={**self.asset,'id':'haze','kind':'background','frames':4,
               'generation_scope':'scene','motion':'loop'}
        scene.pop('reference_prompt');scene.pop('loop')
        moving=resolve(self.root,scene,'painted sports illustration',phase='initialization',extra_rules=[rule])
        still=resolve(self.root,{**scene,'frames':1,'motion':'static'},
                      'painted sports illustration',phase='initialization',extra_rules=[rule])
        self.assertEqual(moving['applied_rules'],['moving_scene'])
        self.assertEqual(still['applied_rules'],[])

    def test_contract_compiler_keeps_background_opaque_and_foreground_extractable(self):
        initialize(self.root)
        background={**self.asset,'id':'arena','kind':'background','frames':1}
        background.pop('reference_prompt');background.pop('loop')
        bg=resolve(self.root,background,'painted sports illustration',phase='initialization')
        self.assertEqual(bg['output_contract']['representation'],'opaque_scene')
        self.assertFalse(bg['postprocess']['chroma_key'])
        self.assertNotIn('pure green',bg['prompt'].lower())
        sprite={**background,'id':'bag','kind':'sprite'}
        fg=resolve(self.root,sprite,'painted sports illustration',phase='initialization')
        self.assertEqual(fg['output_contract']['representation'],'isolated_cutout')
        self.assertTrue(fg['postprocess']['chroma_key'])
        self.assertIn('pure green',fg['prompt'].lower())

    def test_patch_commit_and_rejection_are_separate(self):
        initialize(self.root)
        patch_rule={'id':'learned_test','applies_when':{'kind':'animation','loop':True},
                    'instruction':'Perform one action and recover smoothly.','source':'optimizer'}
        record_patch(self.root,patch_rule,status='rejected',evidence={'reason':'no gain'})
        self.assertEqual(json.loads((self.root/'video_skill/local.json').read_text())['rules'],[])
        commit_patch(self.root,patch_rule,evidence={'gain':.2})
        local=json.loads((self.root/'video_skill/local.json').read_text())
        self.assertEqual([r['id'] for r in local['rules']],['learned_test'])
        rows=[json.loads(x) for x in (self.root/'video_skill/evidence.jsonl').read_text().splitlines()]
        self.assertEqual([r['status'] for r in rows],['rejected','accepted'])

    def test_patch_proposer_is_codex_and_rejects_composition_control(self):
        calls=[]
        class LLM:
            @staticmethod
            def call(*args,**kwargs):
                calls.append((args,kwargs))
                return {'actionable':True,
                        'instruction':'Keep the full subject visible throughout one continuous motion.',
                        'reason':'The source motion was cropped.'}
        rule=propose_patch(SimpleNamespace(llm=LLM()),self.asset,['subject is cropped'])
        self.assertEqual(calls[0][0][0],'gpt-6-sol')
        self.assertEqual(calls[0][1]['backend'],'codex')
        self.assertEqual(rule['source'],'codex')
        self.assertEqual(rule['applies_when']['asset_id'],'fighter')
        self.assertEqual(rule['evidence']['proposer'],{'backend':'codex','model':'gpt-6-sol'})

        class CompositionLLM:
            @staticmethod
            def call(*args,**kwargs):
                return {'actionable':True,
                        'instruction':'Trigger damage when the opponent is contacted.',
                        'reason':'Coupled runtime repair.'}
        with self.assertRaisesRegex(ValueError,'non-asset control'):
            propose_patch(SimpleNamespace(llm=CompositionLLM()),self.asset,['damage is early'])

        class NotActionableLLM:
            @staticmethod
            def call(*args,**kwargs):
                return {'actionable':False,'instruction':'','reason':'Requires actor placement.'}
        self.assertIsNone(propose_patch(SimpleNamespace(llm=NotActionableLLM()),self.asset,
                                        ['actors are too far apart']))

    def test_global_rule_requires_semantic_merge_and_independent_ab_validation(self):
        store=self.root/'global-store';initialize_global_store(store)
        patch_rule={'id':'learned_test_a','applies_when':{'kind':'animation','loop':True},
                    'instruction':'Keep the camera and background locked during one complete subject motion.',
                    'source':'optimizer','evidence':{}}
        submit_global_candidate(store,patch_rule,task_id='boxing')
        paraphrase={**patch_rule,'id':'learned_test_b',
                    'instruction':'Use a fixed view while the subject completes one continuous action.'}
        submit_global_candidate(store,paraphrase,task_id='platformer')
        def merge_call(model,instructions,data,schema):
            self.assertEqual(model,'gpt-6-sol')
            ids=[x['evidence_id'] for x in data['accepted_local_evidence']]
            return {'candidates':[{'source_evidence_ids':ids,
                'instruction':'Keep a fixed camera and stable background through one continuous subject motion.',
                'creator_guidance':'Use this constraint when subject motion shows camera drift or unstable framing.',
                'reason':'Two games show the same source-video failure family.'}]}
        pending=merge_global_candidates(store,caller=merge_call)
        self.assertEqual(len(pending),1)
        self.assertEqual(promote_global_candidates(store),[])
        with self.assertRaisesRegex(ValueError,'independent'):
            record_global_validation(store,pending[0],task_id='boxing',same_seed=True,
                                     improved=True,no_new_defects=True)
        record_global_validation(store,pending[0],task_id='runner-heldout',same_seed=True,
                                 improved=True,no_new_defects=True,
                                 evidence={'baseline':.4,'candidate':.7})
        self.assertEqual(promote_global_candidates(store),pending)
        skill=json.loads((store/'global.json').read_text())
        self.assertEqual(skill['rules'][0]['evidence']['source_games'],['boxing','platformer'])
        self.assertEqual(skill['rules'][0]['evidence']['validation_games'],['runner-heldout'])

    def test_global_merge_needs_two_games_and_does_not_repeat_consumed_evidence(self):
        store=self.root/'global-store';initialize_global_store(store)
        rule={'id':'one','applies_when':{'kind':'sprite'},
              'instruction':'Keep a clean centered silhouette.','source':'optimizer'}
        submit_global_candidate(store,rule,task_id='one-game')
        self.assertEqual(merge_global_candidates(store,caller=lambda *a: self.fail('must not call')),[])

    def test_promoted_creator_guidance_is_given_to_later_local_proposer(self):
        initialize(self.root)
        global_skill={'schema_version':1,'rules':[{
            'id':'global_camera','applies_when':{'kind':'animation','loop':True},
            'instruction':'Keep a stable view throughout the motion.','source':'codex_merge',
            'evidence':{'creator_guidance':'Use a stable-view constraint when framing drifts.'}}]}
        write_json(self.root/'video_skill/global.json',global_skill)
        seen=[]
        class LLM:
            @staticmethod
            def call(*args,**kwargs):
                seen.append(json.loads(args[2][0]['text']))
                return {'actionable':False,'instruction':'','reason':'No H3-only repair.'}
        self.assertIsNone(propose_patch(SimpleNamespace(llm=LLM()),self.asset,['cropped'],workspace=self.root))
        self.assertEqual(seen[0]['validated_global_creator_guidance'],
                         ['Use a stable-view constraint when framing drifts.'])

    def test_old_candidate_ledger_is_imported_once(self):
        store=self.root/'legacy';store.mkdir()
        write_json(store/'global.json',{'schema_version':1,'rules':[]})
        rule={'id':'old','applies_when':{'kind':'sprite'},
              'instruction':'Keep one clean silhouette.','source':'optimizer'}
        (store/'candidates.jsonl').write_text(json.dumps(
            {'created_at':1,'task_id':'old-game','patch':rule,'evidence':{}})+'\n')
        initialize_global_store(store);initialize_global_store(store)
        rows=(store/'local_evidence.jsonl').read_text().splitlines()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0])['migrated_from'],'candidates.jsonl')

    def test_generate_off_is_legacy_and_on_records_recipe(self):
        def setup(path):
            write_json(path/'asset_plan.json',self.plan)
            write_json(path/'asset_manifest.json',{'assets':{}})
        verdict={'usable':.9,'identity':.9,'matte':.9,'failure':''}
        fake=SimpleNamespace(h3_t2va=lambda *a,**k:None,h3_video=lambda *a,**k:None,
                             frames_at=lambda *a,**k:[],key=lambda x:x,fit_ref=lambda *a:None,REF_FIT=(.5,.45))
        # Use a sprite here so the test does not need a generated identity reference.
        self.asset.update(kind='sprite',frames=1,loop=False);self.asset.pop('reference_prompt')
        for enabled,name in [(False,'legacy'),(True,'skill')]:
            root=self.root/name;root.mkdir();setup(root)
            with patch('gamecraft_assets.generate._backend',return_value=fake), \
                 patch('gamecraft_assets.generate._process',side_effect=lambda g,v,a,d:([d/'frame0000.png'],[],{'coverage':.2,'in_frame':1,'bbox_fill':.3})), \
                 patch('gamecraft_assets.generate._judge',return_value=verdict):
                # _process is mocked; create the path consumed only as metadata.
                self.assertEqual(generate(root,rounds=1,candidates=1,video_skill=enabled,validator=validate_generic),[])
            version=next((root/'generated/fighter').glob('v*'))
            self.assertEqual((version/'recipe.json').exists(),enabled)
            self.assertEqual((version/'h3_request.json').exists(),enabled)
            self.assertEqual((root/'video_skill').exists(),enabled)

    def test_rsi_skill_mode_rewrites_prompt_and_returns_patch(self):
        from gamecraft_training.package import asset_worker
        self.asset.update(kind='sprite',frames=1,loop=False);self.asset.pop('reference_prompt')
        for name in ('asset_plan.json','base_plan.json'):write_json(self.root/name,self.plan)
        write_json(self.root/'asset_manifest.json',{'assets':{}})
        class LLM:
            @staticmethod
            def call(*args,**kwargs):
                return {'actionable':True,'instruction':'Keep one clean silhouette with stable framing.',
                        'reason':'fix outline'}
        fake=SimpleNamespace(h3_t2va=lambda *a,**k:None,llm=LLM())
        verdict={'usable':.9,'identity':.9,'matte':.9,'failure':''}
        def process(g,video,asset,dest):
            p=dest/'frame0000.png';p.write_bytes(b'candidate')
            return [p],[],{'coverage':.2,'in_frame':1,'bbox_fill':.3}
        request=self.root/'request.json';result=self.root/'result.json'
        write_json(request,{'workspace':str(self.root),'asset':'fighter','problem':'outline flickers',
                            'result':str(result),'video_skill':True})
        with patch('gamecraft_assets.generate._backend',return_value=fake), \
             patch('gamecraft_assets.generate._process',side_effect=process), \
             patch('gamecraft_assets.generate._judge',return_value=verdict), \
             patch('gamecraft_training.package.asset_worker.rewrite_prompt',return_value={
                 'repairable':True,'prompt':'a clean complete punch animation',
                 'reason':'replace flawed prompt','model':'test'}), \
             patch.object(sys,'argv',['worker',str(request)]):
            asset_worker.main()
        self.assertEqual(json.loads((self.root/'asset_plan.json').read_text())['assets'][0]['prompt'],
                         'a clean complete punch animation')
        self.assertEqual(json.loads((self.root/'base_plan.json').read_text())['assets'][0]['prompt'],
                         'perform one punch')
        output=json.loads(result.read_text())
        self.assertEqual(output['status'],'generated')
        self.assertIn('video_skill_patch',output)
        self.assertEqual(json.loads((self.root/'video_skill/local.json').read_text())['rules'],[])


if __name__=='__main__':unittest.main()
