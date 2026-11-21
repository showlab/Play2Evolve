"""One implementation for behavior, asset and composition rounds; no task branches."""
import json
import copy
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from game_initialization.package import checkout, inspect_package, seal_without_replay
from gamecraft_behavior.core import write_json, digest
from gamecraft_behavior.runtime import player, rollout
from gamecraft_composition.core import snapshot, accept_targeted
from . import models
from .config import load, aggregate, better
from .edits import Generator, apply_parameters, graft_many
from .repair_verifier import state as repair_state, attempted_outcome
from .selection import promotion
from . import trajectories


def _issue_items(issue):
    """Return the original issue rows represented by a routed batch."""
    return issue.get('issues', [issue])


def _batch_diagnosis(issues, side):
    """Give one worker every independent issue routed to its side this round."""
    flat=[];seen=set()
    for issue in issues:
        for item in _issue_items(issue):
            if item['id'] not in seen:
                flat.append(item);seen.add(item['id'])
    if not flat:return None
    if len(flat)==1:return flat[0]
    assets=[]
    for item in flat:
        for asset in item['asset_ids']:
            if asset not in assets:assets.append(asset)
    evidence=[]
    for item in flat:evidence.extend(item['evidence'])
    acceptance=[]
    for item in flat:
        acceptance.extend(f"[{item['id']}] {criterion}" for criterion in item['rendered_acceptance'])
    rendered_status=('defective' if any(x['rendered_observation']['status']=='defective' for x in flat)
                     else 'unobserved' if any(x['rendered_observation']['status']=='unobserved' for x in flat)
                     else 'clean')
    return {
        'id':f"batch-{side}-"+'-'.join(x['id'] for x in flat),
        'problem':'Repair every diagnosed issue in this batch:\n'+
                  '\n'.join(f"- {x['id']}: {x['problem']}" for x in flat),
        'route':'both' if side=='asset' and any(x['route']=='both' for x in flat) else side,
        'asset_ids':assets,
        'source_observation':{'status':'defective' if side=='asset' else 'clean','evidence':'\n'.join(
            f"[{x['id']}] {x['source_observation']['evidence']}" for x in flat)},
        'rendered_observation':{'status':rendered_status,'evidence':'\n'.join(
            f"[{x['id']}] {x['rendered_observation']['evidence']}" for x in flat)},
        'evidence':evidence,'rendered_acceptance':acceptance,'issues':flat,
        'code_instructions':'\n'.join(f"[{x['id']}] {x.get('code_instructions','')}" for x in flat
                                      if x.get('code_instructions')),
        'video_layers':{x['id']:x.get('video_layer') for x in flat},
    }


def _asset_problem(diagnosis, asset):
    rows=[x for x in _issue_items(diagnosis) if asset in x['asset_ids']]
    return '\n'.join(f"[{x['id']}] {x['source_observation']['evidence']}" for x in rows)


def _combine_parallel_edit(baseline, edited, current, output):
    """Apply a code draft made beside H3 without overwriting its asset changes."""
    before,code,world=digest(baseline),digest(edited),digest(current)
    changes={name for name in before.keys()|code.keys() if before.get(name)!=code.get(name)}
    conflicts=sorted(name for name in changes if before.get(name)!=world.get(name))
    if conflicts:
        raise ValueError('Parallel code edit conflicts with accepted asset changes: '+', '.join(conflicts))
    snapshot(current,output)
    for name in sorted(changes):
        target=output/name
        if name in code:
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(edited/name,target)
        elif target.exists():
            target.unlink()
    return sorted(changes)


def _verify_repairs(services,config,adapter,diagnosis,before,after,before_runs,after_runs,feedback):
    """Verify every routed issue independently and retain safe partial progress."""
    issues=_issue_items(diagnosis)
    if len(issues)==1:
        verdict=services.repair(config,adapter,diagnosis,before,after,before_runs,after_runs)
        ok,reason=accept_targeted(before,after,feedback,diagnosis,verdict)
        verdict['fixed_issue_ids']=[diagnosis['id']] if ok and repair_state(verdict)=='gone' else []
        verdict['improved_issue_ids']=[diagnosis['id']] if ok and repair_state(verdict)!='gone' else []
        return verdict,ok,reason
    rows=[];fixed=[];improved=[];unresolved=[];regressions=[]
    for issue in issues:
        verdict=services.repair(config,adapter,issue,before,after,before_runs,after_runs)
        accepted,reason=accept_targeted(before,after,feedback,issue,verdict)
        rows.append({'issue':issue,'verification':verdict,'accepted':accepted,'reason':reason})
        if accepted and repair_state(verdict)=='gone':fixed.append(issue['id'])
        elif accepted:improved.append(issue['id']);unresolved.append(issue['id'])
        else:unresolved.append(issue['id'])
        regressions.extend(verdict.get('regressions',[]))
    states=[repair_state(row['verification']) for row in rows]
    status=('gone' if not unresolved else
            'not_exercised' if all(value=='not_exercised' for value in states) else 'still_there')
    verdict={'status':status,'coverage_passed':status!='not_exercised',
             'coverage_reason':'; '.join(f"{row['issue']['id']}: {row['verification'].get('coverage_reason','')}"
                                         for row in rows),
             'defect_fixed':not unresolved,'regressions':regressions,
             'acceptance_checks':[],
        'issue_verifications':rows,'fixed_issue_ids':fixed,'improved_issue_ids':improved,
        'unresolved_issue_ids':unresolved,
        'reason':f"fixed {', '.join(fixed) or 'none'}; improved {', '.join(improved) or 'none'}; "
                 f"unresolved {', '.join(unresolved) or 'none'}"}
    # A clean partial repair advances the incumbent. Remaining issues are
    # intentionally re-diagnosed next round instead of rolling back fixes.
    critical=any(r.get('severity','critical')=='critical' for r in regressions)
    ok=bool(fixed or improved) and not critical
    reason=(f"accepted fixes for {', '.join(fixed) or 'none'}; improvements: "
            f"{', '.join(improved) or 'none'}; unresolved: {', '.join(unresolved)}"
            if ok else ('targeted verifier found a critical regression' if critical
                        else 'none of the batched issues visibly improved'))
    return verdict,ok,reason


def verify_episode(adapter,original,replayed):
    if not replayed['replay_verified'] or not adapter.equivalent(original['replay'],replayed['replay']):
        raise RuntimeError('Rendering edit changed fixed-interaction game outcome/state')
    if len(original['decisions'])!=len(replayed['decisions']):raise RuntimeError('Rendering edit changed episode length')
    for before,after in zip(original['decisions'],replayed['decisions']):
        if 'evaluation_state' in before and not adapter.equivalent(before['evaluation_state'],after.get('evaluation_state',{})):
            raise RuntimeError('Rendering edit changed an intermediate game state')


def verify_behavior_non_regression(before,after,adapter,config):
    """Report configured outcomes for code edits; they do not accept/reject visual repairs."""
    old=aggregate(before,adapter,config);new=aggregate(after,adapter,config)
    improved=[];regressed=[]
    for metric in config.get('behavior',{}).get('metrics',[]):
        direction=1 if metric['direction']=='max' else -1
        delta=(new[metric['id']]-old[metric['id']])*direction
        if delta>1e-9:improved.append(metric['id'])
        elif delta < -1e-9:regressed.append(metric['id'])
    return {'before':old,'after':new,'improved_metrics':improved,'regressed_metrics':regressed,
            'acceptance_role':'report_only'}


def accepts_skill_execution(judgment):
    return judgment['status']=='followed' and not judgment['critical_violation']


def run(package,output,*,godot,asset_python,rounds=1,max_decisions=40,step_frames=6,player_state='engine',
        player_mode='interactive',player_backend='local',player_model='local',
        services=models,rollout_fn=rollout,generator_factory=Generator,composition_mode='parameters',
        composition_policy=None,code_editor=None,video_skill=False,judge_backend='local',judge_model='local',
        profile='rsi',initial_behavior_skill=None,global_video_skill=None,local_video_skill=None,
        global_video_skill_store=None,asset_parallelism=1,asset_candidates=4,initialization_max_frames=64,
        initialization_scene=None,initialization_mode='per_scene',initialization_groups=None,
        initialization_playtest='fixed',initialization_group_count=3,
        initialization_behavior_skill=False,rsi_goal_mode='planned',rsi_mode='repair',
        rsi_upgrade_planning='legacy',
        rsi_upgrade_execution='incremental',
        final_showcase=True,history_windows=None):
    from .profiles import resolve as resolve_profile
    defaults={'judge':8,'legacy_planner':5,'planner':3,'editor':5,'skill':8}
    history_windows={**defaults,**(history_windows or {})}
    if any(type(value) is not int or value<0 for value in history_windows.values()):
        raise ValueError('History windows must be nonnegative integers')
    execution=resolve_profile(profile)
    if rsi_goal_mode not in ('planned','legacy'):
        raise ValueError('Unknown RSI goal mode')
    if rsi_mode not in ('upgrade','repair'):
        raise ValueError('Unknown RSI optimization mode')
    if rsi_upgrade_planning not in ('legacy','evidence'):
        raise ValueError('Unknown RSI upgrade planning mode')
    if rsi_upgrade_planning!='legacy' and (profile!='rsi' or rsi_mode!='upgrade'):
        raise ValueError('Evidence planning requires RSI upgrade mode')
    if rsi_upgrade_execution not in ('incremental','build_repair'):
        raise ValueError('Unknown RSI upgrade execution mode')
    if rsi_upgrade_execution=='build_repair' and (profile!='rsi' or rsi_mode!='upgrade' or
                                                   rsi_upgrade_planning!='evidence'):
        raise ValueError('Build-and-repair requires RSI upgrade with evidence planning')
    if profile=='rsi' and rsi_mode=='upgrade' and composition_mode!='codex':
        raise ValueError('RSI upgrade mode requires the Codex game editor')
    if profile=='rsi' and rsi_mode=='upgrade' and rsi_goal_mode!='planned':
        raise ValueError('RSI upgrade mode requires a planned core-gameplay entry')
    if initialization_playtest not in ('fixed','program','agent','qwen27'):
        raise ValueError('Unknown initialization playtest mode')
    program_initialization=initialization_playtest=='program'
    interactive_initialization=initialization_playtest in ('agent','qwen27')
    if initialization_behavior_skill and (profile!='initialization' or not program_initialization):
        raise ValueError('Initialization Behavior Skill requires initialization program playtests')
    if interactive_initialization:
        if profile!='initialization' or player_state!='engine' or initialization_mode=='whole_game':
            raise ValueError('Interactive initialization playtests require initialization profile, engine state and per_scene/grouped mode')
        if initialization_playtest=='qwen27':
            if player_backend!='local' or player_model!='local':
                raise ValueError('Qwen27 mode requires the local Player backend and local service alias')
            if not os.environ.get('LOCAL_URL') or os.environ.get('GAMECRAFT_LOCAL_MODEL_ID')!='Qwen3.8-27B':
                raise ValueError('Qwen27 mode requires explicit LOCAL_URL and GAMECRAFT_LOCAL_MODEL_ID=Qwen3.8-27B')
    if program_initialization:
        if profile!='initialization' or player_state!='engine':
            raise ValueError('Initialization program playtests require initialization profile and engine state')
        if initialization_groups is not None or initialization_scene is not None or initialization_mode=='whole_game':
            raise ValueError('Initialization Player plans its own groups')
        if type(initialization_group_count) is not int or not 1<=initialization_group_count<=10:
            raise ValueError('Initialization Player group count must be 1–10')
    if (not execution['behavior_enabled'] and not initialization_behavior_skill
            and not interactive_initialization and initial_behavior_skill is not None):
        raise ValueError('Initialization profile does not use a Behavior Skill in this playtest mode')
    if initialization_scene is not None and profile!='initialization':
        raise ValueError('Single-scene selection requires initialization profile')
    if initialization_mode not in ('per_scene','grouped','whole_game'):
        raise ValueError('Unknown initialization mode')
    if initialization_mode=='whole_game' and profile!='initialization':
        raise ValueError('Whole-game initialization requires initialization profile')
    if initialization_mode=='whole_game' and initialization_scene is not None:
        raise ValueError('Whole-game initialization cannot select a single scene')
    if not program_initialization and (initialization_mode=='grouped') != (initialization_groups is not None):
        raise ValueError('Grouped initialization requires scenario groups')
    if initialization_groups is not None and (profile!='initialization' or initialization_scene is not None):
        raise ValueError('Scenario groups require full initialization profile')
    if not video_skill and any(x is not None for x in (global_video_skill,local_video_skill,global_video_skill_store)):
        raise ValueError('Video Skill layers/store require video_skill=True')
    if global_video_skill_store and not execution['submit_global_video_skill']:
        raise ValueError('Global Video Skill submission requires initialization profile')
    if type(rounds) is not int or not 1<=rounds<=20:raise ValueError('Rounds must be 1–20')
    if type(initialization_max_frames) is not int or not 1<=initialization_max_frames<=600:
        raise ValueError('Initialization frame budget must be 1–600')
    if type(asset_parallelism) is not int or not 1<=asset_parallelism<=4:
        raise ValueError('Asset parallelism must be 1–4')
    if type(asset_candidates) is not int or not 1<=asset_candidates<=4:
        raise ValueError('Asset candidates must be 1–4')
    if not 1<=step_frames<=30 or not 1<=max_decisions or max_decisions*step_frames>600:raise ValueError('Invalid decision budget')
    if judge_backend not in ('local','codex','api') or (judge_backend!='local' and judge_model=='local'):
        raise ValueError('Invalid Judge backend/model')
    if player_backend not in ('local','codex','api') or (player_backend!='local' and player_model=='local'):
        raise ValueError('Invalid Player backend/model')
    if player_mode not in ('interactive','program'):
        raise ValueError('Unknown Player mode')
    if player_mode=='program' and (profile!='rsi' and not program_initialization or player_state!='engine'):
        raise ValueError('Program Player requires RSI profile and engine state')
    if profile=='rsi' and player_state!='engine':
        raise ValueError('RSI candidate acceptance policies require engine state')
    os.environ['GAMECRAFT_JUDGE_BACKEND']=judge_backend
    os.environ['GAMECRAFT_JUDGE_MODEL']=judge_model
    # Validate the new contract before creating the experiment directory.
    _,preview_adapter,cases=inspect_package(package)
    _,_,preview_spec,_=load(package,preview_adapter,cases,profile=profile)
    if composition_mode not in ('parameters','codex'):raise ValueError('Unknown composition mode')
    edit_policy=None
    if composition_mode=='codex':
        from .codex_edit import CodexEditor, policy
        edit_policy=policy(preview_spec,composition_policy)
        from .config import relative
        for name in edit_policy['protected']:
            if not relative(Path(package)/'game',name).exists():raise ValueError(f'Missing protected path: {name}')
        code_editor=code_editor or CodexEditor(godot=godot)
        if isinstance(code_editor,CodexEditor):
            code_editor.history_window=history_windows['editor']
    out=Path(output).resolve()
    project,adapter,cases=checkout(package,out)
    if profile=='rsi' and rsi_mode=='upgrade' and rsi_upgrade_planning=='evidence':
        # Both game versions receive the same generic mouse controls. Never
        # change the frozen package adapter or another run's action contract.
        from .upgrade import with_generic_pointer_actions
        adapter=with_generic_pointer_actions(adapter)
    config,plan,composition,groups=load(out/'initial',adapter,cases,profile=profile)
    config['_history_windows']=history_windows
    if initialization_scene is not None:
        selected=[case for case in groups['initialization'] if case['id']==initialization_scene]
        if len(selected)!=1:raise ValueError('Unknown initialization scene: '+str(initialization_scene))
        groups['initialization']=selected
    init_units=groups['initialization']
    init_player=None
    if program_initialization:
        from .initialization_player import InitializationPlayer, plan_groups, validate_groups
        planner=getattr(services,'plan_initialization_groups',plan_groups)
        requirements=json.loads((out/'initial/requirements.json').read_text())['requirements']
        fingerprint=digest(project)
        proposal=planner(adapter,project,init_units,requirements,count=initialization_group_count,
                         backend=player_backend,model=player_model)
        if digest(project)!=fingerprint:raise RuntimeError('Player planning changed the game')
        init_units=validate_groups(proposal,init_units,initialization_group_count)
        write_json(out/'player_groups.json',proposal)
        initialization_mode='grouped'
        init_player=InitializationPlayer(adapter=adapter,rollout=rollout_fn,godot=godot,
            model=player_model,backend=player_backend,step_frames=step_frames,max_decisions=max_decisions,
            policy_factory=getattr(services,'initialization_policy',None),
            behavior_skill_enabled=initialization_behavior_skill)
        init_player.configure(init_units)
    elif initialization_mode=='grouped':
        from .scenario_groups import build as build_scenario_groups
        init_units=build_scenario_groups(init_units,initialization_groups)
    if interactive_initialization:
        from .interactive_initialization import InteractiveInitializationPlayer
        init_player=InteractiveInitializationPlayer(adapter=adapter,rollout=rollout_fn,godot=godot,
            model=player_model,backend=player_backend,step_frames=step_frames,
            max_decisions=max_decisions,mode=initialization_playtest,
            game_brief=(out/'initial/instruction.md').read_text(errors='replace')[:6000],
            factory=getattr(services,'interactive_initialization_player',None))
    config['_package_root']=str(out/'initial')
    composition['editor']=composition_mode
    if edit_policy:composition['code_edit']=edit_policy
    decide=(player(model=player_model,backend=player_backend,adapter=adapter)
            if execution['behavior_enabled'] and player_mode=='interactive' else None)
    generator=generator_factory(out/'generation',plan,config,asset_python,video_skill,asset_candidates)
    if video_skill and (global_video_skill is not None or local_video_skill is not None):
        from gamecraft_video_skill import install_layers
        install_layers(generator.root,global_source=global_video_skill,local_source=local_video_skill)
    history=[]
    scene_reports={}
    finished_scenes=set()
    selected_recordings=None
    if profile=='initialization':
        from .recordings import SelectedRecordings
        selected_recordings=SelectedRecordings(out/'initialization_recordings',
            [unit['id'] for unit in init_units] if initialization_mode!='whole_game' else ['whole_game'])
    if profile=='initialization' and initialization_mode in ('per_scene','grouped'):
        schedule=[(case,local_round) for case in init_units for local_round in range(1,rounds+1)]
        write_json(out/'scene_schedule.json',{'rounds_per_scene':rounds,'max_frames':initialization_max_frames,
                   'scenes':init_units,
                   'coverage_policy':'retain_unverified_without_repair_or_promotion'})
    else:
        schedule=[(None,index) for index in range(1,rounds+1)]
    if initial_behavior_skill is None:skill=''
    else:
        source=Path(initial_behavior_skill)
        skill=source.read_text().strip() if source.is_file() else str(initial_behavior_skill).strip()
    run_objective=None
    selected_entry=None
    if profile=='rsi' and rsi_goal_mode=='planned':
        from .objective import create
        print('[rsi] planning shared objective once before all rounds',flush=True)
        run_objective,selected_entry=create(
            adapter,project,out/'initial',out,model=player_model,backend=player_backend,
            cases=cases,frame_budget=max_decisions*step_frames,
            planner=getattr(services,'plan_rsi_objective',None))
        groups['train']=[{'id':selected_entry['entry_id'],
                          'launch':selected_entry['entry_mode']}]
    seen_compositions=set();seen_skills={'',skill}
    write_json(out/'run.json',{'package':str(Path(package).resolve()),'task_id':adapter.task_id,'configuration':config,
               'profile':profile,'profile_settings':execution,
               'rsi_goal_mode':rsi_goal_mode if profile=='rsi' else None,
               'rsi_mode':rsi_mode if profile=='rsi' else None,
               'rsi_upgrade_planning':rsi_upgrade_planning if profile=='rsi' and rsi_mode=='upgrade' else None,
               'rsi_upgrade_execution':rsi_upgrade_execution if profile=='rsi' and rsi_mode=='upgrade' else None,
               'run_objective':run_objective,
               'rsi_entry':selected_entry,
               'rounds':rounds,'round_budget_scope':initialization_mode if profile=='initialization' else 'whole_game',
               'initialization_mode':initialization_mode if profile=='initialization' else None,
               'initialization_scene':initialization_scene,
               'max_decisions':max_decisions,'step_frames':step_frames,'player_state':player_state,
               'player':({'backend':player_backend,'model':player_model,
                          **({'mode':'program'} if player_mode=='program' or program_initialization else
                             {'mode':'interactive'} if interactive_initialization else {}),
                          **({'local_model_identity':os.environ['GAMECRAFT_LOCAL_MODEL_ID'],
                              'local_url':os.environ['LOCAL_URL']} if initialization_playtest=='qwen27' else {})}
                         if execution['behavior_enabled'] or init_player is not None else None),
               'initialization_playtest':initialization_playtest if profile=='initialization' else None,
               'initialization_behavior_skill':initialization_behavior_skill if profile=='initialization' else None,
               'initialization_max_frames':initialization_max_frames,
               'initialization_trajectories':({'mode':'reactive_program','backend':player_backend,
                    'model':player_model,'group_count':len(init_units)} if program_initialization else
                    {'mode':'interactive_agent','backend':player_backend,'model':player_model,
                     'group_count':len(init_units),
                     **({'local_model_identity':'Qwen3.8-27B'} if initialization_playtest=='qwen27' else {})}
                    if interactive_initialization else
                    {'mode':'candidate_authored','backend':'codex',
                    'model':os.environ.get('GAMECRAFT_TRAJECTORY_MODEL',
                                os.environ.get('GAMECRAFT_REPAIR_PLANNER_MODEL','gpt-6-sol'))}
                    if profile=='initialization' else None),
               'asset_parallelism':asset_parallelism,
               'history_windows':history_windows,
               'asset_candidates':asset_candidates,
               'composition_mode':composition_mode,'composition_policy':edit_policy,
               'judge':{'backend':judge_backend,'model':judge_model},
               'behavior_gate':({'type':'provisional_skill_guidance','outcome_metrics':'report_only'}
                                if profile=='rsi' and rsi_mode=='upgrade' else
                                {'type':'next_round_train_observation','outcome_metrics':'report_only'}),
               'candidate_playtest':({'mode':'two_profile_upgrade_program',
                                      'uses_behavior_skill':bool(skill),
                                      'backend':player_backend,'model':player_model}
                                     if profile=='rsi' and rsi_mode=='upgrade' else
                                     {'mode':'interactive_agent' if interactive_initialization else 'fresh_program_policy',
                                      'uses_behavior_skill':bool(skill) if interactive_initialization else initialization_behavior_skill,
                                      'backend':player_backend,'model':player_model}
                                     if execution['behavior_enabled'] or initialization_behavior_skill
                                     or (interactive_initialization and skill) else None),
               'video_skill':video_skill,
               'video_skill_proposer':({'backend':'codex','model':os.environ.get('GAMECRAFT_VIDEO_SKILL_MODEL','gpt-6-sol')}
                                       if video_skill else None),
               'official_benchmark_metric':False})

    def play(current,text,tasks,path):
        from gamecraft_behavior.objective import previous_context
        previous=previous_context(last_training) if run_objective is not None else None
        active_decide=decide
        if run_objective is not None and player_mode=='interactive':
            active_decide=player(model=player_model,backend=player_backend,adapter=adapter,
                                 run_objective=run_objective,previous_playtest=previous)
        if player_mode=='program':
            from gamecraft_behavior.program_policy import program_player
            active_decide=program_player(current,path/'policy.py',model=player_model,
                                         backend=player_backend,adapter=adapter,
                                         run_objective=run_objective,previous_playtest=previous)
        results=[]
        for i,case in enumerate(tasks):
            r=rollout_fn(current,path/f'episode-{i:03}',text,active_decide,godot=godot,adapter=adapter,
                          scenario=case['id'],normal_launch=case.get('launch','scenario')=='default',
                          step_frames=step_frames,max_decisions=max_decisions,player_state=player_state)
            if not r['replay_verified']:raise RuntimeError('Policy episode failed deterministic replay')
            results.append(r)
        return results

    def play_scripted(current,tasks,path):
        results=[]
        for i,case in enumerate(tasks):
            actions=iter(case['actions'])
            def scripted(*args,**kwargs):
                return adapter.scripted_action(next(actions), 'Declared initialization check')
            r=rollout_fn(current,path/f'episode-{i:03}','',scripted,godot=godot,adapter=adapter,
                         scenario=case['id'],normal_launch=case.get('launch','scenario')=='default',
                         step_frames=case.get('step_frames',6),
                         max_decisions=len(case['actions']),player_state=player_state)
            if not r['replay_verified']:raise RuntimeError('Initialization episode failed deterministic replay')
            r['scripted_case']=copy.deepcopy(case)
            results.append(r)
        return results

    def replay_candidate(candidate,episodes,path,*,diagnosis,changes,config):
        if init_player is not None:
            kwargs={'diagnosis':diagnosis,'changes':changes}
            if program_initialization or interactive_initialization:
                kwargs['skill']=round_skill
            return init_player.candidate(candidate,episodes,path,**kwargs)
        if execution['behavior_enabled']:
            from .playtests import replay_candidate as play_candidate
            return play_candidate(candidate,episodes,path,adapter=adapter,
                diagnosis=diagnosis,changes=changes,rollout=rollout_fn,godot=godot,
                model=player_model,backend=player_backend,step_frames=step_frames,
                max_decisions=max_decisions,player_state=player_state)
        return trajectories.replay_candidate(config,adapter,candidate,episodes,path,
            diagnosis=diagnosis,changes=changes,play=play_scripted,
            propose=getattr(services,'trajectory',trajectories.author))

    def replay_same(candidate,episodes,path,*,preserve_state=True):
        results=[]
        for i,original in enumerate(episodes):
            actions=iter(original['decisions'])
            def scripted(*args,**kwargs):
                d=next(actions);return {**{k:d[k] for k in ('action','pointer') if k in d},
                                         'reason':'Identical interaction for world comparison'}
            r=rollout_fn(candidate,path/f'episode-{i:03}','',scripted,godot=godot,adapter=adapter,
                          scenario=original['scenario'],normal_launch=original.get('normal_launch',False),
                          step_frames=original.get('decision_frames',step_frames),
                          max_decisions=len(original['decisions']),player_state=player_state)
            if preserve_state:
                verify_episode(adapter,original,r)
            elif not r['replay_verified']:
                raise RuntimeError('Edited game failed deterministic replay')
            results.append(r)
        return results

    # A promoted candidate was already replayed with the declared actions. Keep
    # that evidence for the next round when its project and scripts are identical.
    scripted_replay_cache=None

    def scripted_signature(tasks):
        return [(task['id'],task.get('launch','scenario')=='default',
                 task.get('step_frames',6),tuple(task['actions'])) for task in tasks]

    def reusable_scripted_replay(current,tasks):
        entry=scripted_replay_cache
        if entry is None or entry['signature']!=scripted_signature(tasks):return None
        if entry['project_digest']!=digest(current):return None
        episodes=entry['episodes']
        if len(episodes)!=len(tasks):return None
        for episode,task in zip(episodes,tasks):
            decisions=episode.get('decisions',[])
            if (not episode.get('replay_verified') or episode.get('scenario')!=task['id'] or
                episode.get('normal_launch')!=(task.get('launch','scenario')=='default') or
                episode.get('decision_frames')!=task.get('step_frames',6) or
                not decisions or len(decisions)>len(task['actions']) or
                [{k:d[k] for k in ('action','pointer') if k in d} for d in decisions]!=
                [{k:d[k] for k in ('action','pointer') if k in d} for d in
                 (adapter.scripted_action(a) for a in task['actions'][:len(decisions)])] or
                any(not Path(d.get('image','')).is_file() for d in decisions)):
                return None
        return episodes

    def remember_scripted_replay(current,tasks,episodes):
        nonlocal scripted_replay_cache
        scripted_replay_cache={'signature':scripted_signature(tasks),
                               'project_digest':digest(current),'episodes':episodes}

    if profile=='rsi' and rsi_mode=='upgrade':
        from .upgrade import run_upgrade
        last_training=[]
        (out/'upgrade_baseline').mkdir()
        baseline=play(project,skill,groups['train'],out/'upgrade_baseline'/'train')
        result=run_upgrade(package=package,output=out,project=project,adapter=adapter,
            cases=cases,config=config,plan=plan,composition=composition,
            generator=generator,baseline=baseline,skill=skill,rounds=rounds,
            selected_entry=selected_entry,run_objective=run_objective,
            player_backend=player_backend,player_model=player_model,
            godot=godot,rollout_fn=rollout_fn,code_editor=code_editor,
            edit_policy=edit_policy,asset_python=asset_python,
            video_skill=video_skill,step_frames=step_frames,
            max_decisions=max_decisions,services=services,
            planning_mode=rsi_upgrade_planning,
            execution_mode=rsi_upgrade_execution)
        inspect_package(package)
        return result

    bootstrap_recordings=None
    if program_initialization:
        from .recordings import SelectedRecordings
        bootstrap_recordings=SelectedRecordings(out/'bootstrap_recordings',
            [unit['id'] for unit in init_units])
        write_json(out/'progress.json',{'stage':'bootstrap_playtests','groups':len(init_units)})
        bootstrap_episodes=init_player.record_bootstrap(project,out/'bootstrap_playtests',skill=skill)
        for unit in init_units:
            bootstrap_recordings.remember(project,unit,bootstrap_episodes[unit['id']],0)
        bootstrap_recordings.finish(project)

    last_training=[]
    for case,local_round in schedule:
        if case and case['id'] in finished_scenes:continue
        index=len(history)+1
        if case:
            ordinal=init_units.index(case)+1
            # Numeric directories never interpolate an agent-authored path.
            scene_dir=out/'scenes'/f'{ordinal:03}'
            rd=scene_dir/f'round-{local_round:03}'
            rd.mkdir(parents=True)
            config['_initialization_scene']={'id':case['id'],'case':case,
                'scenario_ids':case.get('scenario_ids',[case['id']]),
                'round':local_round,'max_rounds':rounds,'max_frames':initialization_max_frames}
            acceptance=json.loads((out/'initial/acceptance_plan.json').read_text())['requirements']
            mapped={row['id'] for row in acceptance if
                    set(case.get('scenario_ids',[case['id']])) & set(row.get('scenarios',[]))}
            requirements=json.loads((out/'initial/requirements.json').read_text())['requirements']
            config['_initialization_scene']['requirements']=[row for row in requirements if row['id'] in mapped]
            if init_player is not None:
                case['requirements']=requirements
            print(f'[scene {ordinal}/{len(init_units)} {case["id"]}] round {local_round}/{rounds}',flush=True)
            write_json(out/'progress.json',{'stage':'scene_optimization',**config['_initialization_scene'],
                       'directory':str(rd)})
        else:
            rd=out/f'round-{index:03}';rd.mkdir()
            if profile=='initialization':
                print(f'[whole-game initialization] round {index}/{rounds}',flush=True)
                write_json(out/'progress.json',{'stage':'whole_game_optimization','round':index,
                           'max_rounds':rounds,'directory':str(rd)})
        record={'round':index,'project_before':str(project),'sides':{}}
        round_skill=skill
        if case:record.update(scene=case['id'],scene_round=local_round)
        if execution['behavior_enabled']:
            training=play(project,skill,groups['train'],rd/'train')
            last_training=training
            behavior={'action':'skip','report_metrics':{'train':aggregate(training,adapter,config)},
                      'verification':{'status':'not_run','reason':'Candidate Skill is observed in the next round'}}
            # The same train interaction supplies behavior and world-repair evidence.
            world_runs=training
            print(f'[round {index}] behavior: diagnose and propose',flush=True)
            proposal=services.skill(adapter,skill,training,history[-history_windows['skill']:] if history_windows['skill'] else [])
            write_json(rd/'skill_proposal.json',proposal)
            candidate=proposal['skill'].strip()
            if not candidate:
                raise ValueError('Skill must not be empty')
            if candidate in seen_skills:
                behavior.update(reason='Repeated skill')
            else:
                seen_skills.add(candidate)
                skill=candidate
                behavior.update(action='adopt_unverified',reason=proposal.get('reason','Queued for the next round'))
        else:
            training=[]
            scripted_tasks=case.get('members',[case]) if case else groups['initialization']
            if initialization_behavior_skill:
                baseline=None
            elif init_player is not None:
                baseline=init_player.reusable(project,case)
            else:
                baseline=reusable_scripted_replay(project,scripted_tasks)
            if baseline is None:
                if init_player is not None:
                    prior=init_player.selected.get(case['id'],{}).get('episodes',[])
                    kwargs={'unit':case,'prior':prior}
                    if program_initialization:
                        kwargs.update(fresh=initialization_behavior_skill,skill=round_skill)
                    elif interactive_initialization:
                        kwargs['skill']=round_skill
                    baseline=init_player.play(project,scripted_tasks,rd/'initialization_current',**kwargs)
                else:
                    baseline=play_scripted(project,scripted_tasks,rd/'initialization_current')
            else:
                write_json(rd/'initialization_current_reused.json',{
                    'project':str(project),'scenarios':[task['id'] for task in scripted_tasks],
                    'videos':[episode.get('video') for episode in baseline]})
            world_runs=baseline
            behavior={'action':'disabled','reason':'Initialization playtesting does not update Behavior Skill'}
            if initialization_behavior_skill:
                behavior={'action':'skip','verification':{'status':'next_round_observation'}}
                proposal=services.skill(adapter,round_skill,world_runs,
                                        history[-history_windows['skill']:] if history_windows['skill'] else [])
                write_json(rd/'skill_proposal.json',proposal)
                candidate=proposal['skill'].strip()
                if not candidate:
                    raise ValueError('Skill must not be empty')
                if candidate in seen_skills:
                    behavior['reason']='Repeated skill'
                else:
                    seen_skills.add(candidate)
                    skill=candidate
                    behavior.update(action='adopt_unverified',
                                    reason=proposal.get('reason','Queued for the next round'))
        record['sides']['behavior']=behavior
        if initialization_behavior_skill:
            (rd/'skill_before.txt').write_text(round_skill)
            (rd/'skill_after.txt').write_text(skill)
        (rd/'skill.txt').write_text(skill)
        # A single judge owns root-cause attribution before either world side runs.
        evaluation=services.judge(config,adapter,project,world_runs)
        evaluation['execution_profile']=execution
        write_json(rd/'quality_before.json',evaluation)
        repair_plan=services.route(config,adapter,project,plan,composition,evaluation,world_runs,history)
        if video_skill:
            from gamecraft_video_skill.decisions import retrieve
            config['_video_skill_guidance']=retrieve(
                generator.root,assets=plan['assets'],
                problems=[item.get('problem','') for item in repair_plan.get('issues',[])],
                task_id=adapter.task_id,global_store=global_video_skill_store)
            write_json(rd/'video_skill_planning_context.json',config['_video_skill_guidance'])
        if composition_mode=='codex' and hasattr(services,'plan_candidates'):
            repair_plan=services.plan_candidates(config,adapter,project,plan,composition,
                                                repair_plan,world_runs,history)
        elif composition_mode=='codex' and hasattr(services,'plan_repair'):
            repair_plan=services.plan_repair(config,adapter,project,plan,composition,
                                             repair_plan,world_runs,history)
        write_json(rd/'repair_plan.json',repair_plan)
        record['repair_plan']=repair_plan
        grouped=repair_plan.get('grouped',False)
        if grouped:
            from .candidates import run_groups,run_round_bundle
            runner=run_round_bundle if repair_plan.get('round_bundle') else run_groups
            outcome=runner(repair_plan=repair_plan,project=project,plan=plan,config=config,
                generator=generator,directory=rd/'groups',adapter=adapter,evaluation=evaluation,
                episodes=world_runs,history=history,services=services,code_editor=code_editor,
                edit_policy=edit_policy,generator_factory=generator_factory,asset_python=asset_python,
                asset_candidates=asset_candidates,video_skill=video_skill,replay_same=replay_same,
                replay_candidate=replay_candidate,
                global_video_skill_store=global_video_skill_store)
            project=outcome['project'];world_runs=outcome['episodes'];plan=outcome['plan']
            config=outcome['config'];generator=outcome['generator']
            record['groups']=outcome['groups'];record['sides'].update(outcome['sides'])
            for side in ('asset','composition'):
                sd=rd/side;sd.mkdir(exist_ok=True)
                write_json(sd/'result.json',record['sides'][side])
            write_json(out/'current_asset_plan.json',plan)
            write_json(out/'current_training.json',{k:v for k,v in config.items() if not k.startswith('_')})
        blocked_both=set();pending_assets={};parallel_code=None
        for side in (() if grouped else ('asset','composition')):
            sd=rd/side;sd.mkdir(exist_ok=True)
            rec={'action':'skip'}
            print(f'[round {index}] {side}: route',flush=True)
            diagnosis=None;pending_key=None
            try:
                choices=[x for x in repair_plan['issues'] if x['route']==side]
                choices += [x for x in repair_plan['issues'] if x['route']=='both']
                if side=='composition':
                    choices=[x for x in choices if x['id'] not in blocked_both]
                diagnosis=_batch_diagnosis(choices,side) if repair_plan['coverage_passed'] else None
                if not repair_plan['coverage_passed']:
                    if case:rec['action']='retained_unverified'
                    rec['reason']='Insufficient interaction evidence: '+repair_plan['coverage_reason']
                    write_json(sd/'result.json',rec);record['sides'][side]=rec
                    continue
                if diagnosis is None:
                    rec['reason']='No issue routed to this side'
                    write_json(sd/'result.json',rec);record['sides'][side]=rec
                    continue
                if side=='composition' and any(x['id'] in blocked_both for x in _issue_items(diagnosis)):
                    rec.update(reason='Asset-first repair did not pass; composition integration blocked',diagnosis=diagnosis)
                    write_json(sd/'result.json',rec);record['sides'][side]=rec
                    continue
                write_json(sd/'diagnosis.json',diagnosis);rec['diagnosis']=diagnosis
                pending_key=(next(iter(pending_assets)) if side=='composition' and pending_assets
                             else diagnosis['id'])
                pending_context=pending_assets.get(pending_key,{})
                repair_project=pending_context.get('project',project)
                editor_evaluation=pending_context.get('evaluation',evaluation)
                editor_episodes=pending_context.get('runs',world_runs)
                if side=='composition' and composition_mode=='parameters' and not composition['parameters']:
                    rec['reason']='No declared mutable parameters'
                    if pending_key in pending_assets:
                        pending=pending_assets.pop(pending_key);asset_rec=pending['record']
                        asset_rec.update(action='reject',reason='asset candidate not committed because no composition editor is available')
                        write_json(pending['directory']/'result.json',asset_rec)
                else:
                    candidate_project=sd/'candidate'
                    if side=='asset':
                        assets=diagnosis['asset_ids'];generations=[]
                        if any(asset not in config['assets']['bindings'] for asset in assets):
                            raise ValueError('Unknown asset diagnosis')
                        # Start the Codex draft from this round's incumbent while
                        # H3 works on independent asset files. Only the main
                        # thread merges candidates and runs game-level judges.
                        code_choices=[x for x in repair_plan['issues'] if x['route'] in ('composition','both')]
                        code_diagnosis=(_batch_diagnosis(code_choices,'composition')
                                        if composition_mode=='codex' else None)
                        integration_planned=bool(code_choices) and (
                            code_diagnosis is not None or
                            (composition_mode=='parameters' and bool(composition['parameters'])))
                        workers=min(asset_parallelism,len(assets))
                        parallel_draft=code_diagnosis is not None and not repair_plan.get('unified')
                        with ThreadPoolExecutor(max_workers=workers+parallel_draft) as pool:
                            code_future=None
                            if parallel_draft:
                                code_dir=rd/'composition'/'parallel_draft'
                                code_dir.mkdir(parents=True,exist_ok=True)
                                code_project=code_dir/'candidate'
                                print(f'[round {index}] H3 and Codex: parallel drafts',flush=True)
                                code_future=pool.submit(
                                    code_editor,project,code_project,code_dir,diagnosis=code_diagnosis,
                                    evaluation=evaluation,episodes=world_runs,history=history,
                                    adapter=adapter,policy=edit_policy)
                            pending={pool.submit(generator,asset,
                                {'problem':_asset_problem(diagnosis,asset),
                                 'prompt':repair_plan['asset_prompts'][asset]}
                                if repair_plan.get('unified') else _asset_problem(diagnosis,asset),
                                sd/asset):asset for asset in assets}
                            by_asset={}
                            for future in as_completed(pending):
                                asset=pending[future];generated=future.result()
                                by_asset[asset]={'asset':asset,**generated}
                            if code_future is not None:
                                parallel_code={'baseline':project,'project':code_project,
                                               'diagnosis':code_diagnosis,'directory':code_dir}
                                try:parallel_code['edit']=code_future.result()
                                except Exception as exc:
                                    parallel_code['error']=f'{type(exc).__name__}: {exc}'
                        generations=[by_asset[asset] for asset in assets]
                        rec['generations']=generations
                        successful=[item['asset'] for item in generations if item['status']=='generated']
                        failed_assets=set(assets)-set(successful)
                        rec['unavailable_assets']=sorted(failed_assets)
                        blocked_both.update(x['id'] for x in _issue_items(diagnosis)
                                            if x['route']=='both' and failed_assets.intersection(x['asset_ids']))
                        if not successful:
                            rec.update(action=('error' if all(item['status']=='error' for item in generations)
                                               else 'reject'),
                                       reason='No viable asset candidate in this batch: '+
                                       '; '.join(f"{item['asset']}: {item.get('reason',item['status'])}"
                                                 for item in generations))
                            write_json(sd/'result.json',rec);record['sides'][side]=rec
                            continue
                        graft_many(project,generator.root,successful,config['assets']['bindings'],candidate_project)
                        if integration_planned:
                            # Validate only the merged game, not an intermediate
                            # asset-only game that will never be promoted.
                            pending_assets[diagnosis['id']]={'project':candidate_project,
                                'record':rec,'directory':sd,'generations':generations,
                                'diagnosis':diagnosis}
                            rec.update(action='pending',reason='Waiting for one merged game replay',
                                       before=evaluation['scores'])
                            write_json(sd/'result.json',rec);record['sides'][side]=rec
                            continue
                    elif composition_mode=='codex':
                        draft_ids=({x['id'] for x in _issue_items(parallel_code['diagnosis'])}
                                   if parallel_code else set())
                        current_ids={x['id'] for x in _issue_items(diagnosis)}
                        if parallel_code and draft_ids==current_ids:
                            if 'error' not in parallel_code:
                                try:
                                    _combine_parallel_edit(parallel_code['baseline'],parallel_code['project'],
                                                           repair_project,candidate_project)
                                    edit=parallel_code['edit']
                                    rec['parallel_edit']=True
                                except ValueError as exc:
                                    rec['parallel_fallback']=str(exc)
                            else:
                                rec['parallel_fallback']=parallel_code['error']
                            if not rec.get('parallel_edit'):
                                edit=code_editor(repair_project,candidate_project,sd,diagnosis=diagnosis,
                                                 evaluation=editor_evaluation,episodes=editor_episodes,
                                                 history=history,adapter=adapter,policy=edit_policy)
                        else:
                            # Rebuild a code draft when no compatible parallel
                            # draft is available for this diagnosis.
                            edit=code_editor(repair_project,candidate_project,sd,diagnosis=diagnosis,evaluation=editor_evaluation,
                                             episodes=editor_episodes,history=history,adapter=adapter,policy=edit_policy)
                        rec['edit']=edit
                        signature=json.dumps(digest(candidate_project),sort_keys=True)
                        if (not edit['changed_files'] and not pending_context) or signature in seen_compositions:
                            rec.update(action='reject',reason='No code change or repeated code candidate')
                            write_json(sd/'result.json',rec);record['sides'][side]=rec
                            continue
                        seen_compositions.add(signature)
                    else:
                        edit=services.parameters(repair_project,composition,diagnosis,history)
                        write_json(sd/'proposal.json',edit);rec['edit']=edit
                        signature=json.dumps({'parent':digest(repair_project),'changes':edit['changes']},sort_keys=True)
                        if signature in seen_compositions:
                            rec.update(action='reject',reason='Repeated composition candidate')
                            write_json(sd/'result.json',rec);record['sides'][side]=rec
                            continue
                        seen_compositions.add(signature)
                        snapshot(repair_project,candidate_project)
                        apply_parameters(candidate_project,composition,edit['changes'])
                    codex_game_code=side=='composition' and composition_mode=='codex'
                    comparison_diagnosis=diagnosis
                    if side=='composition' and pending_context:
                        comparison_diagnosis=_batch_diagnosis(
                            _issue_items(pending_context['diagnosis'])+_issue_items(diagnosis),
                            'composition')
                    evidence_dir=sd/('candidate_trajectory' if profile=='initialization' else 'candidate_playtest')
                    after_runs=replay_candidate(candidate_project,world_runs,evidence_dir,
                        diagnosis=comparison_diagnosis,changes=rec,config=config)
                    rec['trajectory']=str(evidence_dir/'result.json')
                    if codex_game_code and profile!='initialization':
                        rec['behavior_check']=verify_behavior_non_regression(world_runs,after_runs,adapter,config)
                    if codex_game_code and training:
                        # Report outcomes only: independently authored policies
                        # are not a controlled measurement of skill improvement.
                        rec['training_behavior_check']=verify_behavior_non_regression(
                            training,after_runs[:len(training)],adapter,config)
                    # The merged game's own verified replay is the sole
                    # candidate evidence. One A/B review decides promotion;
                    # there is no intermediate gate or issue-by-issue judge.
                    reviews=services.selection(config,adapter,comparison_diagnosis,
                                               world_runs,after_runs,max_reviews=1)
                    if len(reviews)!=1:raise ValueError('One replay requires exactly one A/B review')
                    write_json(sd/'pairwise_selection.json',reviews)
                    ok,reason=promotion(reviews)
                    rec['pairwise_reviews']=reviews
                    rec['repair_outcome']='improved' if ok else 'unverified' if (
                        not reviews[0].get('evidence_valid') or
                        reviews[0].get('preferred_role')=='unavailable') else 'not_improved'
                    unverified=bool(case) and rec['repair_outcome']=='unverified'
                    rec.update(action='accept' if ok else 'retained_unverified' if unverified else 'reject',
                               reason=reason,before=evaluation['scores'],
                               comparison=reviews[0])
                    if case:rec.update(candidate=str(candidate_project),evidence=str(evidence_dir))
                    if ok:
                        project=candidate_project;world_runs=after_runs
                        if side=='composition' and pending_key in pending_assets:
                            pending=pending_assets.pop(pending_key);asset_rec=pending['record']
                            asset_rec.update(action='accept',reason='merged game candidate accepted',
                                             comparison=reviews[0],
                                             candidate=str(candidate_project),
                                             evidence=str(evidence_dir))
                            write_json(pending['directory']/'result.json',asset_rec)
                            for item in pending['generations']:
                                if item['status']=='generated' and item.get('video_skill_patch'):
                                    from gamecraft_video_skill import commit_patch
                                    evidence={'phase':profile,'round':index,'asset':item['asset'],
                                              'before':asset_rec['before'],'after':reviews[0]}
                                    commit_patch(generator.root,item['video_skill_patch'],evidence=evidence)
                                    if execution['submit_global_video_skill'] and global_video_skill_store:
                                        from gamecraft_video_skill import submit_global_candidate
                                        submit_global_candidate(global_video_skill_store,item['video_skill_patch'],
                                                                task_id=adapter.task_id,evidence=evidence)
                        if side=='asset':
                            for item in generations:
                                if item['status']!='generated' or not item.get('video_skill_patch'):continue
                                from gamecraft_video_skill import commit_patch
                                evidence={'phase':profile,'round':index,'asset':item['asset'],
                                          'before':rec['before'],'after':reviews[0]}
                                commit_patch(generator.root,item['video_skill_patch'],evidence=evidence)
                                if execution['submit_global_video_skill'] and global_video_skill_store:
                                    from gamecraft_video_skill import submit_global_candidate
                                    submit_global_candidate(global_video_skill_store,item['video_skill_patch'],
                                                            task_id=adapter.task_id,evidence=evidence)
                    else:
                        if side=='asset':
                            from gamecraft_video_skill import record_patch
                            for item in generations:
                                if item['status']=='generated' and item.get('video_skill_patch'):
                                    record_patch(generator.root,item['video_skill_patch'],
                                                 status='unverified' if unverified else 'rejected',
                                                 evidence={'phase':profile,'round':index,'asset':item['asset'],'reason':reason})
                        if diagnosis['route']=='both' and side=='asset':
                            blocked_both.update(x['id'] for x in _issue_items(diagnosis) if x['route']=='both')
                    if side=='composition' and pending_key in pending_assets and not ok:
                        pending=pending_assets.pop(pending_key);asset_rec=pending['record']
                        asset_rec.update(action='retained_unverified' if unverified else 'reject',
                                         reason='asset candidate retained without promotion: composition evidence insufficient'
                                                if unverified else 'asset candidate not committed because composition integration failed')
                        write_json(pending['directory']/'result.json',asset_rec)
            except Exception as exc:
                rec.update(action='error',reason=f'{type(exc).__name__}: {exc}')
                if side=='asset' and diagnosis is not None and diagnosis.get('route')=='both':
                    blocked_both.update(x['id'] for x in _issue_items(diagnosis) if x['route']=='both')
                if side=='composition' and diagnosis is not None and pending_key in pending_assets:
                    pending=pending_assets.pop(pending_key);asset_rec=pending['record']
                    asset_rec.update(action='reject',reason='asset candidate not committed because composition errored')
                    write_json(pending['directory']/'result.json',asset_rec)
            write_json(sd/'result.json',rec);record['sides'][side]=rec
            print(f'[round {index}] {side}: {rec["action"]}',flush=True)
        for pending in pending_assets.values():
            asset_rec=pending['record']
            asset_rec.update(action='reject',reason='asset candidate not committed because integration was not evaluated')
            write_json(pending['directory']/'result.json',asset_rec)
        if video_skill:
            from gamecraft_video_skill.decisions import learn, round_case
            learning_case=round_case(repair_plan,record)
            if learning_case:
                problems,decisions,outcome,learning_evidence=learning_case
                try:
                    record['video_skill_decisions_learned']=learn(
                        generator.root,task_id=adapter.task_id,phase=profile,
                        problems=problems,decisions=decisions,outcome=outcome,
                        evidence=learning_evidence,global_store=global_video_skill_store)
                except Exception as exc:
                    record['video_skill_learning_error']=f'{type(exc).__name__}: {exc}'
        record['project_after']=str(project)
        if selected_recordings is not None:
            selected_recordings.remember(project,case,world_runs,local_round)
        if execution['behavior_enabled'] or init_player is not None:
            # Save only the selected version's evidence, not a rejected policy.
            # This is acceptance evidence, not the final learned-skill evaluation.
            evidence={'project':str(project),'episodes':world_runs,
                      'purpose':'selected_version_evidence'}
            write_json(rd/'selected_playtest.json',evidence)
            write_json(out/'current_playtest.json',evidence)
        history.append(record)
        write_json(rd/'result.json',record);write_json(out/'history.json',history)
        if not execution['behavior_enabled']:
            if init_player is not None:
                init_player.remember(project,case,world_runs)
                init_player.save(out/'selected_initialization_playtests.json')
            else:
                trajectories.adopt(cases,world_runs)
                write_json(out/'current_scenarios.json',cases)
                remember_scripted_replay(project,scripted_tasks,world_runs)
        if case:
            unverified=not repair_plan['coverage_passed']
            for side in ('asset','composition'):
                side_rec=record['sides'][side]
                if side_rec['action']=='retained_unverified':unverified=True
            remaining=repair_plan['issues']
            # An accepted repair is judged again as the next round's incumbent.
            # These round reports describe observed coverage; there is no
            # post-optimization quality gate.
            accepted_world=any(record['sides'][side]['action']=='accept' for side in ('asset','composition'))
            no_observed_issues=(not remaining and not repair_plan.get('missing_evidence')
                                and not unverified and not accepted_world)
            status=('no_observed_issues' if no_observed_issues else 'unverified' if unverified else
                    'optimized' if local_round==rounds else 'optimizing')
            scene_report={'scene':case['id'],'rounds_completed':local_round,'max_rounds':rounds,
                'status':status,'project':str(project),'unresolved_issues':remaining,
                'missing_evidence':repair_plan.get('missing_evidence',[]),
                'last_round':str(rd)}
            scene_reports[case['id']]=scene_report
            write_json(scene_dir/'result.json',scene_report)
            write_json(out/'scene_results.json',list(scene_reports.values()))
            # Incomplete evidence must not consume the remaining round budget
            # after an evidenced partial repair was actually committed. A scene
            # with no accepted progress still stops instead of repeating the
            # same unexercised check without new evidence.
            stop_scene=no_observed_issues or (unverified and not accepted_world)
            if (initialization_behavior_skill and behavior['action']=='adopt_unverified'
                    and local_round<rounds):
                stop_scene=False
            if stop_scene:finished_scenes.add(case['id'])
    config.pop('_initialization_scene',None)
    final_recordings=None
    showcase_result=None
    if program_initialization:
        from .recordings import SelectedRecordings
        final_recordings=SelectedRecordings(out/'final_recordings',
            [unit['id'] for unit in init_units])
        write_json(out/'progress.json',{'stage':'final_playtests','groups':len(init_units)})
        final_episodes=init_player.record_final(project,out/'final_playtests',skill=skill)
        for unit in init_units:
            final_recordings.remember(project,unit,final_episodes[unit['id']],rounds+1)
        final_recordings.finish(project)
        if final_showcase:
            from . import final_showcase as showcase
            write_json(out/'progress.json',{'stage':'final_showcase'})
            requirements=json.loads((out/'initial/requirements.json').read_text())['requirements']
            previous=[{'group':unit['id'],'goal':unit['goal'],
                       'final_state':[episode.get('replay',{}) for episode in final_episodes[unit['id']]]}
                      for unit in init_units]
            baseline_groups=[]
            for unit in init_units:
                before=bootstrap_episodes[unit['id']]
                after=final_episodes[unit['id']]
                if len(before)!=len(after):
                    raise RuntimeError(f'Bootstrap scene count changed: {unit["id"]}')
                for episode_index,(bootstrap,final) in enumerate(zip(before,after),1):
                    baseline_groups.append({
                        'id':unit['id'] if len(before)==1 else f'{unit["id"]}_{episode_index:02d}',
                        'goal':unit['goal'], 'scenario':bootstrap['scenario'],
                        'normal_launch':bool(bootstrap.get('normal_launch')),
                        'bootstrap_video':bootstrap['video'], 'final_record':final})
            showcase_result=showcase.run(project,out/'final_showcase',adapter=adapter,
                scenarios=cases,requirements=requirements,previous=previous,skill=skill,
                baseline_groups=baseline_groups,
                godot=godot,model=player_model,backend=player_backend,
                step_frames=step_frames,policy_factory=init_player.factory,
                rollout=rollout_fn,
                planner=getattr(services,'plan_final_showcase',showcase.plan),
                assessor=getattr(services,'assess_final_showcase',showcase.assess))
            if showcase_result.get('bootstrap_scene_floor_passed') is not True:
                raise RuntimeError('Final showcase did not verify the Bootstrap scene coverage floor')
    if profile=='initialization':write_json(out/'progress.json',{'stage':'final_packaging'})
    # RSI reports the last train rollout without another Player call. It predates
    # any world edit in that round and is not an evaluation of the final project.
    test=[]
    score=None
    (out/'best_skill.txt').write_text(skill)
    errors=[{'round':r['round'],'side':s,'reason':d['reason']} for r in history for s,d in r['sides'].items() if d['action']=='error']
    result={'profile':profile,'project':str(project),'skill':str(out/'best_skill.txt'),'test':score,'errors':errors,
            'state':'completed_with_errors' if errors else 'completed','official_benchmark_metric':False}
    if execution['behavior_enabled']:
        result['run_objective']=run_objective
        result['train_completion']=[r.get('completion') for r in last_training]
        result['train']=aggregate(last_training,adapter,config)
        result['final_project_evaluated']=False
        result['selected_playtest']=str(out/'current_playtest.json')
    if video_skill:
        result['video_skill']=str(generator.root/'video_skill/local.json')
    if profile=='initialization':
        result['initialization_playtest']=initialization_playtest
        result['initialization_behavior_skill']=initialization_behavior_skill
        result['selected_recordings']=str(selected_recordings.finish(project))
        if bootstrap_recordings is not None:
            result['bootstrap_recordings']=str(bootstrap_recordings.directory/'manifest.json')
        if final_recordings is not None:
            result['final_recordings']=str(final_recordings.directory/'manifest.json')
        if init_player is not None:
            result['selected_playtest']=str(out/'selected_initialization_playtests.json')
        result['scenes']=list(scene_reports.values())
        result['final_acceptance']='not_run'
        result['test_scope']='not_run'
        if initialization_scene:
            result['initialization_scope']='single_scene'
        else:
            if initialization_mode=='whole_game':result['initialization_scope']='whole_game'
            final_draft=out/'final-draft'
            shutil.copytree(out/'initial',final_draft,ignore=shutil.ignore_patterns('.godot','__pycache__','.git','checks'))
            shutil.rmtree(final_draft/'game');shutil.copytree(project,final_draft/'game')
            write_json(final_draft/'scenarios.json',cases)
            # Preserve declared inputs without claiming they were replayed on
            # the final version. Later RSI or benchmark collection supplies
            # actual final-version gameplay evidence.
            demo_dir=final_draft/'game/demo_outputs'
            if demo_dir.exists():shutil.rmtree(demo_dir)
            if showcase_result is not None:
                shutil.copytree(out/'final_showcase/demo_outputs',demo_dir)
                demo_manifest={k:v for k,v in showcase_result.items() if k!='attempts'}
                showcase_archive=final_draft/'final_showcase'
                showcase.archive(out/'final_showcase',showcase_archive,showcase_result)
            else:
                demo_manifest=trajectories.export_declared_demos(cases,adapter,demo_dir)
            if init_player is not None:
                # Preserve the selected policies with their original source
                # versions; final-version replays are archived separately.
                archive=final_draft/'initialization_playtests'
                if archive.exists():shutil.rmtree(archive)
                archive.mkdir()
                saved=json.loads((out/'selected_initialization_playtests.json').read_text())
                for group_index,entry in enumerate(saved['groups'].values()):
                    for episode_index,episode in enumerate(entry['episodes']):
                        if episode.get('player_policy'):
                            target=archive/f'group-{group_index:03}-policy-{episode_index:03}.py'
                            shutil.copyfile(episode['player_policy'],target)
                            episode['archived_policy']=str(target.relative_to(final_draft))
                write_json(archive/'selected.json',saved)
                if program_initialization:
                    shutil.copyfile(out/'player_groups.json',archive/'groups.json')
                if showcase_result is None:
                    demo_manifest['scope']=('Original declared input scripts only; selected version-owned playtests '
                        'are in initialization_playtests/. Final-version Player replays are in final_recordings/.')
            write_json(final_draft/'trajectory_manifest.json',demo_manifest)
            recording_archive=final_draft/'initialization_recordings'
            if recording_archive.exists():shutil.rmtree(recording_archive)
            shutil.copytree(selected_recordings.directory,recording_archive)
            if bootstrap_recordings is not None:
                bootstrap_archive=final_draft/'bootstrap_recordings'
                if bootstrap_archive.exists():shutil.rmtree(bootstrap_archive)
                shutil.copytree(bootstrap_recordings.directory,bootstrap_archive)
            if final_recordings is not None:
                final_archive=final_draft/'final_recordings'
                if final_archive.exists():shutil.rmtree(final_archive)
                shutil.copytree(final_recordings.directory,final_archive)
            result['benchmark_demos']=demo_manifest
            write_json(final_draft/'asset_plan.json',plan)
            write_json(final_draft/'training.json',{k:v for k,v in config.items() if not k.startswith('_')})
            write_json(final_draft/'asset_bindings.json',config['assets']['bindings'])
            if video_skill:
                target=final_draft/'video_skill'
                if target.exists():shutil.rmtree(target)
                shutil.copytree(generator.root/'video_skill',target)
            if initialization_behavior_skill:
                (final_draft/'behavior_skill.txt').write_text(skill)
            metadata=json.loads((final_draft/'initialization.json').read_text())
            metadata.update(state='draft',version=metadata['version']+'-evolved')
            metadata.pop('files',None);write_json(final_draft/'initialization.json',metadata)
            frozen=seal_without_replay(final_draft,out/'initialized-package')
            result['package']=str(frozen)
            if initialization_behavior_skill:
                result['skill']=str(frozen/'behavior_skill.txt')
            result['selected_recordings']=str(frozen/'initialization_recordings/manifest.json')
            if bootstrap_recordings is not None:
                result['bootstrap_recordings']=str(frozen/'bootstrap_recordings/manifest.json')
                bootstrap_manifest=json.loads((frozen/'bootstrap_recordings/manifest.json').read_text())
                result['bootstrap_benchmark_videos']=[
                    str(frozen/'bootstrap_recordings'/episode['files']['video']['path'])
                    for unit in init_units
                    for episode in bootstrap_manifest['groups'][unit['id']]['episodes']]
            if final_recordings is not None:
                result['final_recordings']=str(frozen/'final_recordings/manifest.json')
                final_manifest=json.loads((frozen/'final_recordings/manifest.json').read_text())
                result['final_benchmark_videos']=[
                    str(frozen/'final_recordings'/episode['files']['video']['path'])
                    for unit in init_units
                    for episode in final_manifest['groups'][unit['id']]['episodes']]
            if showcase_result is not None:
                result['showcase_manifest']=str(frozen/'final_showcase/manifest.json')
                result['showcase_benchmark_videos']=[
                    str(frozen/'final_showcase'/entry['archived_video'])
                    for entry in showcase_result['demos']]
            result['selected_project']=str(project)
            result['project']=str(frozen/'game')
        if global_video_skill_store:
            from gamecraft_video_skill import merge_global_candidates
            # Accepted local repairs propose transferable rules, but cannot
            # validate those rules on the same games that produced them.
            try:
                result['global_pending_candidates']=merge_global_candidates(global_video_skill_store)
            except Exception as exc:
                result['global_merge_error']=f'{type(exc).__name__}: {exc}'
    inspect_package(package)  # Frozen source must still be intact.
    write_json(out/'result.json',result)
    if profile=='initialization':write_json(out/'progress.json',{'stage':result['state'],
               'project':str(project),'result':str(out/'result.json')})
    print(json.dumps(result),flush=True)
    return result
