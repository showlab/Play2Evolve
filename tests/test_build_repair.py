"""The opt-in complete-build flow repairs an incumbent without touching the package."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from gamecraft_training.package import build_repair, models, upgrade
from gamecraft_training.package.loop import run as package_run


def goal():
    return {'goal': 'Coordinated lane defense with animated plants',
            'upgrade_entry_id': 'train', 'upgrade_entry_mode': 'scenario',
            'core_playtest_goal': 'defend original lane',
            'upgrade_playtest_goal': 'defend several lanes',
            'gameplay_plan': {'change': 'overlapping lanes'},
            'aesthetic_plan': {'change': 'plant motion',
                               'h3_material_targets': ['shooter animation']},
            'milestones': ['inspect play', 'inspect materials', 'inspect regressions']}


def options():
    return {'reason': 'implement goal', 'scope': 'single_subject',
            'code_instructions': 'integrate playable game', 'video_controls': [],
            'code_controls': ['play'], 'materials': []}


def arguments(root, services):
    original=root/'original'
    original.mkdir()
    (original/'project.godot').write_text('original')
    out=root/'run'
    out.mkdir()
    return dict(output=out, original=original, adapter=SimpleNamespace(),
                config={'assets':{'bindings':{}}}, plan={'assets':[]},
                composition={}, generator=SimpleNamespace(root=root), baseline=[],
                skill='', rounds=3, goal=goal(),
                tasks=[{'profile':'core','max_decisions':100},
                       {'profile':'upgrade','max_decisions':100}],
                player_backend='codex', player_model='gpt-6-sol', godot='godot',
                rollout_fn=None, code_editor=None, edit_policy={}, asset_python='python',
                video_skill=False, step_frames=6, services=services)


class BuildRepairTests(TestCase):
    def test_vector_material_is_described_without_pil_preview(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'assets').mkdir()
            (root/'assets/dungeon_mark.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            config={'assets':{'bindings':{'dungeon_mark':['assets/dungeon_mark.svg']}}}
            plan={'assets':[{'id':'dungeon_mark','kind':'sprite'}]}
            with patch.dict('sys.modules', {'llm':SimpleNamespace(img=lambda *args, **kwargs: None)}):
                rows=models.material_content(config,root,plan)
            self.assertEqual(len(rows),1)
            self.assertIn('vector; inspect rendered game frames', rows[0]['text'])

    def test_upgrade_dispatch_is_opt_in(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            args=arguments(root,SimpleNamespace())
            with (patch.object(upgrade,'_plan_goal_evidence',return_value=goal()),
                  patch.object(build_repair,'run',return_value={'state':'completed'}) as worker):
                result=upgrade.run_upgrade(
                    package=root, output=args['output'], project=args['original'],
                    adapter=args['adapter'], cases=[], config=args['config'],
                    plan=args['plan'], composition=args['composition'],
                    generator=args['generator'], baseline=args['baseline'],
                    skill='', rounds=3,
                    selected_entry={'entry_id':'train','entry_mode':'scenario'},
                    run_objective=None, player_backend='codex', player_model='gpt-6-sol',
                    godot='godot', rollout_fn=None, code_editor=None, edit_policy={},
                    asset_python='python', video_skill=False, step_frames=6,
                    max_decisions=40, planning_mode='evidence',
                    execution_mode='build_repair')
            self.assertEqual(result['state'],'completed')
            self.assertEqual(worker.call_count,1)
            self.assertEqual(worker.call_args.kwargs['rounds'],3)

    def test_complete_build_then_three_playtest_repairs(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            route_calls=[]
            issue={'id':'visible-gap','problem':'plant does not move'}
            def route(config,adapter,project,plan,composition,evaluation,episodes,history):
                route_calls.append(project)
                return {'issues':[issue] if len(route_calls)==1 else [],
                        'coverage_passed':True}
            services=SimpleNamespace(judge=lambda *args:{},route=route,
                                     skill=lambda *args:{'skill':''})
            args=arguments(root,services)
            edits=[]
            def build(working,folder,option,plan,config,generator,**kwargs):
                candidate=folder/'candidate_project'
                candidate.mkdir()
                (candidate/'project.godot').write_text('candidate '+str(len(edits)))
                edits.append((working,kwargs['ordinal']))
                return candidate,plan,config,generator,{'summary':'edited'}
            good={'gameplay_status':'partial','aesthetic_status':'partial',
                  'core_playable':True,'critical_regression':False,
                  'evidence_valid':True,'issues':['plant does not move'],
                  'reason':'needs repair'}
            clean={**good,'gameplay_status':'achieved','aesthetic_status':'achieved',
                   'issues':[]}
            with (patch.object(build_repair,'_plan_stage',return_value=options()) as stage,
                  patch.object(build_repair,'_build_candidate',side_effect=build),
                  patch.object(build_repair,'_play_version',return_value=[{'scenario':'train'}]),
                  patch.object(build_repair,'_goal_review',side_effect=[good,good,clean,clean]),
                  patch.object(build_repair,'_repair_comparison',return_value={
                      'preferred_role':'candidate','evidence_valid':True,
                      'critical_regression':False,'improved_tracks':['aesthetic'],
                      'regressed_tracks':[]})):
                result=build_repair.run(**args)
            self.assertEqual(result['state'],'completed')
            self.assertEqual(result['upgrade_status'],'accepted')
            self.assertFalse(result['final_project_evaluated'])
            self.assertFalse((root/'run/final_original').exists())
            self.assertFalse((root/'run/final_upgrade').exists())
            self.assertEqual(json.loads((root/'run/final_upgrade_review.json').read_text())
                             ['selection_method'], 'automatic_after_round_review')
            self.assertEqual(result['rounds_completed'],3)
            self.assertEqual(result['full_build_status'],'playable')
            self.assertEqual(len(edits),2)
            self.assertEqual(edits[0][1],0)
            self.assertEqual(edits[1][0],root/'run/full_build/attempt-001/candidate_project')
            self.assertEqual([call.kwargs['phase'] for call in stage.call_args_list],
                             ['full_build','repair'])
            self.assertEqual(len(route_calls),3)
            self.assertEqual((args['original']/'project.godot').read_text(),'original')
            self.assertEqual(json.loads((root/'run/round-001/result.json').read_text())['action'],
                             'accept_repair')

    def test_unplayable_complete_build_keeps_original_and_skips_repair(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            services=SimpleNamespace(judge=lambda *args:{},
                                     route=lambda *args: self.fail('repair should not run'))
            args=arguments(root,services)
            with (patch.object(build_repair,'_plan_stage',side_effect=ValueError('invalid build')),
                  patch.object(build_repair,'_play_version') as play):
                result=build_repair.run(**args)
            self.assertEqual(result['state'],'completed_with_errors')
            self.assertEqual(result['full_build_status'],'failed')
            self.assertEqual(result['rounds_completed'],0)
            self.assertEqual(result['project'],str(args['original']))
            self.assertFalse((root/'run/round-001').exists())
            play.assert_not_called()

    def test_unfinished_upgrade_enters_three_repairs_and_keeps_diagnosis(self):
        for gameplay, aesthetic in [('partial', 'unobserved'),
                                    ('partial', 'failed'),
                                    ('unobserved', 'unobserved'),
                                    ('failed', 'partial')]:
            with self.subTest(gameplay=gameplay, aesthetic=aesthetic), TemporaryDirectory() as directory:
                root=Path(directory)
                services=SimpleNamespace(judge=lambda *args:{},
                                         route=lambda *args:{'issues':[]},
                                         skill=lambda *args:{'skill':''})
                args=arguments(root,services)
                def build(working,folder,option,plan,config,generator,**kwargs):
                    candidate=folder/'candidate_project'
                    candidate.mkdir()
                    (candidate/'project.godot').write_text('playable')
                    return candidate,plan,config,generator,{}
                review={'gameplay_status':gameplay,'aesthetic_status':aesthetic,
                        'core_playable':True,'critical_regression':False,
                        'evidence_valid':True,'issues':['Show the bargaining interaction'],
                        'reason':'original shop works; upgrade still needs repair'}
                with (patch.object(build_repair,'_plan_stage',return_value=options()) as planner,
                      patch.object(build_repair,'_build_candidate',side_effect=build),
                      patch.object(build_repair,'_play_version',return_value=[{'scenario':'train'}]),
                      patch.object(build_repair,'_goal_review',return_value=review),
                      patch.object(build_repair,'_repair_comparison',return_value={
                          'preferred_role':'tie','evidence_valid':True,
                          'critical_regression':False,'improved_tracks':[],
                          'regressed_tracks':[]})):
                    result=build_repair.run(**args)
                self.assertEqual(result['full_build_status'],'playable')
                self.assertEqual(result['rounds_completed'],3)
                attempts=json.loads((root/'run/full_build/attempts.json').read_text())
                self.assertEqual(len(attempts),1)
                self.assertEqual(attempts[0]['review'],review)
                self.assertEqual([c.kwargs['phase'] for c in planner.call_args_list],
                                 ['full_build','repair','repair','repair'])
                for call in planner.call_args_list[1:]:
                    self.assertEqual(call.kwargs['repair_context']['goal_review'],review)
                self.assertEqual((args['original']/'project.godot').read_text(),'original')

    def test_basic_runtime_and_evidence_failures_still_block_repair(self):
        for failed_check, value in [('core_playable',False), ('evidence_valid',False),
                                    ('critical_regression',True)]:
            with self.subTest(failed_check=failed_check), TemporaryDirectory() as directory:
                root=Path(directory)
                services=SimpleNamespace(judge=lambda *args:{},
                                         route=lambda *args:self.fail('repair should not run'))
                args=arguments(root,services)
                def build(working,folder,option,plan,config,generator,**kwargs):
                    candidate=folder/'candidate_project'
                    candidate.mkdir()
                    (candidate/'project.godot').write_text('candidate')
                    return candidate,plan,config,generator,{}
                review={'gameplay_status':'achieved','aesthetic_status':'achieved',
                        'core_playable':True,'critical_regression':False,
                        'evidence_valid':True,'issues':[],'reason':'basic check failed',
                        failed_check:value}
                with (patch.object(build_repair,'_plan_stage',return_value=options()),
                      patch.object(build_repair,'_build_candidate',side_effect=build),
                      patch.object(build_repair,'_play_version',return_value=[{'scenario':'train'}]),
                      patch.object(build_repair,'_goal_review',return_value=review)):
                    result=build_repair.run(**args)
                self.assertEqual(result['full_build_status'],'failed')
                self.assertEqual(result['rounds_attempted'],0)
                self.assertEqual(result['project'],str(args['original']))
                self.assertEqual(len(json.loads((root/'run/full_build/attempts.json').read_text())),2)

    def test_new_execution_mode_is_rejected_for_initialization(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaisesRegex(ValueError, 'requires RSI upgrade'):
                package_run(root/'package', root/'output', godot='godot',
                            asset_python='python', profile='initialization',
                            rsi_upgrade_execution='build_repair')
            self.assertFalse((root/'output').exists())

    def test_full_build_requires_planned_h3_materials(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with (patch.object(upgrade.models, 'call', return_value=options()) as planner,
                  patch.object(upgrade, '_validate_stage')):
                with self.assertRaisesRegex(ValueError, 'omitted planned H3 material'):
                    upgrade._plan_stage(goal(), 1, root, {'assets':[]},
                                        {'assets':{'bindings':{}}}, [], {}, [],
                                        phase='full_build')
            self.assertEqual(planner.call_count, 3)
            self.assertEqual(planner.call_args.args[4]['materials']['maxItems'], 8)

    def test_static_reference_is_converted_to_text_only_for_new_execution(self):
        material={'id':'garden','kind':'background','motion':'static',
                  'reference_asset_id':'garden','prompt':'An empty garden plate.'}
        option={'materials':[{'operation':'replace','asset':material}]}
        plan={'assets':[{'id':'garden','description':'Warm hand-painted garden'}]}
        upgrade._textualize_static_references(option,plan)
        self.assertNotIn('reference_asset_id',material)
        self.assertIn('Warm hand-painted garden',material['prompt'])

    def test_short_animation_uses_backend_compatible_duration(self):
        from unittest.mock import patch as mock_patch
        for backend, expected in (('local', 4), ('wan', 1)):
            with self.subTest(backend=backend):
                material={'id':'walker_walk','kind':'animation','motion':'loop',
                          'source_duration_seconds':1}
                option={'materials':[{'operation':'replace','asset':material}]}
                with mock_patch.dict('os.environ', {'H3_BACKEND':backend}):
                    upgrade._normalize_build_materials(option, {'assets':[]})
                self.assertEqual(material['source_duration_seconds'],expected)

    def test_new_scene_material_kind_follows_its_scope(self):
        material={'id':'torchlit_room','kind':'animation','generation_scope':'scene',
                  'motion':'loop','source_duration_seconds':4}
        option={'materials':[{'operation':'add','asset':material}]}
        upgrade._normalize_build_materials(option, {'assets':[]})
        self.assertEqual(material['kind'],'background')

    def test_svg_reference_is_replaced_by_text_reference(self):
        material={'id':'rune_loop','kind':'animation','generation_scope':'single_subject',
                  'motion':'loop','reference_asset_id':'dungeon_mark',
                  'description':'Glowing rune','reference_prompt':''}
        option={'materials':[{'operation':'add','asset':material}]}
        plan={'assets':[{'id':'dungeon_mark','description':'Carved white dungeon emblem'}]}
        config={'assets':{'bindings':{'dungeon_mark':['assets/dungeon_mark.svg']}}}
        upgrade._normalize_build_materials(option,plan,config)
        self.assertNotIn('reference_asset_id',material)
        self.assertEqual(material['reference_prompt'],'Carved white dungeon emblem')

    def test_raster_reference_remains_available(self):
        material={'id':'rune_loop','kind':'animation','generation_scope':'single_subject',
                  'motion':'loop','reference_asset_id':'dungeon_mark'}
        option={'materials':[{'operation':'add','asset':material}]}
        plan={'assets':[{'id':'dungeon_mark','description':'Carved white dungeon emblem'}]}
        config={'assets':{'bindings':{'dungeon_mark':['assets/dungeon_mark.png']}}}
        upgrade._normalize_build_materials(option,plan,config)
        self.assertEqual(material['reference_asset_id'],'dungeon_mark')
