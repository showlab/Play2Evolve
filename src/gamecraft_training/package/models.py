"""Shared model calls. Tasks supply criteria, asset requirements and parameters."""
import json
import os
from pathlib import Path
from .evidence import content, outcome_content, sample


def call(system,data,images,name,properties,tokens=1400,*,backend='local',model='local'):
    import llm
    schema={'type':'object','additionalProperties':False,'required':list(properties),'properties':properties}
    return llm.call(model,system,[{'type':'input_text','text':json.dumps(data)}]+images,
                    name,schema,backend=backend,max_tokens=tokens,
                    effort=os.environ.get('GAMECRAFT_CODEX_REASONING_EFFORT') if backend=='codex' else None)


def judge_call(system,data,images,name,properties,tokens=1400):
    """Use the independently configured Judge while keeping the acting Player local."""
    backend=os.environ.get('GAMECRAFT_JUDGE_BACKEND','local')
    model=os.environ.get('GAMECRAFT_JUDGE_MODEL','local')
    return call(system,data,images,name,properties,tokens,backend=backend,model=model)


def _behavior_history(history):
    """Retain attempt outcomes without leaking prior private-state judgments."""
    rows=[]
    for record in history:
        behavior=record.get('sides',{}).get('behavior',{})
        verification=behavior.get('verification') or {}
        rows.append({'round':record.get('round'),'outcome':behavior.get('action'),
                     'execution_status':verification.get('status')})
    return rows


def skill(adapter,current,results,history):
    return judge_call('Improve the player skill using the supplied game rules and observed episodes. '
                'Return a complete replacement skill with concise, nonredundant guidance. '
                'Reasons are fallible interpretations; use observed states as evidence. Do not change game rules or hardcode seeds/action sequences. '
                'Every trigger in the skill and acceptance checks must be observable in a screenshot or the supplied public Player state. '
                'Never refer to private telemetry, replay-only fields, hidden timers, internal variables, or a scenario name as a decision trigger. '
                'Refer to actions only by the supplied semantic action IDs. Never write keyboard keys, button letters, '
                'or instructions such as "press W"; write the exact action ID such as `dodge_high` instead. '
                'Also provide short, general acceptance checks describing observable trigger/action relationships that demonstrate the skill was followed. '
                'Work toward the supplied fixed run objective or initialization group goal without redefining it. '
                'Player completion is a claim; budget exhaustion is truncation. '
                'Checks must be usable in another episode and must not require a better score, win rate, damage, survival time or other game-specific outcome.',
                {'rules':adapter.instructions,'allowed_semantic_action_ids':getattr(adapter,'allowed_actions',list(adapter.actions)),
                 'public_state_fields':list(adapter.observation_fields),
                 'public_state_description':adapter.state_description,
                 'current_skill':current,'previous_attempts':_behavior_history(history),
                 'run_objectives':[r.get('run_objective') for r in results],
                 'initialization_goals':[r.get('initialization_unit',{}).get('goal') for r in results],
                 'episode_completion':[{'completion':r.get('completion'),
                                        'final_public_state':r.get('final_player_state',{})} for r in results]},
                content(results,include_reasons=True,player_visible=True),
                'package_skill',{'reason':{'type':'string'},'skill':{'type':'string'},
                 'acceptance':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'string'}}},tokens=1800)


def _behavior_trace(results):
    """Trace limited to observations available to the acting Player."""
    rows=[]
    for episode_index,result in enumerate(results):
        decisions=[]
        for decision in result['decisions']:
            decisions.append({'frame':decision['frame'],'action':decision['action'],'reason':decision.get('reason',''),
                              'pointer':decision.get('pointer'), 'state':decision.get('player_state',{})})
        rows.append({'scenario':f'episode-{episode_index:03d}','decisions':decisions})
    return rows


def _runtime_trace(results):
    """Complete deterministic telemetry for non-behavior program verification."""
    rows=[]
    for result in results:
        decisions=[]
        for decision in result['decisions']:
            decisions.append({'frame':decision['frame'],'action':decision['action'],'reason':decision.get('reason',''),
                              'pointer':decision.get('pointer'),
                              'state':decision.get('evaluation_state',decision.get('player_state',{}))})
        rows.append({'scenario':result['scenario'],
                     'launch':'default' if result.get('normal_launch') else 'scenario',
                     'replay_verified':bool(result.get('replay_verified')),
                     'trace_recorded':bool(result.get('trace')),
                     'video_recorded':bool(result.get('video')),
                     'final_state':result.get('replay',{}),'run_objective':result.get('run_objective'),
                     'completion':result.get('completion'),'decisions':decisions})
    return rows


def _project_artifacts(project):
    """Bounded facts about files omitted from editable project source context."""
    root=Path(project)
    demos=sorted((root/'demo_outputs').glob('*.json')) if (root/'demo_outputs').is_dir() else []
    rows=[]
    for path in demos[:64]:
        try:
            data=json.loads(path.read_text())
            events=data.get('events',[])
            frames=[event.get('frame') for event in events]
            duration=data.get('duration_frames')
            rows.append({'file':path.name,'duration_frames':duration,'event_count':len(events),
                         'event_types':sorted({event.get('type','') for event in events}),
                         'keycodes':sorted({str(event['keycode']) for event in events if 'keycode' in event}),
                         'frames_ordered':all(isinstance(frame,int) for frame in frames) and frames==sorted(frames),
                         'frames_within_duration':isinstance(duration,int) and all(
                             isinstance(frame,int) and 0<=frame<duration for frame in frames),
                         'mouse_events_have_coordinates':all(
                             isinstance(event.get('x'),(int,float)) and isinstance(event.get('y'),(int,float))
                             for event in events if event.get('type') in ('mouse_click','mouse_down','mouse_move','mouse_up'))})
        except (OSError,ValueError,TypeError,AttributeError) as exc:
            rows.append({'file':path.name,'parse_error':str(exc)[:200]})
    directory_names=sorted(path.name for path in root.iterdir() if path.is_dir())
    return {'project_godot_exists':(root/'project.godot').is_file(),
            'directories':{name:(root/name).is_dir() for name in
                           ('assets','demo_outputs','scenes','scripts')},
            'top_level_directories':directory_names[:64],
            'top_level_directories_truncated':len(directory_names)>64,
            'demo_file_count':len(demos),'demo_files_truncated':len(demos)>len(rows),
            'demo_files':rows}


def _initialization_context(config):
    """Keep public requirements available to the router without a separate gate."""
    package=config.get('_package_root')
    scope=config.get('_initialization_scene') or {}
    requirements=scope.get('requirements')
    brief=''
    if package:
        root=Path(package)
        if (root/'instruction.md').is_file():
            brief=(root/'instruction.md').read_text()
        if requirements is None and (root/'requirements.json').is_file():
            requirements=json.loads((root/'requirements.json').read_text()).get('requirements',[])
    return {'original_brief':brief,'requirements':requirements or []}


def _rsi_playtest_context(config, project, results):
    """Bounded game-design context for the world Judge, not the acting Player."""
    package = config.get('_package_root')
    requirements_path = Path(package) / 'requirements.json' if package else None
    requirements = []
    if requirements_path and requirements_path.is_file():
        requirements = [{'id': row['id'], 'description': row['description']}
                        for row in json.loads(requirements_path.read_text()).get('requirements', [])]
    sources = []
    remaining = 24000
    for path in sorted(Path(project).rglob('*.gd')):
        if remaining <= 0 or len(sources) == 16:
            break
        excerpt = path.read_text(errors='replace')[:min(8000, remaining)]
        sources.append({'file': path.relative_to(project).as_posix(), 'text': excerpt})
        remaining -= len(excerpt)
    episodes = []
    for result in results:
        decisions = result.get('decisions') or []
        indexes = sorted({round(i * (len(decisions) - 1) / 5) for i in range(6)}) if decisions else []
        episodes.append({'scenario': result.get('scenario'),
                         'decision_frames': result.get('decision_frames'),
                         'run_objective': result.get('run_objective'),
                         'completion': result.get('completion'),
                         'final_state': result.get('replay', {}),
                         'checkpoints': [
                             {'frame': decisions[i]['frame'], 'action': decisions[i]['action'],
                              'state': decisions[i].get('evaluation_state', {})} for i in indexes]})
    return {'declared_requirements': requirements, 'game_code': sources,
            'exploration_episodes': episodes}


def behavior(adapter,proposal,candidate_results):
    """Verify whether the candidate executes the proposed skill when its triggers occur."""
    evidence={'type':'array','items':{'type':'object','additionalProperties':False,
        'required':['scenario','frame','observation'],'properties':{
        'scenario':{'type':'string'},
        'frame':{'type':'integer','minimum':0},'observation':{'type':'string'}}}}
    answer=judge_call(
        'Verify whether the candidate player follows the proposed reusable skill. Use actions, chronological states, '
        'reasons and screenshots as evidence. Evaluate instruction execution only: '
        'Use only the screenshot and supplied public Player state. Never infer a violation from private telemetry, '
        'replay-only fields, hidden timers, internal variables, or the scenario name. '
        'do not require or reward higher win rate, score, damage, survival time, HP, stamina, speed or any other game-specific outcome. '
        'Return followed when the shown triggers occur and the candidate consistently performs the required semantic actions. '
        'Return not_followed for delayed, opposite, missing, illegal or contradictory actions. Return not_exercised when '
        'the supplied episodes never show enough of the declared triggers. A critical violation means an invalid action, '
        'an explicit rule violation, or persistent behavior opposite to the skill.',
        {'rules':adapter.instructions,'public_state_fields':list(adapter.observation_fields),
         'public_state_description':adapter.state_description,'candidate_skill':proposal['skill'],
         'acceptance_checks':proposal.get('acceptance',[]),
         'candidate_traces':_behavior_trace(candidate_results)},
        content(candidate_results,limit=16,include_reasons=True,player_visible=True),
        'package_skill_execution',{
         'status':{'type':'string','enum':['followed','not_followed','not_exercised']},
         'critical_violation':{'type':'boolean'},
         'reason':{'type':'string'},'evidence':evidence},tokens=2200)
    return answer


def judge(config,adapter,project,results):
    ids=[c['id'] for c in config['quality']['criteria']]
    extra=getattr(adapter,'quality_measurements',lambda *_:{})(project,results)
    props={k:{'type':'number','minimum':0,'maximum':1} for k in ids}
    props['reason']={'type':'string'}
    props['findings']={'type':'object','additionalProperties':False,
                       'required':ids,'properties':{k:{'type':'string'} for k in ids}}
    initialization=config.get('_profile',{}).get('name')=='initialization'
    if not initialization:
        aspects=('rules_controls','challenge_strategy','content_progression',
                 'feedback_readability','visual_presentation',
                 'aesthetic_richness','gameplay_richness')
        props['playtest_aspects']={'type':'object','additionalProperties':False,
                                  'required':list(aspects),
                                  'properties':{name:{'type':'string'} for name in aspects}}
        props['playtest_findings']={'type':'array','maxItems':6,'items':{
            'type':'object','additionalProperties':False,
            'required':['kind','problem','evidence'],
            'properties':{
                'kind':{'type':'string','enum':['gameplay_bug','progression_design','content_depth','feedback','visual']},
                'problem':{'type':'string'},'evidence':{'type':'string'}}}}
        props['playtest_limitations']={'type':'array','items':{'type':'string'}}
    goal=(
        'This is initialization quality assessment. Judge functional scene-role correctness, material completeness, basic '
        'playability and visible state feedback. Do not reward optional beauty, stylistic polish, subtle anatomy, '
        'smoothness, balance or player strategy; those belong to later RSI. A missing required actor, prop, screen, '
        'action state or essential feedback is severe. '
        if initialization else
        'This is RSI playtesting. Check the fixed overall objective against observed evidence; '
        'Player success/failure reports are claims, not ground truth. Budget exhaustion is truncation, '
        'not success or definitive defeat. An intermediate stage win is not overall completion. '
        'Judge the observed game experience across five equally considered aspects: '
        '(1) rules, controls and state transitions; (2) challenge and meaningful strategy, including whether '
        'skilled play becomes trivial or repetitive; (3) content variety and progression, including whether '
        'continued play brings new situations; (4) feedback and readability; (5) visual presentation and art. '
        'Separately report gameplay_richness: whether repeated core-loop encounters create meaningfully different '
        'situations or decisions, not merely higher numbers or cosmetic changes. Separately report '
        'aesthetic_richness: variation in authored subjects, motion, composition and atmosphere across the '
        'observed play, not merely whether existing assets are technically clean. These two reports are '
        'diagnostic dimensions, not extra scores or independent repair tracks. '
        'A coherent, defect-free asset can still be reused so often that the overall experience feels visually flat. '
        'Report evidenced repetition or concrete missed design potential in the observed loop as a finding '
        'even when no existing asset is broken; '
        'do not prescribe a particular repair medium or reward variety for its own sake. '
        'For every playtest_aspects field, briefly state what the observed play supports or say unverified. '
        'Report concrete issues in playtest_findings without a fixed category priority. Distinguish game '
        'problems from Player mistakes. Mark unvisited or too-short-to-judge states in playtest_limitations; '
        'do not call them good or broken. Source code may corroborate but cannot replace observed play. ')
    answer=judge_call(goal+'Judge the supplied game interaction against each criterion. Score 0–1 using the visible evidence, '
                'where 1 means free of evident problems for this profile, 0.5 means noticeable defects, and 0 means '
                'severe or pervasive defects. '
                'also inspect for concrete artifacts and basic visual correctness: missing or duplicated actors, '
                'green-screen spill, broken silhouettes, clipping, inconsistent scale or grounding, mismatched poses, '
                'incorrect contact/feedback, scene-role errors, and unreadable UI. Judge each attached scenario and '
                'timepoint; do not infer quality in states that were not shown. Do not give a perfect score merely '
                'because the game builds or the HUD is readable. Give a short evidence-based finding for every '
                'criterion, naming the scenario and visible problem or explaining what the supplied frames support.',
                {'criteria':config['quality']['criteria'],'measurements':extra,
                 'active_scene':config.get('_initialization_scene'),
                 **({} if initialization else {'playtest_context':_rsi_playtest_context(config,project,results)})},
                content(results,limit=config.get('_initialization_scene',{}).get('max_frames',12),
                        scene_focus=not initialization or bool(config.get('_initialization_scene')))+outcome_content(results),
                'package_quality',props)
    import math
    if any(type(answer[k]) not in (int,float) or not math.isfinite(answer[k]) or not 0<=answer[k]<=1 for k in ids):
        raise ValueError('Judge returned invalid scores')
    return {'build_ok':True,'errors':[],'num_demos':len(results),'scores':{k:answer[k] for k in ids},
            'findings':answer['findings'],'reason':answer['reason'],'measurements':extra,
            **({} if initialization else {'playtest_aspects':answer['playtest_aspects'],
                                         'playtest_findings':answer['playtest_findings'],
                                         'playtest_limitations':answer['playtest_limitations']})}


def material_content(config,project,plan,limit=24):
    """Show bounded chronological source samples for source/integration attribution."""
    import llm
    from .config import relative
    rows=[];count=0
    for asset in plan['assets']:
        files=config['assets']['bindings'][asset['id']]
        indexes=sorted({round(i*(len(files)-1)/min(3,len(files)-1)) for i in range(min(4,len(files)))}) if len(files)>1 else [0]
        for index in indexes:
            path=relative(project,files[index])
            source={'source_asset':asset['id'],'kind':asset['kind'],
                    'frame_index':index,'frame_count':len(files),'file':files[index]}
            if path.suffix.lower() in ('.svg', '.svgz'):
                # Godot can render vectors, but llm.img decodes with PIL. The
                # rendered playtest frames still show this asset in context.
                source['source_preview']='vector; inspect rendered game frames'
                rows.append({'type':'input_text','text':json.dumps(source)})
            else:
                rows.extend(({'type':'input_text','text':json.dumps(source)},
                             llm.img(str(path), jpeg_quality=95)))
            count+=1
            if count>=limit:return rows
    return rows


def route(config,adapter,project,plan,composition,evaluation,results,history):
    """One evidence-grounded diagnosis owns attribution before either repair side runs."""
    assets=[a['id'] for a in plan['assets']]
    scenarios=sorted({r['scenario'] for r in results})
    evidence={'type':'array','minItems':1,'items':{'type':'object','additionalProperties':False,
        'required':['scenario','frame','observation'],'properties':{
        'scenario':{'type':'string','enum':scenarios},'frame':{'type':'integer','minimum':0},
        'observation':{'type':'string'}}}}
    observation={'type':'object','additionalProperties':False,'required':['status','evidence'],
        'properties':{'status':{'type':'string','enum':['clean','defective','unobserved']},
                      'evidence':{'type':'string'}}}
    issue={'type':'object','additionalProperties':False,
        'required':['id','problem','route','asset_ids','source_observation','rendered_observation','evidence','rendered_acceptance'],
        'properties':{'id':{'type':'string'},'problem':{'type':'string'},
        'route':{'type':'string','enum':['asset','composition','both']},
        # Codex structured outputs support only a subset of JSON Schema. Keep
        # uniqueness as an application-level invariant below instead of using
        # the unsupported `uniqueItems` keyword here.
        'asset_ids':{'type':'array','items':{'type':'string','enum':assets}},
        'source_observation':observation,'rendered_observation':observation,'evidence':evidence,
        'rendered_acceptance':{'type':'array','minItems':1,'items':{'type':'string'}}}}
    initialization=config.get('_profile',{}).get('name')=='initialization'
    if not initialization:
        issue['required'].append('kind')
        issue['properties']['kind']={'type':'string','enum':[
            'gameplay_bug','progression_design','content_depth','feedback','visual']}
    goal=(
        'This is initialization: prioritize blocking functional completeness, required material coverage, scene-role '
        'correctness and basic visible feedback from the requirements and observed play. Do not spend a repair on optional beauty. '
        'The original brief is context; diagnose only the active scope. Record unobserved required outcomes in '
        'missing_evidence. This diagnosis is not an absolute requirement acceptance verdict. '
        'A declared script that attempts a required interaction but shows that the mechanic, material, or feedback is '
        'missing is sufficient negative evidence: route that defect for repair. '
        if initialization else
        'This is RSI game playtesting. The Agent is a tester whose improving play can reveal deeper game states. '
        'Consider rules and controls, challenge and strategy, content and progression, feedback and readability, '
        'and visual presentation together. Route concrete observed issues without a fixed category priority. '
        'In particular, check whether skilled play continues to offer new situations and meaningful choices, '
        'or merely repeats an easy loop; do not infer this from a short or unvisited run. Preserve the game\'s '
        'intended goals, not every incumbent difficulty parameter. Distinguish a Player mistake from a game defect. '
        'Consider aesthetic richness and gameplay richness separately from functional correctness: a clean existing '
        'asset does not prove that repeating it across observed play supplies enough visual or interactive variety. '
        'Route a concrete observed plateau as a design issue without assuming that new media is the solution. ')
    scope=config.get('_initialization_scene')
    if scope:
        goal+=('Diagnose ONLY the active scene and its observed activities. Check visual role/action consistency, '
               'actor identity and scale, prop placement, motion and input feedback. A named activity must be '
               'visibly that activity, not a label over unrelated animation. Never diagnose an unshown event as '
               'broken merely because a script was intended to test it. Record absent evidence in missing_evidence. '
               'Do not request fixes to other '
               'scenes. ')
    goal+=('When an observed action should show articulated internal motion but the integrated game visibly '
           'slides or rotates one unchanged static frame, report the missing motion if supported by chronological '
           'frames and source-material evidence. Route it to both material generation and code integration if '
           'new animation frames and runtime playback are both needed. In that case the static source is defective '
           'for the observed action even if its identity and appearance are otherwise correct. Name its asset ID. '
           'Keep world position and collision code-owned. '
           'Do not infer absent motion from a single frame or an unobserved action. ')
    answer=judge_call(
        goal+'Act as the single repair router for a generated game. You receive chronological integrated-game frames '
        'followed by labeled representative source-asset frames. Compare them before assigning root cause. '
        'Coverage is per observed issue, not an all-or-nothing judgment of the whole game or scene. '
        'Set coverage_passed=true when at least one concrete issue has sufficient evidence, even if other required '
        'events are unshown; list those unshown events in missing_evidence without diagnosing them as broken. '
        'Set coverage_passed=false only when no issue can be diagnosed from the supplied interaction. '
        'Screenshots of idle actors cannot prove attacks, contact, defense, reactions or animation timing. '
        'Use the post-action final observation to distinguish an unseen result from an absent result. '
        'For RSI, inspect the chronological playtest checkpoints, declared requirements, code and playtest '
        'findings together. A source-level absence such as one repeated hazard type may support a content defect '
        'only when the observed run also reaches the relevant gameplay; do not claim unvisited late-game states '
        'are defective. A gameplay issue may be composition/code-only with no generated asset. If the defect is '
        'missing a NEW content variant rather than a bad existing asset, route it as composition with no existing '
        'asset ID; the downstream planner can propose a new material binding. Preserve the observed visual or '
        'gameplay richness goal in rendered_acceptance, so the unified planner can compare code-only reuse with '
        'new authored content and gameplay integration. Do not turn an unobserved possibility into a defect. '
        'List only concrete evidenced issues and assign each once: asset means '
        'the source image/video content is intrinsically wrong (identity, anatomy, baked scene, missing motion); '
        'composition means the source can be correct but engine placement, keying, scale, UI, state logic, timing or '
        'gameplay code is wrong; both means a missing/wrong source must be regenerated first and then integrated by '
        'code. asset_ids names only media sources that must be regenerated, not every asset visible in the scene; '
        'describe sound assets used by a composition-only fix in the evidence text and leave asset_ids empty. '
        'source_observation refers to bound image/video material, not Godot source code: for a code-only '
        'gameplay defect mark media clean or unobserved and describe the code evidence in the issue text. '
        'Record source and rendered observations separately as clean, defective, or unobserved. Put observable repair goals for '
        'the final game frames in rendered_acceptance. These are diagnostic guidance, not new mandatory requirements '
        'or an all-or-nothing acceptance gate. Source-file viability is checked before integrated-game comparison. '
        'Never route the same issue independently to both sides. An empty issue list is valid only when the shown '
        'evidence supports it. Preserve game goals.',
        {'rules':adapter.instructions,'criteria':config['quality']['criteria'],'evaluation':evaluation,
         'assets':plan['assets'],'composition_parameters':composition['parameters'],
         'active_scene':scope,
         'code_edit_policy':composition.get('code_edit'),
         'previous_attempts':history[-config.get('_history_windows',{}).get('judge',8):]
         if config.get('_history_windows',{}).get('judge',8) else [],
         **({'public_requirements':_initialization_context(config)} if initialization else
            {'playtest_context':_rsi_playtest_context(config,project,results)})},
        content(results,limit=scope.get('max_frames',64) if scope else 12,include_reasons=True,
                scene_focus=not initialization or bool(scope))+
        outcome_content(results)+
        material_content(config,project,plan,limit=24 if initialization else 4),
        'package_repair_route',{
         'coverage_passed':{'type':'boolean'},'coverage_reason':{'type':'string'},
         'missing_evidence':{'type':'array','items':{'type':'string'}},
         'issues':{'type':'array','maxItems':6,'items':issue}},tokens=2200)
    if not answer['coverage_passed'] and answer['issues']:
        # A partially observed scene can still contain repairable defects. Keep
        # only issues tied to frames we actually supplied, rather than aborting
        # the round or turning unobserved requirements into repair tasks.
        observed={(item['scenario'],item['frame']) for item in
                  sample(results,limit=scope.get('max_frames',64) if scope else 16,
                         scene_focus=bool(scope))}
        for result in results:
            decisions=result.get('decisions') or []
            if decisions and decisions[-1].get('image'):
                final_image=Path(decisions[-1]['image']).with_name(f'frame_{len(decisions):04d}.png')
                if final_image.is_file():
                    observed.add((result['scenario'],decisions[-1]['frame']+
                                  result.get('decision_frames',0)))
        answer['issues']=[item for item in answer['issues']
                          if item['rendered_observation']['status']=='defective' and
                          any((row['scenario'],row['frame']) in observed for row in item['evidence'])]
        if answer['issues']:
            answer['coverage_passed']=True
            answer['coverage_reason']='Partial scene coverage: evidenced issues can be repaired; '+answer['coverage_reason']
    seen=set()
    for item in answer['issues']:
        if not item['id'].strip() or item['id'] in seen or not item['problem'].strip():
            raise ValueError('Repair issues need unique nonempty ids and problems')
        seen.add(item['id'])
        if item['route'] in ('asset','both') and not item['asset_ids']:
            raise ValueError('Asset-routed issue must identify at least one planned asset')
        if len(item['asset_ids']) != len(set(item['asset_ids'])):
            raise ValueError('Repair issue asset ids must be unique')
        if item['route']=='composition' and item['asset_ids']:
            if item['source_observation']['status']=='defective':
                # A defect in both the media source and its use needs both sides.
                item['route']='both'
            else:
                # The model sometimes lists sound assets used by a code-only fix.
                # Those are context, not regeneration targets.
                item['asset_ids']=[]
        if item['route'] in ('asset','both') and item['source_observation']['status']!='defective':
            raise ValueError('Asset route requires visible source evidence')
        if item['route']=='composition' and item['source_observation']['status']=='defective':
            raise ValueError('Defective source cannot be routed only to composition')
    return answer


def plan_candidates(config,adapter,project,plan,composition,diagnosis,results,history):
    from .boundary_planner import plan as grouped_plan
    return grouped_plan(config,adapter,project,plan,composition,diagnosis,results,history)


def plan_repair(config,adapter,project,plan,composition,diagnosis,results,history):
    """One Codex decision for the material and code work in this round.

    The playtest Judge supplies observations. This planner owns the repair route,
    complete H3 prompts, and the code integration brief for each issue.
    """
    if not diagnosis['coverage_passed'] or not diagnosis['issues']:
        return {**diagnosis,'unified':True,'decisions':[]}
    assets={item['id']:item for item in plan['assets']}
    source=[];remaining=60000
    for file in sorted(Path(project).rglob('*.gd')):
        relative=file.relative_to(project).as_posix()
        if len(source)>=30 or remaining<=0:break
        excerpt=file.read_text(errors='replace')[:min(14000,remaining)]
        source.append({'file':relative,'text':excerpt});remaining-=len(excerpt)
    issue_ids=[item['id'] for item in diagnosis['issues']]
    answer=call(
        'You are the ONE repair planner for both generated material and Godot code. The prior visual review is '
        'evidence, not a binding implementation route. Read the game code, asset contracts, source previews and '
        'rendered evidence together. Decide which issues share a root cause and which changes should be made in '
        'one candidate. For each issue choose code, asset, both or skip. Use asset only when replacing the bound '
        'source can solve the visible problem without code changes; use both whenever new material needs placement, '
        'animation, keying, timing or state integration. For every generated asset write a COMPLETE replacement H3 '
        'prompt, not a suffix to the old prompt. Keep generated files within existing asset IDs and frame contracts; '
        'new scene-level bindings are not yet supported. The code brief must tell the editor what new material is '
        'expected and how to check it after generation. Choose the narrowest useful video layer: prop, actor_motion, '
        'scene_motion, or full_scene. If a requested layer cannot fit an existing binding, choose a feasible layer '
        'and explain the limitation. Treat video_skill_planning_guidance as prior hypotheses: apply only when the '
        'current evidence matches a trigger, and treat caution records as failed attempts. '
        'For RSI, a reproducible gameplay or progression issue may require a code-only repair; '
        'preserve core game goals and fair challenge, not every incumbent rule value. '
        'Do not propose changes unsupported by playtest evidence. Return one decision '
        'for every issue; no asset may receive conflicting prompts.',
        {'rules':adapter.instructions,'issues':diagnosis['issues'],'assets':plan['assets'],
         'asset_bindings':config['assets']['bindings'],'code':source,
         'composition':composition,
         'previous_attempts':history[-config.get('_history_windows',{}).get('legacy_planner',5):]
         if config.get('_history_windows',{}).get('legacy_planner',5) else [],
         'video_skill_planning_guidance':config.get('_video_skill_guidance', [])},
        content(results,limit=12,include_reasons=True)+material_content(config,project,plan),
        'package_unified_repair_plan',{
            'decisions':{'type':'array','items':{'type':'object','additionalProperties':False,
                'required':['issue_id','method','layer','reason','asset_prompts','code_instructions'],
                'properties':{
                    'issue_id':{'type':'string','enum':issue_ids},
                    'method':{'type':'string','enum':['code','asset','both','skip']},
                    'layer':{'type':'string','enum':['prop','actor_motion','scene_motion','full_scene']},
                    'reason':{'type':'string'},
                    'asset_prompts':{'type':'array','items':{'type':'object','additionalProperties':False,
                        'required':['asset_id','prompt'],'properties':{
                            'asset_id':{'type':'string','enum':list(assets)},'prompt':{'type':'string'}}}},
                    'code_instructions':{'type':'string'}}}}},tokens=3500,backend='codex',
        model=os.environ.get('GAMECRAFT_REPAIR_PLANNER_MODEL','gpt-6-sol'))
    decisions=answer['decisions']
    if len(decisions)!=len(issue_ids) or {row['issue_id'] for row in decisions}!=set(issue_ids):
        raise ValueError('Repair planner must decide every issue exactly once')
    prompts={}
    updated=[];normalized=[]
    for issue in diagnosis['issues']:
        decision=next(row for row in decisions if row['issue_id']==issue['id'])
        method=decision['method'];requested=decision['asset_prompts']
        code_brief=decision['code_instructions'].strip()
        fallback=''
        if method in ('asset','both') and not requested:
            fallback='No complete H3 prompt was supplied; material generation was skipped'
            method='code' if code_brief else 'skip'
        elif method in ('code','skip') and requested:
            fallback='H3 prompts were supplied despite the chosen method; route normalized to use them'
            method='both' if code_brief else 'asset'
        if method in ('code','both') and not code_brief:
            fallback='No code instructions were supplied; code editing was skipped'
            method='asset' if requested else 'skip'
        normalized.append({**decision,'effective_method':method,'fallback':fallback})
        ids=[]
        for item in requested:
            asset_id=item['asset_id'];prompt=item['prompt'].strip()
            if asset_id in ids or not prompt or len(prompt)>4000:
                raise ValueError('Duplicate, empty or oversized asset prompt')
            if asset_id in prompts and prompts[asset_id]!=prompt:
                raise ValueError('One asset received conflicting H3 prompts')
            ids.append(asset_id);prompts[asset_id]=prompt
        if method!='skip':
            updated.append({**issue,'route':{'code':'composition','asset':'asset','both':'both'}[method],
                            'asset_ids':ids,'code_instructions':code_brief,
                            'video_layer':decision['layer'],
                            'planner_reason':decision['reason']+(' ['+fallback+']' if fallback else '')})
    return {**diagnosis,'issues':updated,'decisions':normalized,'asset_prompts':prompts,'unified':True}


def _paired_repair_content(before_results, after_results, limit=12, labels=('VERSION A','VERSION B'), *, scene_focus=False):
    """Bind both versions and their labels into each comparison image."""
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFont

    def paired_image(a_path,b_path,pair_number):
        with Image.open(a_path) as source:
            a=source.convert('RGB')
        with Image.open(b_path) as source:
            b=source.convert('RGB')
        width=min(640,max(a.width,b.width))
        height=round(max(a.height,b.height)*width/max(a.width,b.width))
        a=a.resize((width,height))
        b=b.resize((width,height))
        header=36
        canvas=Image.new('RGB',(width*2,height+header),(30,30,30))
        canvas.paste(a,(0,header))
        canvas.paste(b,(width,header))
        draw=ImageDraw.Draw(canvas)
        try:
            font=ImageFont.truetype('DejaVuSans-Bold.ttf',20)
        except OSError:
            font=ImageFont.load_default()
        draw.rectangle((0,0,width-1,header-1),fill=(25,74,140))
        draw.rectangle((width,0,width*2-1,header-1),fill=(145,72,24))
        draw.text((14,7),f'{labels[0]}  |  PAIR {pair_number}',font=font,fill='white')
        draw.text((width+14,7),f'{labels[1]}  |  PAIR {pair_number}',font=font,fill='white')
        draw.line((width,0,width,height+header),fill='white',width=3)
        output=io.BytesIO()
        canvas.save(output,format='JPEG',quality=70)
        return {'type':'input_image','detail':'auto',
                'image_url':'data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode()}

    rows=[];pairs=0
    for before,after in zip(before_results,after_results):
        if before['scenario'] != after['scenario']:
            raise ValueError('Repair comparison scenarios do not match')
        count=min(len(before['decisions']),len(after['decisions']))
        if not count:
            continue
        take=min(count,limit) if scene_focus else min(4,count)
        indexes=sorted({round(i*(count-1)/(take-1)) for i in range(take)}) if take>1 else [0]
        for i in indexes:
            if pairs>=limit:break
            left,right=before['decisions'][i],after['decisions'][i]
            label={'scenario':before['scenario'],'decision_index':i,'a_frame':left['frame'],
                   'b_frame':right['frame'],'a_action':left['action'],'b_action':right['action']}
            rows += [{'type':'input_text','text':'MATCHED REPAIR PAIR '+json.dumps(label)+
                      '. The next single image has VERSION A on the left and VERSION B on the right.'},
                     paired_image(left['image'],right['image'],pairs+1)]
            pairs+=1
        if pairs>=limit:break
    return rows


def repair(config,adapter,diagnosis,before_evaluation,after_evaluation,before_results,after_results):
    """Verify the diagnosed defect itself on identical before/after interactions."""
    checks=diagnosis.get('rendered_acceptance',diagnosis.get('acceptance',[]))
    check_schema={'type':'array','minItems':len(checks),'maxItems':len(checks),'items':{
        'type':'object','additionalProperties':False,'required':['criterion','passed','evidence'],
        'properties':{'criterion':{'type':'string','enum':checks},'passed':{'type':'boolean'},
                      'evidence':{'type':'string'}}}}
    regressions={'type':'array','items':{'type':'object','additionalProperties':False,
        'required':['problem','evidence','severity'],'properties':{
            'problem':{'type':'string'},'evidence':{'type':'string'},
            'severity':{'type':'string','enum':['minor','major','critical']}}}}
    answer=judge_call(
        'Blindly compare VERSION A and VERSION B frames from identical interactions. You are not told whether an edit '
        'was attempted. Verify the supplied issue rather than rewarding change. '
        'Judge the diagnosed defect against the original game rules and matched frames. The supplied desired '
        'outcomes are diagnostic guidance, not extra mandatory requirements: report each check, but a candidate can '
        'make valuable partial progress before all outcomes pass. Persistent unrelated defects are not '
        'a reason to fail this repair. coverage_passed requires matched frames that actually show the diagnosed '
        'object/event. Rate VERSION A as before_severity and VERSION B as after_severity; target_change describes '
        'VERSION B relative to VERSION A and is better only for a concrete visible reduction of the diagnosed defect. '
        'Keep the two severity ratings consistent with this comparison. status is gone only when the diagnosed defect '
        'is no longer visible, still_there when it remains, and not_exercised when it is not shown well enough. '
        'Severity is 0 for absent and 1 for maximally severe. List new or materially worsened problems under '
        'regressions. Mark critical only for a broken core interaction, unusable visual, or other blocking failure; '
        'smaller tradeoffs are minor or major. Do not infer hidden quality or reward change alone.',
        {'rules':adapter.instructions,'diagnosis':diagnosis,'quality_criteria':config['quality']['criteria'],
         'before_quality':before_evaluation,'after_quality':after_evaluation},
        _paired_repair_content(before_results,after_results,
                               limit=config['_initialization_scene'].get('max_frames',64) if config.get('_initialization_scene') else 12,
                               scene_focus=bool(config.get('_initialization_scene'))),
        'package_targeted_repair_verification',{
            'status':{'type':'string','enum':['gone','still_there','not_exercised']},
            'target_change':{'type':'string','enum':['better','same','worse','unobserved']},
            'coverage_passed':{'type':'boolean'},'coverage_reason':{'type':'string'},
            'defect_fixed':{'type':'boolean'},
            'before_severity':{'type':'number','minimum':0,'maximum':1},
            'after_severity':{'type':'number','minimum':0,'maximum':1},
            'acceptance_checks':check_schema,'regressions':regressions,'reason':{'type':'string'}},tokens=1800)
    from .repair_verifier import validate
    validate(answer,checks)
    return answer


def _independent_repair_content(left,right,limit=64):
    """Keep each version's complete time span; decision indices are not aligned."""
    if ([r['scenario'] for r in left] != [r['scenario'] for r in right]
            or any(bool(a.get('normal_launch')) != bool(b.get('normal_launch'))
                   for a,b in zip(left,right))):
        raise ValueError('Version-specific trajectories must cover the same scenarios and launch modes')
    rows=[]
    for label,episodes in (('A',left),('B',right)):
        rows.append({'type':'input_text','text':f'VERSION {label}: its own trajectory. '
                     'The following observations and outcomes all belong to this version. '
                     'Times and actions are independent of the other version.'})
        goals=[{'scenario':episode['scenario'],
                'group':episode['initialization_unit']['id'],
                'profile':episode.get('player_profile'),
                'goal':episode['initialization_unit']['goal']}
               for episode in episodes if 'initialization_unit' in episode]
        if goals:
            rows.append({'type':'input_text','text':json.dumps({
                'playtest_goals':goals,
                'scope':'Goals describe intended coverage, not observed success. '
                        'Different programs may play better without improving the game.'})})
        coverage=[{'scenario':episode['scenario'],
                   'status':episode['goal_coverage']['status'],
                   'failed_checks':episode['goal_coverage']['failed']}
                  for episode in episodes if 'goal_coverage' in episode]
        if coverage:
            rows.append({'type':'input_text','text':json.dumps({
                'declared_goal_checks':coverage,
                'scope':'Machine-observable milestones only; inspect frames to judge visual repair.'})})
        rows.extend(content(episodes,limit=max(1,limit//2),scene_focus=True))
        rows.extend(outcome_content(episodes))
    return rows


def selection(config,adapter,diagnosis,incumbent_results,candidate_results,*,max_reviews=2):
    """Compare version-owned trajectories when acceptance used a fresh policy."""
    from .selection import label_review,needs_confirmation
    reviews=[]
    orders=[({'A':'incumbent','B':'candidate'},incumbent_results,candidate_results),
            ({'A':'candidate','B':'incumbent'},candidate_results,incumbent_results)]
    initialization=config.get('_profile',{}).get('name')=='initialization'
    independent=initialization or any(r.get('playtest',{}).get('purpose')=='candidate_acceptance'
                                     for r in incumbent_results+candidate_results)
    focus=('For initialization, prefer visible functional and material completeness in the active scene; '
           'optional beauty is secondary. An unfinished candidate may still be better when the target visibly '
           'improves without a critical regression. ' if initialization else
           'For RSI, compare the observed rules and controls, challenge and strategy, content and progression, '
           'feedback and readability, and visual presentation without a fixed category priority. Assess aesthetic '
           'and gameplay richness through observed variety and meaningful decisions, not asset count or change alone. Preserve the '
           'intended game loop and fair challenge; a higher Agent score alone is not an improvement. Do not call an '
           'unexercised later state fixed. Reject critical playability regressions. ')
    for index,(roles,left,right) in enumerate(orders):
        matched_outcomes={label:[{'scenario':result.get('scenario'),
                                   'final_state':result.get('replay', {}),
                                   'decisions':len(result.get('decisions') or [])}
                                  for result in episodes]
                          for label,episodes in (('A',left),('B',right))}
        protocol=('Each version has its own authored and executed trajectory for the same scenario goals. '
                  'Actions, durations and decision indices may differ; never assume time-aligned events. '
                  'Compare the demonstrated experience and required interaction coverage. Better play alone '
                  'does not prove a code repair, and avoiding an interaction does not prove it fixed. '
                  if independent else 'Both versions replay the same recorded inputs. ')
        if independent and not initialization:
            protocol += ('RSI candidate acceptance policies do not use the learned Behavior Skill; '
                         'the incumbent may have used one. Do not attribute higher scores or longer '
                         'survival to better game code. This is not a player-skill comparison. ')
        limit=config['_initialization_scene'].get('max_frames',64) if config.get('_initialization_scene') else 12
        evidence=(_independent_repair_content(left,right,limit=limit) if independent else
                  _paired_repair_content(left,right,limit=limit,scene_focus=True))
        answer=judge_call(
            focus+protocol+'Choose the stronger game version from anonymous A/B evidence. Judge the diagnosed issue, '
            'overall coherence, readability, feedback, and visible regressions. Do not reward asset count, complexity, '
            'or change itself. Treat diagnostic desired outcomes as guidance, not an all-or-nothing checklist. '
            'For a missing-motion issue, compare the observed action across chronological frames; check that '
            'the subject actually changes pose and that input, position and state transitions remain correct. '
            'If evidence does not exercise the issue choose unavailable; if neither is clearly better '
            'choose tie. A serious playability or rule regression is critical even when visuals improve. '
            'For every diagnosis.issues item (or the diagnosis itself), record its state in BOTH versions '
            'and cite visible evidence. Do not mark all issues fixed merely because the overall candidate wins. '
            'Check required_controls when supplied, including input response and completion; missing evidence '
            'must be reported as unobserved rather than inferred success.',
            {'rules':adapter.instructions,'diagnosis':diagnosis,
             'quality_criteria':config['quality']['criteria'],
             'version_outcomes' if independent else 'matched_outcomes':matched_outcomes},
            evidence,
            'package_blind_pairwise_selection',{
                'preferred':{'type':'string','enum':['A','B','tie','unavailable']},
                'confidence':{'type':'string','enum':['low','medium','high']},
                'evidence_valid':{'type':'boolean'},'critical_regression':{'type':'boolean'},
                'reason':{'type':'string'},
                'issue_assessments':{'type':'array','items':{
                    'type':'object','additionalProperties':False,
                    'required':['issue_id','status_A','status_B','evidence'],
                    'properties':{'issue_id':{'type':'string'},
                        'status_A':{'type':'string','enum':['present','resolved','unobserved']},
                        'status_B':{'type':'string','enum':['present','resolved','unobserved']},
                        'evidence':{'type':'string'}}}},
                'control_evidence':{'type':'string'}},tokens=2200)
        reviews.append(label_review(answer,roles))
        if max_reviews==1:break
        if index==0 and not needs_confirmation(reviews[0]):break
    return reviews


def parameters(project,spec,diagnosis,history):
    from .config import relative
    values={name:json.loads(relative(project,p['file']).read_text()) for name,p in spec['parameters'].items()}
    return call('Propose bounded rendering parameter edits addressing this diagnosed defect. '
                'Change only named parameters; do not alter game mechanics. Avoid previously rejected identical proposals.',
                {'diagnosis':diagnosis,'parameters':spec['parameters'],'current_files':values,'previous':history[-12:]},[],
                'package_composition',{'reason':{'type':'string'},'changes':{'type':'array','minItems':1,'maxItems':8,
                 'items':{'type':'object','additionalProperties':False,'required':['parameter','value'],
                 'properties':{'parameter':{'type':'string','enum':list(spec['parameters'])},'value':{'type':'number'}}}}})
