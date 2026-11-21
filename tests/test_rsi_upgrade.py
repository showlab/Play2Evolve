"""The upgrade branch selects the last round-reviewed project."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from gamecraft_behavior.adapters.base import GameAdapter
from gamecraft_training.package import upgrade


def goal():
    return {
        'goal': 'Make the core interaction visibly dynamic',
        'core_playtest_goal': 'exercise the old interaction',
        'upgrade_playtest_goal': 'exercise the new animation',
        'upgrade_entry_id': 'train',
        'upgrade_entry_mode': 'scenario',
        'milestones': ['implement', 'integrate', 'polish'],
    }


def dual_goal():
    return {**goal(), 'focus': 'combined',
            'gameplay_plan': {'baseline_gap': 'one enemy per wave',
                              'change': 'two lanes threatened together',
                              'code_work': 'coordinate spawns',
                              'validation': 'defend both lanes'},
            'aesthetic_plan': {'baseline_gap': 'static plant pose',
                               'change': 'visible firing cycle',
                               'material_strategy': 'generate shooter motion with H3',
                               'h3_material_targets': ['shooter firing animation'],
                               'code_integration': 'play frames on firing',
                               'validation': 'see firing pose in replay'}}


class UpgradeFlowTests(TestCase):
    def test_evidence_goal_rejects_an_existing_feature_before_editing(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            audit={'existing_capabilities':['Night economy already exists'],
                   'upgrade_opportunities':['Add a different plant role']}
            existing={**dual_goal(),'goal':'Add night economy'}
            novel={**dual_goal(),'goal':'Add a new tactical plant role and firing animation'}
            rejected={'gameplay_already_present':False,'aesthetic_already_present':True,
                      'gameplay_visible_delta':True,'aesthetic_visible_delta':False,
                      'material_role_clear':True,'evidence_sufficient':True,
                      'reason':'Night atmosphere already exists'}
            accepted={'gameplay_already_present':False,'aesthetic_already_present':False,
                      'gameplay_visible_delta':True,'aesthetic_visible_delta':True,
                      'material_role_clear':True,'evidence_sufficient':True,
                      'reason':'Both tracks are new'}
            with (patch.object(upgrade,'_audit_baseline',return_value=audit),
                  patch.object(upgrade,'_plan_goal',side_effect=[existing,novel]) as plan_goal,
                  patch.object(upgrade,'_review_novelty',side_effect=[rejected,accepted])):
                selected=upgrade._plan_goal_evidence(root,root,None,[],{},[],3,None,None,root)
            self.assertEqual(selected['goal'],novel['goal'])
            self.assertEqual(plan_goal.call_args.kwargs['previous_rejection'],
                             'Night atmosphere already exists')
            self.assertEqual(len(json.loads((root/'rsi_goal_attempts.json').read_text())),2)

    def test_evidence_goal_schema_requires_gameplay_and_aesthetic_plans(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'instruction.md').write_text('Build a game')
            (root/'requirements.json').write_text('{"requirements": []}')
            entry={'entry_id':'train','entry_mode':'scenario'}
            adapter=SimpleNamespace(task_id='example',instructions='play',
                                    actions={'wait':None},allowed_actions=['wait'])
            with (patch.object(upgrade,'available_entries',return_value=[entry]),
                  patch.object(upgrade.models,'content',return_value=[]),
                  patch.object(upgrade.models,'outcome_content',return_value=[]),
                  patch.object(upgrade.models,'call',return_value={**dual_goal(),
                      'player_visible_change':'new encounter and firing motion',
                      'reason':'both gaps are supported',
                      'code_responsibility':'spawns and animation playback',
                      'video_responsibility':'H3 generates firing frames'}) as call):
                upgrade._plan_goal(root,root,adapter,[],{'assets':[]},[],3,
                                   entry,'defend',audit={'upgrade_opportunities':['both']})
            schema=call.call_args.args[4]
            self.assertIn('gameplay_plan',schema)
            self.assertIn('aesthetic_plan',schema)
            self.assertEqual(schema['focus']['enum'],['combined'])

    def test_evidence_mode_retries_failed_execution_without_consuming_round(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original';original.mkdir()
            (original/'project.godot').write_text('original')
            out=root/'run';out.mkdir()
            def build(current,rd,option,plan,config,generator,**kwargs):
                candidate=rd/'candidate_project';candidate.mkdir()
                (candidate/'project.godot').write_text('improved')
                return candidate,plan,config,generator,{}
            services=SimpleNamespace(judge=lambda *args:{},skill=lambda *args:{'skill':''})
            with (patch.object(upgrade,'_plan_goal_evidence',return_value=goal()),
                  patch.object(upgrade,'_plan_stage',side_effect=[ValueError('invalid plan'),
                                                                   {'materials':[]}]) as stage,
                  patch.object(upgrade,'_build_candidate',side_effect=build),
                  patch.object(upgrade,'_play_version',return_value=[{'scenario':'train'}]),
                  patch.object(upgrade,'_stage_review',return_value={
                      'milestone_status':'achieved','critical_regression':False,
                      'evidence_valid':True})):
                result=upgrade.run_upgrade(
                    package=root,output=out,project=original,adapter=SimpleNamespace(),
                    cases=[],config={'assets':{'bindings':{}}},plan={'assets':[]},
                    composition={},generator=SimpleNamespace(root=root),baseline=[],skill='',
                    rounds=1,selected_entry={'entry_id':'train','entry_mode':'scenario'},
                    run_objective=None,player_backend='codex',player_model='gpt-6-sol',
                    godot='godot',rollout_fn=None,code_editor=None,edit_policy={},
                    asset_python='python',video_skill=False,step_frames=6,max_decisions=40,
                    services=services,planning_mode='evidence')
            self.assertEqual(result['rounds_completed'],1)
            self.assertEqual(result['rounds_attempted'],1)
            self.assertEqual(result['attempts_total'],2)
            self.assertEqual(result['upgrade_status'],'accepted')
            self.assertEqual(len(json.loads((out/'round-001/attempts.json').read_text())),2)
            self.assertEqual(stage.call_args.kwargs['attempt_feedback'],'ValueError: invalid plan')

    def test_evidence_mode_stops_after_unresolved_milestone(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original';original.mkdir()
            (original/'project.godot').write_text('original')
            out=root/'run';out.mkdir()
            with (patch.object(upgrade,'_plan_goal_evidence',return_value=goal()),
                  patch.object(upgrade,'_plan_stage',side_effect=ValueError('invalid plan')),
                  patch.object(upgrade,'_play_version') as play):
                result=upgrade.run_upgrade(
                    package=root,output=out,project=original,adapter=SimpleNamespace(),
                    cases=[],config={'assets':{'bindings':{}}},plan={'assets':[]},
                    composition={},generator=SimpleNamespace(root=root),baseline=[],skill='',
                    rounds=3,selected_entry={'entry_id':'train','entry_mode':'scenario'},
                    run_objective=None,player_backend='codex',player_model='gpt-6-sol',
                    godot='godot',rollout_fn=None,code_editor=None,edit_policy={},
                    asset_python='python',video_skill=False,step_frames=6,max_decisions=40,
                    services=SimpleNamespace(judge=lambda *args:{}),planning_mode='evidence')
            self.assertEqual(result['rounds_completed'],0)
            self.assertEqual(result['rounds_attempted'],1)
            self.assertEqual(result['state'],'completed_with_errors')
            self.assertFalse((out/'round-002').exists())
            play.assert_not_called()

    def test_generic_mouse_actions_do_not_modify_the_frozen_adapter(self):
        adapter=GameAdapter(task_id='example',instructions='play',actions={'wait':None},
                            bridge_script=Path('bridge.gd'),observation_fields={'screen':'state'},
                            state_description='screen',replay_fields=('state',))
        augmented=upgrade.with_generic_pointer_actions(adapter)
        self.assertEqual(adapter.pointer_actions,())
        self.assertEqual(augmented.pointer_actions,('click','drag'))
        self.assertEqual(augmented.events({'action':'click','reason':'open menu',
            'pointer':{'x':.5,'y':.5,'to_x':.5,'to_y':.5}})[0]['type'],'mouse_click')

    def test_new_motion_asset_gets_a_distinct_binding_and_keeps_incumbent(self):
        existing = {'id': 'fighter', 'kind': 'sprite', 'description': 'static fighter',
                    'requirements': 'recognizable fighter', 'prompt': 'fighter portrait',
                    'size': [128, 128], 'frames': 1, 'fps': 8,
                    'generation_scope': 'single_subject', 'motion': 'static',
                    'scope_reason': 'an isolated fighter portrait',
                    'game_controlled': ['position', 'health']}
        plan = {'schema_version': 1, 'task_id': 'boxing', 'style': 'painted arcade art',
                'assets': [existing]}
        config = {'assets': {'bindings': {'fighter': ['assets/fighter.png']}}}
        animated = {'id': 'fighter_jump', 'kind': 'animation',
                    'description': 'fighter jumping', 'requirements': 'articulated jump',
                    'prompt': 'The fighter crouches, jumps, and lands.',
                    'size': [128, 128], 'frames': 12, 'fps': 8,
                    'generation_scope': 'single_subject', 'motion': 'event',
                    'scope_reason': 'Godot retains jump position and collision',
                    'game_controlled': ['jump position', 'collision'],
                    'reference_asset_id': 'fighter', 'reference_prompt': '',
                    'source_duration_seconds': None}
        option = {'reason': 'the old pose slides', 'scope': 'single_subject',
                  'code_instructions': 'switch to jump frames during jumping',
                  'video_controls': ['articulated pose'], 'code_controls': ['jump timing'],
                  'materials': [{'operation': 'add', 'asset': animated}]}
        upgrade._validate_stage(option, plan, config)
        from gamecraft_training.package.candidates import material_state
        updated, bindings = material_state(plan, config, option, 'test_upgrade')
        self.assertEqual({a['id'] for a in updated['assets']}, {'fighter', 'fighter_jump'})
        self.assertEqual(len(bindings['assets']['bindings']['fighter_jump']), 12)
        self.assertEqual(config['assets']['bindings']['fighter'], ['assets/fighter.png'])

    def test_three_partial_versions_continue_and_select_last_upgrade(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root/'original'
            original.mkdir()
            (original/'project.godot').write_text('original')
            out = root/'run'
            out.mkdir()
            inputs = []

            def build(current, rd, option, plan, config, generator, **kwargs):
                inputs.append(current)
                candidate = rd/'candidate_project'
                candidate.mkdir()
                (candidate/'project.godot').write_text(f'version {len(inputs)}')
                return candidate, plan, config, generator, {'summary': 'working milestone'}

            def play(project, tasks, path, **kwargs):
                return [{'scenario': 'train', 'version': Path(project).name}]

            services = SimpleNamespace(
                judge=lambda *args: {'scores': {}},
                skill=lambda *args: {'skill': 'Use the visible motion.', 'reason': 'explore'})
            with (patch.object(upgrade, '_plan_goal', return_value=goal()),
                  patch.object(upgrade, '_plan_stage', return_value={'materials': []}),
                  patch.object(upgrade, '_build_candidate', side_effect=build),
                  patch.object(upgrade, '_play_version', side_effect=play),
                  patch.object(upgrade, '_stage_review',
                               side_effect=[
                                   {'milestone_status': 'partial', 'critical_regression': False},
                                   {'milestone_status': 'partial', 'critical_regression': False},
                                   {'milestone_status': 'achieved', 'critical_regression': False},
                               ])):
                result = upgrade.run_upgrade(
                    package=root, output=out, project=original, adapter=SimpleNamespace(),
                    cases=[], config={'assets': {'bindings': {}}}, plan={'assets': []},
                    composition={}, generator=SimpleNamespace(root=root),
                    baseline=[{'scenario': 'train'}], skill='', rounds=3,
                    selected_entry={'entry_id': 'train', 'entry_mode': 'scenario'},
                    run_objective=None, player_backend='codex', player_model='gpt-6-sol',
                    godot='godot', rollout_fn=None, code_editor=None, edit_policy={},
                    asset_python='python', video_skill=False, step_frames=6,
                    max_decisions=40, services=services)

            self.assertEqual(inputs[0], original)
            self.assertEqual(inputs[1], out/'round-001/candidate_project')
            self.assertEqual(inputs[2], out/'round-002/candidate_project')
            self.assertEqual(result['project'], str(out/'round-003/candidate_project'))
            self.assertEqual((original/'project.godot').read_text(), 'original')
            self.assertEqual(json.loads((out/'final_upgrade_review.json').read_text())['status'],
                             'accepted')
            self.assertFalse(result['final_project_evaluated'])
            self.assertFalse((out/'final_original').exists())
            self.assertFalse((out/'final_upgrade').exists())

    def test_partial_rounds_select_working_version_without_final_judge(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root/'original'
            original.mkdir()
            (original/'project.godot').write_text('original')
            out = root/'run'
            out.mkdir()
            def build(current, rd, option, plan, config, generator, **kwargs):
                candidate = rd/'candidate_project'
                candidate.mkdir()
                (candidate/'project.godot').write_text('working')
                return candidate, plan, config, generator, {}
            services = SimpleNamespace(
                judge=lambda *args: {},
                skill=lambda *args: {'skill': 'Watch the screen.'})
            with (patch.object(upgrade, '_plan_goal', return_value=goal()),
                  patch.object(upgrade, '_plan_stage', return_value={'materials': []}),
                  patch.object(upgrade, '_build_candidate', side_effect=build),
                  patch.object(upgrade, '_play_version', return_value=[{'scenario': 'train'}]),
                  patch.object(upgrade, '_stage_review',
                               return_value={'milestone_status': 'partial',
                                             'critical_regression': False}),
                  patch.object(upgrade.models, 'judge_call') as final_review):
                result = upgrade.run_upgrade(
                    package=root, output=out, project=original, adapter=SimpleNamespace(),
                    cases=[], config={'assets': {'bindings': {}}}, plan={'assets': []},
                    composition={}, generator=SimpleNamespace(root=root),
                    baseline=[{'scenario': 'train'}], skill='', rounds=3,
                    selected_entry={'entry_id': 'train', 'entry_mode': 'scenario'},
                    run_objective=None, player_backend='codex', player_model='gpt-6-sol',
                    godot='godot', rollout_fn=None, code_editor=None, edit_policy={},
                    asset_python='python', video_skill=False, step_frames=6,
                    max_decisions=40, services=services)
            self.assertEqual(result['project'], result['working_project'])
            self.assertNotEqual(result['working_project'], str(original))
            self.assertEqual((out/'best_skill.txt').read_text(), 'Watch the screen.')
            self.assertEqual(result['upgrade_status'], 'accepted')
            final_review.assert_not_called()

    def test_critical_regression_rolls_back_game_and_behavior_skill(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root/'original'
            original.mkdir()
            (original/'project.godot').write_text('original')
            out = root/'run'
            out.mkdir()
            inputs = []

            def build(current, rd, option, plan, config, generator, **kwargs):
                inputs.append(current)
                candidate = rd/'candidate_project'
                candidate.mkdir()
                (candidate/'project.godot').write_text('broken')
                return candidate, plan, config, generator, {}

            services = SimpleNamespace(judge=lambda *args: {},
                                       skill=lambda *args: {'skill': 'For broken game.'})
            with (patch.object(upgrade, '_plan_goal', return_value=goal()),
                  patch.object(upgrade, '_plan_stage', return_value={'materials': []}),
                  patch.object(upgrade, '_build_candidate', side_effect=build),
                  patch.object(upgrade, '_play_version', return_value=[{'scenario': 'train'}]),
                  patch.object(upgrade, '_stage_review',
                               return_value={'milestone_status': 'failed',
                                             'critical_regression': True}),
                  patch.object(upgrade.models, 'judge_call') as final_review):
                result = upgrade.run_upgrade(
                    package=root, output=out, project=original, adapter=SimpleNamespace(),
                    cases=[], config={'assets': {'bindings': {}}}, plan={'assets': []},
                    composition={}, generator=SimpleNamespace(root=root),
                    baseline=[{'scenario': 'train'}], skill='Initial skill', rounds=3,
                    selected_entry={'entry_id': 'train', 'entry_mode': 'scenario'},
                    run_objective=None, player_backend='codex', player_model='gpt-6-sol',
                    godot='godot', rollout_fn=None, code_editor=None, edit_policy={},
                    asset_python='python', video_skill=False, step_frames=6,
                    max_decisions=40, services=services)

            self.assertEqual(inputs, [original]*3)
            self.assertEqual(result['project'], str(original))
            self.assertEqual((out/'best_skill.txt').read_text(), 'Initial skill')
            self.assertEqual(result['upgrade_status'], 'not_run')
            final_review.assert_not_called()

if __name__ == '__main__':
    import unittest
    unittest.main()
