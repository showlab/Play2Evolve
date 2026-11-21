"""Codex-led game construction; H3 is an on-demand tool, not a mandatory first stage."""
import argparse
from play2evolve.runtime import benchmark_root, godot_binary, asset_python
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import time
from codex_provider import add_arguments as add_codex_provider_arguments
from codex_provider import environment_values as codex_environment_values
from codex_provider import from_args as codex_provider_from_args
from game_initialization.batch.__main__ import (REPO, command, read, write, sha, inside,
    codex, preflight, framework_hash, verify_requirements)
from .requirements import (prepare_requirements, assert_scope, review_requirements,
                           require_current_review, validate_acceptance_plan)

HERE=Path(__file__).resolve().parent
BENCH_ROOT=benchmark_root(required=False)


def video_planning_guidance(root, cfg):
    """Give the builder a few relevant prior H3 decisions, never the whole store."""
    if not cfg.get('video_skill', False):
        return ''
    from gamecraft_video_skill.decisions import retrieve
    brief = (Path(root)/'instruction.md').read_text(errors='replace')
    guidance = retrieve(Path(root)/'generation', problems=[brief[:4000]],
                        task_id=cfg.get('task_id'),
                        global_store=cfg.get('global_video_skill_store'))
    write(Path(root)/'video_skill_planning_context.json', guidance)
    if not guidance:
        return ''
    return ('\nRelevant Video Skill planning experience (hypotheses, not requirements). '
            'Use only when the current game evidence matches the trigger; keep H3 model parameters fixed. '
            'Record your own material-boundary reasons in the asset request:\n'
            +json.dumps(guidance, ensure_ascii=False))


def benchmark_pythonpath():
    """Install the official benchmark package for parent checks and children."""
    package=BENCH_ROOT/'gamecraft_bench/verifier/replay.py'
    if not package.is_file():
        raise RuntimeError(f'Missing GameCraft-Bench Python package: {package}')
    root=str(BENCH_ROOT)
    if root not in os.sys.path:os.sys.path.insert(0,root)
    values=[str(REPO),root]
    values.extend(x for x in os.environ.get('PYTHONPATH','').split(os.pathsep) if x and x not in values)
    value=os.pathsep.join(values)
    os.environ['PYTHONPATH']=value
    return value


def agentic_preflight(root,cfg):
    """Run shared checks plus the import needed later by deterministic rollout."""
    pythonpath=benchmark_pythonpath()
    preflight(root,cfg)
    env=dict(os.environ,PYTHONPATH=pythonpath)
    command([cfg['asset_python'],'-c','from gamecraft_bench.verifier import replay'],
            root/'preflight-benchmark.log',env=env,timeout=30)


def prepare(a):
    brief=a.instruction.resolve()
    task=brief.parent.name
    if 'Godot 4' not in brief.read_text():raise ValueError('Godot 4 brief required')
    root=a.workspace.resolve();root.mkdir(parents=True,exist_ok=False)
    (root/'draft/game').mkdir(parents=True);(root/'generation').mkdir()
    shutil.copy2(brief,root/'instruction.md')
    shutil.copy2(HERE/'prompt.txt',root/'prompt.txt')
    write(root/'generation/asset_plan.json',{'schema_version':1,'task_id':task,'style':'','assets':[]})
    write(root/'generation/asset_manifest.json',{'schema_version':1,'task_id':task,'assets':{}})
    if getattr(a,'video_skill',False):
      from gamecraft_video_skill import initialize as initialize_video_skill, copy_skill
      source=getattr(a,'video_skill_path',None)
      if getattr(a,'global_video_skill_store',None):
        from gamecraft_video_skill import initialize_global_store
        store=initialize_global_store(a.global_video_skill_store)
        if source is None:source=store/'global.json'
      copy_skill(source,root/'generation') if source and Path(source).is_dir() else initialize_video_skill(root/'generation',source)
      shutil.copytree(root/'generation/video_skill',root/'draft/video_skill')
    # A shared-profile bootstrap has one construction pass.  A separate,
    # tightly scoped budget exists only for repairing a package/runtime check;
    # material/composition improvement belongs to initialization evolution.
    legacy_rounds=getattr(a,'rounds',None)
    bootstrap_repairs=(max(0,legacy_rounds-1) if legacy_rounds is not None
                       else a.bootstrap_repairs)
    session={'task_id':task,'model':a.model,'codex':a.codex,'host':a.host,
      'reasoning_effort':getattr(a,'reasoning_effort',None),
      'allow_shared_services':getattr(a,'allow_shared_services',False),
      'godot':str(a.godot),'asset_python':str(a.asset_python),'h3_url':a.h3_url,
      'h3_backend':getattr(a,'h3_backend','local'),
      'local_url':os.getenv('LOCAL_URL','http://127.0.0.1:30020/v1'),'timeout':a.timeout,'max_requests':a.max_requests,
      'rounds':1+bootstrap_repairs, # compatibility with pre-profile sessions
      'bootstrap_builds':1,'bootstrap_repairs':bootstrap_repairs,
      'initialization_rounds':a.initialization_rounds,
      'initialization_max_frames':getattr(a,'initialization_max_frames',64),
      'initialization_profile':'shared-rsi-v1',
      'bootstrap_only':getattr(a,'bootstrap_only',False),
      'demo_only':getattr(a,'demo_only',False),
      'judge_backend':a.judge_backend,'judge_model':a.judge_model or a.model,
      'asset_prompt_repair_backend':os.getenv('GAMECRAFT_ASSET_PROMPT_REPAIR_BACKEND','local'),
      'asset_prompt_repair_model':os.getenv('GAMECRAFT_ASSET_PROMPT_REPAIR_MODEL','local'),
      'global_video_skill_store':str(a.global_video_skill_store.resolve()) if a.global_video_skill_store else None,
      'instruction_sha256':sha(brief),'framework_sha256':framework_hash(),
      'created_at':time.time(),'asset_judge':False,'tool_network':True,
      'material_scope_planning':True,
      'video_skill':getattr(a,'video_skill',False),
      'video_skill_proposer':({'backend':'codex','model':a.video_skill_model}
                              if getattr(a,'video_skill',False) else None)}
    session.update(codex_provider_from_args(a))
    write(root/'session.json',session)
    write(root/'state.json',{'stage':'prepared'})
    print(root)


def host(root):
    cfg=read(root/'session.json')
    if socket.gethostname()!=cfg['host']:raise RuntimeError('Tools must execute on the configured training host')
    if sha(root/'instruction.md')!=cfg['instruction_sha256']:raise RuntimeError('Instruction changed')
    return cfg


def asset(root, request_path):
    cfg=host(root)
    from gamecraft_assets.plans import validate_generic
    from gamecraft_assets.generate import generate
    req=read(request_path)
    from gamecraft_assets.contract import validate_shared_style
    validate_shared_style(req.get('style'))
    if not req.get('reason','').strip():raise ValueError('Material request must record its purpose/change reason')
    partial={'schema_version':1,'task_id':cfg['task_id'],'style':req['style'],'assets':req['assets']}
    if not isinstance(req['style'],str) or not req['style'].strip():raise ValueError('Style required')
    for item in req['assets']:
        if cfg.get('material_scope_planning') and ('generation_scope' not in item or 'motion' not in item):
            raise ValueError('Bootstrap material needs generation_scope and motion before H3 generation')
        if not isinstance(item['requirements'],str):raise ValueError('Requirements must be resolved prose, not IDs')
        if item['kind']=='animation' and not item.get('reference_asset_id'):
            if not isinstance(item.get('reference_prompt'),str):raise ValueError('reference_prompt must be a STRING, not an object. Fix the request; do not downgrade to static sprites.')
            try:item['reference_prompt'].format(desc=item['description'])
            except (ValueError, KeyError) as exc:raise ValueError('Invalid reference_prompt template') from exc
    validate_generic(partial)
    with (root/'.asset-tool.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        existing={x['id']:x for x in read(root/'generation/asset_plan.json')['assets']}
        accepted=read(root/'generation/asset_manifest.json')['assets']
        requested_ids={item['id'] for item in req['assets']}
        for item in req['assets']:
            if existing.get(item['id'],{}).get('kind')=='animation' and item['kind']!='animation':
                raise ValueError('Do not downgrade an existing animation to a static asset; fix the technical request.')
            source_id=item.get('reference_asset_id')
            if source_id:
                source=existing.get(source_id)
                expected=(hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
                          if source else None)
                if (source_id in requested_ids or not source or source_id not in accepted or
                        accepted[source_id].get('plan_sha256')!=expected):
                    raise ValueError('reference_asset_id must name a previously accepted, unchanged material')
        journal=root/'material_requests.json'
        history=read(journal) if journal.exists() else []
        used=sum(len(r['asset_ids']) for r in history)
        if used+len(req['assets'])>cfg['max_requests']:raise RuntimeError('Material request budget exhausted')
        state=read(root/'state.json') if (root/'state.json').exists() else {}
        row={'id':len(history)+1,'asset_ids':[x['id'] for x in req['assets']],
             'reason':req['reason'],'started_at':time.time(),'status':'running',
             'round':state.get('round')}
        history.append(row);write(journal,history)
        request_dir=root/'requests'/f'{row["id"]:04d}';request_dir.mkdir(parents=True)
        write(request_dir/'request.json',req)
        plan=read(root/'generation/asset_plan.json');byid={x['id']:x for x in plan['assets']}
        for item in req['assets']:
            if byid.get(item['id'],{}).get('kind')=='animation' and item['kind']!='animation':
                raise ValueError('Do not downgrade an existing animation to a static asset; fix the technical request.')
            # Bind the art direction used to author this asset.  A later,
            # unrelated material request must not silently restyle it.
            item=dict(item,style=req['style'])
            byid[item['id']]=item
        plan.update(style=req['style'],assets=list(byid.values()));write(root/'generation/asset_plan.json',plan)
        env=dict(os.environ,PYTHONPATH=benchmark_pythonpath(),H3_URL=cfg['h3_url'],
                 H3_BACKEND=cfg.get('h3_backend','local'),LLM_BACKEND='local',OPTIMIZER_BACKEND='local')
        try:
            command([cfg['asset_python'],'-m','game_initialization.agentic','generate-worker','--workspace',str(root),
                     '--request',str(request_dir/'request.json')],request_dir/'h3.log',env=env,timeout=cfg['timeout'])
            manifest=read(root/'generation/asset_manifest.json');bindings=read(root/'draft/asset_bindings.json') if (root/'draft/asset_bindings.json').exists() else {}
            pending=[];source_videos={}
            for item in req['assets']:
                planned=byid[item['id']]
                rec=manifest['assets'].get(item['id'])
                sig=hashlib.sha256(json.dumps(planned,sort_keys=True).encode()).hexdigest()
                if not rec or rec.get('plan_sha256')!=sig:
                    pending.append(item['id']);continue
                files=[]
                for i,name in enumerate(rec['files']):
                    src=inside(root/'generation',name);target=root/'draft/game/assets'/item['id']/f'frame{i:04d}.png'
                    target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,target)
                    files.append(str(target.relative_to(root/'draft/game')))
                bindings[item['id']]=files
                version=rec.get('version')
                if type(version) is int:
                    raw=root/'generation/generated'/item['id']/f'v{version:04d}'/'raw.mp4'
                    if raw.is_file():source_videos[item['id']]=str(raw.relative_to(root))
            write(root/'draft/asset_bindings.json',bindings)
            shutil.copy2(root/'generation/asset_plan.json',root/'draft/asset_plan.json')
            if cfg.get('video_skill',False):
                shutil.copytree(root/'generation/video_skill',root/'draft/video_skill',dirs_exist_ok=True)
            row.update(status='partial' if pending else 'generated',pending=pending,
                       bindings={i:bindings.get(i,[]) for i in row['asset_ids']},
                       source_videos=source_videos)
        except Exception as exc:
            row.update(status='error',error=str(exc))
        finally:
            row['finished_at']=time.time();row['elapsed_seconds']=row['finished_at']-row['started_at']
            write(journal,history);write(request_dir/'result.json',row)
        print(json.dumps(row))
        return row


def generate_worker(root,request):
    cfg=host(root)
    os.environ['H3_BACKEND']=cfg.get('h3_backend','local')
    from gamecraft_assets.generate import generate
    from gamecraft_assets.plans import validate_generic
    req=read(request)
    state=read(root/'state.json') if (root/'state.json').exists() else {}
    rules=[]
    if cfg.get('video_skill',False):
        from .video_skill import rules_for_assets
        rules=rules_for_assets(root,[i['id'] for i in req['assets']])
    pending=generate(root/'generation',only=[i['id'] for i in req['assets']],rounds=1,candidates=1,
                     validator=validate_generic,judge=False,video_skill=cfg.get('video_skill',False),
                     phase='initialization',auto_commit=False,extra_video_rules=rules,
                     propose_video_patch=False,context={'workflow':'agentic_initialization',
                                                        'round':state.get('round')})
    # Missing items return as structured pending results, preserving useful work.
    print(json.dumps({'pending':pending}))


def bootstrap_smoke_ids(cases):
    """Check normal launch plus at most one builder-selected core interaction."""
    initial=[case for case in cases if case['purpose']=='initialization']
    if not initial:raise ValueError('Bootstrap needs an initialization scenario')
    entry=next((case for case in initial if case.get('launch')=='default'),initial[0])
    marked=[case for case in initial if case.get('bootstrap_smoke') is True and case['id']!=entry['id']]
    if len(marked)>1:
        raise ValueError('Mark at most one additional bootstrap smoke scenario')
    return [entry['id']]+[case['id'] for case in marked]


def benchmark_preflight(game, task_id, output, godot, *, demo_only=False):
    """Prove that every declared demo can reach the benchmark's video judge.

    This is a technical gate, not a synthetic score or an assessment of game quality.
    """
    benchmark_pythonpath()
    from gamecraft_evaluation.packaging import validate_trace
    from gamecraft_bench.verifier.replay import replay_trace
    from gamecraft_bench.verifier.score import _sample_frames
    rubric_path=BENCH_ROOT/'tasks'/task_id/'tests/rubric.json'
    if not demo_only and not rubric_path.is_file():
        raise ValueError(f'Missing benchmark rubric for {task_id}')
    rubric={} if demo_only else read(rubric_path)
    demo_dir=game/'demo_outputs'
    traces=sorted(demo_dir.glob('*.json')) if demo_dir.is_dir() else []
    max_demos=int(rubric.get('max_demos',10))
    if not 1<=len(traces)<=max_demos:
        if demo_only:
            raise ValueError(f'Need 1–{max_demos} demo traces; found {len(traces)}')
        raise ValueError(f'Benchmark needs 1–{max_demos} demo traces; found {len(traces)}')
    output.mkdir()
    results=[]
    for trace in traces:
        validate_trace(trace)
        target=output/trace.stem
        video=target/(trace.stem+'.mp4')
        replay=replay_trace(project_dir=game,trace_path=trace,output_mp4=video,
                            godot_bin=str(godot),log_dir=target/'logs')
        frames=_sample_frames(video,target/'frames',
                              duration_seconds=replay.duration_seconds,
                              interval_seconds=0.5,
                              max_window_seconds=float(rubric.get('max_demo_seconds',20)),
                              seed=trace.stem)
        if not frames:
            raise RuntimeError(f'Benchmark sampled no frames from {trace.name}')
        results.append({'demo':trace.name,'video':str(video),'frames':len(frames)})
    write(output/'report.json',{'ready':True,'task_id':task_id,'demos':results,
         'scope':('All declared showcase traces replay and yield sampled frames; no benchmark score or quality claim.'
                  if demo_only else
                  'All declared benchmark traces replay and yield judge frames; no VLM score or quality claim.')})
    return results


def check(root):
    cfg=host(root)
    benchmark_pythonpath()
    from game_initialization.package import inspect_package,freeze,inventory
    from gamecraft_training.package.config import load
    serial=time.time_ns();out=root/'checks'/str(serial)
    out.mkdir(parents=True)
    report={'passed':False,'created_at':time.time(),'output':str(out.relative_to(root))}
    try:
        assert_scope(root,read(root/'requirements_origin.json'))
        validate_acceptance_plan(root)
        verify_requirements(root)
        # The shared initialization profile judges the frozen bootstrap package,
        # so carry the immutable public scope and its implementation plan with it.
        for name in ('instruction.md','requirements.json','acceptance_plan.json'):
            shutil.copy2(root/name,root/'draft'/name)
        plan=read(root/'generation/asset_plan.json');manifest=read(root/'generation/asset_manifest.json')
        if not plan['assets']:raise ValueError('No generated initial materials')
        bindings=read(root/'draft/asset_bindings.json')
        for item in plan['assets']:
            rec=manifest['assets'].get(item['id'])
            expected=hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest()
            if not rec or rec.get('plan_sha256')!=expected:raise ValueError('Missing/stale material: '+item['id'])
            if len(bindings.get(item['id'],[]))!=item['frames']:raise ValueError('Incomplete material bindings')
            for src,dst in zip(rec['files'],bindings[item['id']]):
                if sha(inside(root/'generation',src))!=sha(inside(root/'draft/game',dst)):
                    raise ValueError(f'Material file mismatch for {item["id"]}: generation/{src} must be copied byte-for-byte to draft/game/{dst}')
        write(root/'draft/initialization.json',{'schema_version':1,'control_contract_version':1,'task_id':cfg['task_id'],'version':'v0','state':'draft',
              'generator':cfg['model'],'asset_judge':False,'video_skill':cfg.get('video_skill',False),
              'origin':{'instruction_sha256':cfg['instruction_sha256']}})
        _,adapter,cases=inspect_package(root/'draft',sealed=False)
        _,_,composition,_=load(root/'draft',adapter,cases)
        # A bootstrap package must also be usable by the later Codex training
        # entrypoint. Otherwise a successful smoke replay can still fail before
        # the first initialization round starts.
        from gamecraft_training.package.codex_edit import policy
        from gamecraft_training.package.config import relative
        for name in policy(composition)['protected']:
            if not relative(root/'draft/game',name).exists():
                raise ValueError(f'Missing protected path: {name}')
        if (root/'draft/checks').exists():raise ValueError('Store author evidence under draft/evidence, not reserved draft/checks')
        command([cfg['godot'],'--headless','--path',str(root/'draft/game'),'--editor','--quit'],out/'import.log',timeout=120)
        command([cfg['godot'],'--headless','--path',str(root/'draft/game'),'--quit-after','5'],out/'start.log',timeout=60)
        for log in ('import.log','start.log'):
            if any(x in (out/log).read_text() for x in ('SCRIPT ERROR:','\nERROR:')):raise RuntimeError('Godot errors in '+log)
        smoke=(bootstrap_smoke_ids(cases) if cfg.get('initialization_profile')=='shared-rsi-v1' else None)
        freeze(root/'draft',out/'package',cfg['godot'],scenario_ids=smoke)
        videos=[str(p.relative_to(root)) for p in out.rglob('replay.mp4')]
        screenshots=[str(p.relative_to(root)) for p in out.rglob('frame_0000.png')]
        report.update(passed=True,package=str((out/'package').relative_to(root)),videos=videos,screenshots=screenshots,
                      replay_scenarios=smoke if smoke is not None else [c['id'] for c in cases if c['purpose']=='initialization'],
                      scope='Godot build/start and listed scripted smoke replays; full scene requirements are judged during initialization evolution')
    except Exception as exc:report['error']=str(exc)
    write(out/'result.json',report);write(root/'latest_check.json',report)
    print(json.dumps(report));return report


def publish(root):
    from game_initialization.package import inventory,inspect_package
    report=read(root/'latest_check.json')
    if not report['passed'] or inventory(root/'draft')!=report['draft_sha256']:raise ValueError('No current passing runtime check')
    require_current_review(root,report)
    from .visual_v2 import require_current_visual_review
    require_current_visual_review(root,report)
    inspection=read(root/'inspection.json')
    if not inspection.get('screens') or not inspection.get('videos'):raise ValueError('Actual screenshot and video inspection is required')
    allowed=set(report['videos'])
    for r in inspection['screens']+inspection['videos']:
        path=r.get('image',r.get('path',''))
        if not r.get('observation','').strip() or not inside(root,path).is_file():raise ValueError('Invalid inspection evidence')
    if not any(r['path'] in allowed for r in inspection['videos']):raise ValueError('Inspection must include latest runtime video')
    package=inside(root,report['package']);inspect_package(package)
    shutil.copytree(package,root/'initial-v0')
    if (root/'generation/video_skill').is_dir():
        # Skill decisions happen after the checked replay's visual review. They
        # are metadata and do not alter the already checked game runtime.
        shutil.copytree(root/'generation/video_skill',root/'initial-v0/video_skill',dirs_exist_ok=True)
    write(root/'state.json',{'stage':'ready','package':'initial-v0','finished_at':time.time(),
          'scope':'independent requirements and basic visual integration reviews; no polish score or official benchmark score'})


def run_legacy(root):
    cfg=host(root)
    with (root/'.run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        agentic_preflight(root,cfg)
        if (root/'initial-v0').exists():raise ValueError('Initial version already exists; use a new workspace')
        origin=prepare_requirements(root,cfg)
        prompt=(root/'prompt.txt').read_text()+video_planning_guidance(root,cfg)
        prompt+=f"\nActive generation backend: {cfg.get('h3_backend','local')}."
        prompt+='\nExact package schema references: '+str(REPO/'game_initialization/package.py')+'; '+str(REPO/'gamecraft_training/package/config.py')+'; '+str(REPO/'gamecraft_behavior/adapters/base.py')
        prompt+='\nRead '+str(REPO/'game_initialization/batch/contract.txt')+' for JSON schemas ONLY; the old PLAN/IMPLEMENT ordering and ban on tool calls are superseded by this agent-led workflow.'
        import shlex
        prefix=f'PYTHONPATH={shlex.quote(benchmark_pythonpath())} {shlex.quote(os.sys.executable)} -m game_initialization.agentic'
        prompt+=f'\nMaterial tool: {prefix} asset --workspace {shlex.quote(str(root))} --request REQUEST_JSON\nRuntime tool: {prefix} check --workspace {shlex.quote(str(root))}\nGodot: {cfg["godot"]}\nRequest budget: {cfg["max_requests"]} asset entries, including explicit replacements.'
        error=(root/'initial_feedback.txt').read_text() if (root/'initial_feedback.txt').exists() else ''
        for i in range(cfg['rounds']):
            if cfg.get('video_skill',False):
                from .video_skill import begin_round
                begin_round(root,i+1)
            attempt=root/'attempts'/f'{i+1:03d}'
            attempt.mkdir(parents=True,exist_ok=False)
            shutil.copytree(root/'draft',attempt/'before')
            write(root/'state.json',{'stage':'building','round':i+1,'started_at':time.time()})
            visual_before=sha(root/'latest_visual_review.json') if (root/'latest_visual_review.json').exists() else None
            skill_finished=False
            try:
                codex(root,cfg,f'build-{i}',prompt+'\nParent feedback from previous attempt: '+error)
                assert_scope(root,origin)
                report=check(root)
                if not report['passed']:raise RuntimeError(report['error'])
                # Agent inspection uses its preceding check; same draft implies same checked game.
                inspected=read(root/'inspection.json')
                if not inspected.get('videos'):raise RuntimeError('No video inspection recorded')
                # Preserve the new parent check but permit the immediately preceding
                # agent check video when it came from the identical draft hash.
                for r in inspected['videos']:
                    p=inside(root,r['path']);evidence=p
                    for parent in p.parents:
                        if parent==root:break
                        candidate=parent/'result.json'
                        if candidate.exists():
                            prior=read(candidate)
                            if prior.get('passed') and prior.get('draft_sha256')==report['draft_sha256']:
                                report['videos'].append(str(evidence.relative_to(root)))
                                break
                write(root/'latest_check.json',report)
                from .visual_v2 import review_visual
                write(root/'state.json',{'stage':'visual_review','round':i+1})
                review_visual(root,cfg,f'visual-{i}',report)
                if cfg.get('video_skill',False):
                    from .video_skill import finish_round
                    finish_round(root,i+1,cfg['rounds'],read(root/'latest_visual_review.json')['review'])
                    skill_finished=True
                write(root/'state.json',{'stage':'requirements_review','round':i+1})
                review_requirements(root,cfg,f'acceptance-{i}',report,origin)
                publish(root)
                shutil.copytree(root/'draft',attempt/'after')
                write(attempt/'result.json',{'round':i+1,'passed':True})
                return
            except Exception as exc:
                error=str(exc)
                repair={'round':i+1,'error':error}
                visual_path=root/'latest_visual_review.json'
                visual_changed=(visual_path.is_file() and
                                (visual_before is None or sha(visual_path)!=visual_before))
                if visual_changed:
                    visual=read(visual_path)
                    if visual.get('runtime_sha256')==sha(root/'latest_check.json'):
                        review=visual.get('review',{})
                        if cfg.get('video_skill',False) and not skill_finished:
                            from .video_skill import finish_round
                            repair['video_skill']=finish_round(root,i+1,cfg['rounds'],review)
                            skill_finished=True
                        if review.get('issues'):
                            repair['visual_review']={
                                'passed':review.get('passed'),
                                'issues':review['issues'],
                                'limitations':review.get('limitations',[]),
                                'evidence':visual.get('evidence',[])}
                            error=('Independent visual review findings follow. Fix every blocking issue, '
                                   'using the cited evidence and suggested repair. Preserve prior versions.\n'
                                   +json.dumps(repair['visual_review'],ensure_ascii=False,indent=2))
                requirement_path=root/'latest_requirements_review.json'
                if requirement_path.is_file():
                    requirement=read(requirement_path)
                    if requirement.get('runtime_sha256')==sha(root/'latest_check.json'):
                        repair['requirements_review']=requirement.get('review')
                        error+='\nIndependent requirements review:\n'+json.dumps(
                            requirement.get('review'),ensure_ascii=False,indent=2)
                repair['repair_feedback']=error
                write(root/'last_error.json',repair)
                shutil.copytree(root/'draft',attempt/'after')
                write(attempt/'result.json',{'round':i+1,'passed':False,'error':error})
            finally:
                for name in ('latest_check.json','latest_visual_review.json','latest_requirements_review.json'):
                    if (root/name).exists():shutil.copy2(root/name,attempt/name)
        write(root/'state.json',{'stage':'needs_attention','error':error})
        raise RuntimeError(error)


def run_profile(root,cfg):
    """Construct once, repair only blocking runtime failures, then evolve."""
    with (root/'.run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        agentic_preflight(root,cfg)
        if (root/'initial-v0').exists():raise ValueError('Initial version already exists; use a new workspace')
        origin=prepare_requirements(root,cfg)
        prompt=(root/'prompt.txt').read_text()+video_planning_guidance(root,cfg)
        prompt+=f"\nActive generation backend: {cfg.get('h3_backend','local')}."
        prompt+='\nExact package schema references: '+str(REPO/'game_initialization/package.py')+'; '+str(REPO/'gamecraft_training/package/config.py')+'; '+str(REPO/'gamecraft_behavior/adapters/base.py')
        prompt+='\nRead '+str(REPO/'game_initialization/batch/contract.txt')+' for JSON schemas ONLY; the old PLAN/IMPLEMENT ordering and ban on tool calls are superseded by this agent-led workflow.'
        import shlex
        prefix=f'PYTHONPATH={shlex.quote(benchmark_pythonpath())} {shlex.quote(os.sys.executable)} -m game_initialization.agentic'
        prompt+=f'\nMaterial tool: {prefix} asset --workspace {shlex.quote(str(root))} --request REQUEST_JSON\nRuntime tool: {prefix} check --workspace {shlex.quote(str(root))}\nGodot: {cfg["godot"]}\nRequest budget: {cfg["max_requests"]} asset entries, including explicit replacements.'
        feedback=(root/'initial_feedback.txt').read_text() if (root/'initial_feedback.txt').exists() else ''
        # Older prepared workspaces stored only ``rounds``.  Preserve their
        # configured ceiling; new workspaces express the build and repair
        # budgets separately.
        repair_budget=cfg.get('bootstrap_repairs',max(0,cfg.get('rounds',1)-1))
        attempts=1+repair_budget
        report=None
        for index in range(1,attempts+1):
            attempt=root/'attempts'/f'{index:03d}';attempt.mkdir(parents=True,exist_ok=False)
            shutil.copytree(root/'draft',attempt/'before')
            phase='bootstrap_build' if index==1 else 'bootstrap_repair'
            write(root/'state.json',{'stage':phase,'attempt':index,'max_attempts':attempts,
                                     'started_at':time.time()})
            try:
                task_prompt=prompt
                if index==1:
                    task_prompt+=('\nSHARED INITIALIZATION PROFILE: this is the single construction pass. '
                                  'Use the runtime tool until the package launches and its declared scenarios '
                                  'are structurally valid. Do not perform aesthetic improvement rounds; the '
                                  'parent shared initialization loop owns later material/composition diagnosis.')
                    task_prompt+=('\nDeclare separate initialization scenarios for independently testable screens '
                                  'and activities, including distinct training mini-games. Keep a normal end-to-end '
                                  'flow for progression. Every scenario must observe the action itself, not only a '
                                  'menu label or a state counter.')
                else:
                    task_prompt+=('\nBOOTSTRAP TECHNICAL REPAIR ONLY: fix the blocking package/build/runtime '
                                  'failure quoted below. Preserve working features and materials. Do not add '
                                  'polish, redesign scenes, or regenerate an asset unless that exact asset is '
                                  'missing or invalid and prevents the package check from passing.')
                task_prompt+='\nBootstrap feedback from previous attempt: '+feedback
                codex(root,cfg,f'bootstrap-{index-1}',task_prompt)
                assert_scope(root,origin)
                report=check(root)
                # A structurally valid game can still have an acceptance-plan
                # hole. Repair that evidence contract inside this bootstrap
                # attempt instead of spending H3/initialization rounds on an
                # issue that cannot be observed. These repairs may only change
                # the plan and declared scenarios; game/assets remain intact.
                coverage_index=0
                while (not report['passed'] and
                       str(report.get('error','')).startswith('Acceptance plan needs ') and
                       coverage_index<3):
                    coverage_index+=1
                    coverage_dir=attempt/'coverage-repairs'/f'{coverage_index:03d}'
                    coverage_dir.mkdir(parents=True)
                    for source,name in ((root/'acceptance_plan.json','acceptance_plan.before.json'),
                                        (root/'draft/scenarios.json','scenarios.before.json')):
                        if source.is_file():shutil.copy2(source,coverage_dir/name)
                    from game_initialization.package import inventory
                    protected_draft=inventory(root/'draft')
                    protected_draft.pop('scenarios.json',None)
                    protected_generation=inventory(root/'generation')
                    write(root/'state.json',{'stage':'bootstrap_coverage_repair','attempt':index,
                                             'coverage_attempt':coverage_index,'started_at':time.time()})
                    coverage_prompt=(
                        'REPAIR ONLY THE INITIALIZATION ACCEPTANCE COVERAGE. Read immutable requirements.json, '
                        'acceptance_plan.json, draft/scenarios.json, and the reported validation error below. '
                        'Edit only acceptance_plan.json and draft/scenarios.json. Preserve every public requirement, '
                        'all game code, bindings, generated materials, and existing useful scenarios. Add or correct '
                        'deterministic initialization scenarios so every mapped requirement is actually exercised. '
                        'For normal/default launch or title-entry requirements, use launch:"default"; this makes the '
                        'runtime omit --scenario and capture the real first screen before applying actions. Do not '
                        'rename a scenario as a substitute for changing its launch mode. Do not invoke H3 or perform '
                        'visual/gameplay polish. Fix all acceptance coverage gaps you can identify, not only the first '
                        'reported ID.\nValidation error: '+str(report['error']))
                    codex(root,cfg,f'bootstrap-{index-1}-coverage-{coverage_index}',coverage_prompt)
                    assert_scope(root,origin)
                    current_draft=inventory(root/'draft');current_draft.pop('scenarios.json',None)
                    if current_draft!=protected_draft or inventory(root/'generation')!=protected_generation:
                        raise RuntimeError('Acceptance coverage repair changed game code or generated materials')
                    for source,name in ((root/'acceptance_plan.json','acceptance_plan.after.json'),
                                        (root/'draft/scenarios.json','scenarios.after.json')):
                        if source.is_file():shutil.copy2(source,coverage_dir/name)
                    report=check(root)
                    write(coverage_dir/'result.json',{'attempt':coverage_index,
                          'passed':report['passed'],'error':report.get('error'),
                          'package':report.get('package')})
                if not report['passed']:raise RuntimeError(report['error'])
                benchmark_preflight(inside(root,report['package'])/'game',cfg['task_id'],
                                    attempt/'benchmark-preflight',cfg['godot'],
                                    demo_only=cfg.get('demo_only',False))
                shutil.copytree(root/'draft',attempt/'after')
                write(attempt/'result.json',{'attempt':index,'phase':phase,'passed':True,
                                             'package':report['package']})
                break
            except Exception as exc:
                feedback=str(exc)
                report={'passed':False,'error':feedback}
                shutil.copytree(root/'draft',attempt/'after')
                write(attempt/'result.json',{'attempt':index,'phase':phase,'passed':False,
                                             'error':feedback})
                write(root/'last_error.json',{'attempt':index,'phase':phase,
                                              'repair_feedback':feedback})
        if not report or not report.get('passed'):
            write(root/'state.json',{'stage':'needs_attention','error':feedback})
            raise RuntimeError(feedback)
        if cfg.get('bootstrap_only'):
            write(root/'state.json',{'stage':'bootstrap_ready','package':report['package'],
                                    'finished_at':time.time(),'evolution_started':False})
            return {'bootstrap_package':report['package'],'state':'bootstrap_ready'}
        return evolve_bootstrap(root,cfg,report)


def evolve_bootstrap(root,cfg,report):
    """Continue from a checked bootstrap package without regenerating its assets."""
    if (root/'evolution').exists() or (root/'initial-v0').exists():
        raise ValueError('Evolution or initial-v0 already exists; use a new recovery destination')
    write(root/'state.json',{'stage':'initialization_evolution','round':0,
          'bootstrap_package':report['package']})
    from gamecraft_training.package.loop import run as evolve
    from gamecraft_training.package.codex_edit import CodexEditor
    editor=CodexEditor(binary=cfg['codex'],model=cfg['model'],timeout=cfg['timeout'],provider=cfg)
    with service_environment(cfg):
        result=evolve(inside(root,report['package']),root/'evolution',godot=cfg['godot'],
            asset_python=cfg['asset_python'],rounds=cfg['initialization_rounds'],
            initialization_max_frames=cfg.get('initialization_max_frames',64),
            profile='initialization',composition_mode='codex',composition_policy={'protected':[]},
            code_editor=editor,video_skill=cfg.get('video_skill',False),
            global_video_skill_store=cfg.get('global_video_skill_store'),
            judge_backend=cfg.get('judge_backend','codex'),judge_model=cfg.get('judge_model',cfg['model']))
    write(root/'evolution_result.json',result)
    # Publish the packaged optimization result without claiming absolute
    # requirement acceptance. Side errors remain visible as warnings.
    if not result.get('package') or result.get('state') not in ('completed','completed_with_errors'):
        error='Shared initialization profile did not produce a completed package'
        write(root/'state.json',{'stage':'needs_attention','error':error,'result':'evolution_result.json'})
        raise RuntimeError(error)
    shutil.copytree(result['package'],root/'initial-v0')
    write(root/'state.json',{'stage':'ready','package':'initial-v0','finished_at':time.time(),
          'profile':'initialization','evolution':'evolution_result.json',
          'final_acceptance':result.get('final_acceptance','not_run'),
          'warnings':len(result.get('errors',[]))})
    return result


def resume_evolution(root):
    """Recover a completed bootstrap after a framework-only parent-check failure."""
    cfg=host(root)
    benchmark_pythonpath()
    with (root/'.run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'evolution').exists() or (root/'initial-v0').exists():
            raise ValueError('This workspace already started or completed evolution')
        report=check(root)
        if not report.get('passed'):
            error='Bootstrap recovery check failed: '+str(report.get('error','unknown error'))
            write(root/'state.json',{'stage':'needs_attention','error':error})
            raise RuntimeError(error)
        write(root/'recovery.json',{'created_at':time.time(),'reason':'resume after parent environment fix',
              'bootstrap_package':report['package'],'preserved_generation':str(root/'generation')})
        return evolve_bootstrap(root,cfg,report)


@contextlib.contextmanager
def service_environment(cfg):
    """In-process evolution and its workers use the prepared session endpoints."""
    values={'H3_URL':cfg['h3_url'],'H3_BACKEND':cfg.get('h3_backend','local'),
            'LOCAL_URL':cfg['local_url'],'CODEX_BIN':cfg['codex'],
            'GAMECRAFT_ASSET_PROMPT_REPAIR_BACKEND':cfg.get('asset_prompt_repair_backend','local'),
            'GAMECRAFT_ASSET_PROMPT_REPAIR_MODEL':cfg.get('asset_prompt_repair_model','local'),
            'PYTHONPATH':benchmark_pythonpath(),
            'LLM_BACKEND':'local','OPTIMIZER_BACKEND':'local',
            'GAMECRAFT_VIDEO_SKILL_MODEL':(cfg.get('video_skill_proposer') or {}).get('model','gpt-6-sol')}
    values.update(codex_environment_values(cfg))
    if cfg.get('reasoning_effort'):
        values['GAMECRAFT_CODEX_REASONING_EFFORT']=cfg['reasoning_effort']
    keys=set(values)|{'GAMECRAFT_JUDGE_BACKEND','GAMECRAFT_JUDGE_MODEL'}
    previous={key:os.environ.get(key) for key in keys}
    os.environ.update(values)
    try:yield
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def run(root):
    cfg=host(root)
    if cfg.get('initialization_profile')=='shared-rsi-v1':
        return run_profile(root,cfg)
    return run_legacy(root)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','run','resume-evolution','asset','generate-worker','check'])
    p.add_argument('--workspace',type=Path,required=True);p.add_argument('--instruction',type=Path)
    p.add_argument('--request',type=Path);p.add_argument('--model',default='gpt-6-sol');p.add_argument('--codex',default=os.getenv('CODEX_BIN','codex'))
    p.add_argument('--reasoning-effort',choices=['low','medium','high','xhigh'],
                   help='Explicit Codex reasoning effort; omitted preserves the existing configuration')
    p.add_argument('--allow-shared-services',action='store_true',
                   help='Bootstrap may share existing training/H3 services; requests use the H3 queue')
    add_codex_provider_arguments(p)
    p.add_argument('--host',default=socket.gethostname())
    p.add_argument('--godot',type=Path,default=godot_binary())
    p.add_argument('--asset-python',type=Path,default=asset_python())
    p.add_argument('--h3-url',default=os.getenv('H3_URL','http://127.0.0.1:30010'));p.add_argument('--timeout',type=int,default=7200)
    p.add_argument('--h3-backend',choices=['local','minimax','wan'],default='local',
                   help='Use local H3 (default), MiniMax API, or local Wan2.2-5B')
    p.add_argument('--max-requests',type=int,default=64)
    p.add_argument('--bootstrap-repairs',type=int,default=1,
                   help='Technical repair attempts after the single bootstrap build')
    p.add_argument('--bootstrap-only',action='store_true',
                   help='Stop after a checked bootstrap; resume evolution later if desired')
    p.add_argument('--demo-only',action='store_true',
                   help='Use the same bootstrap checks and trace replay for a custom brief without an official benchmark rubric')
    p.add_argument('--rounds',type=int,
                   help='Deprecated total bootstrap-attempt limit; use --bootstrap-repairs')
    p.add_argument('--initialization-rounds',type=int,default=3,
                   help='Maximum material/composition optimization rounds per initialization scene after bootstrap')
    p.add_argument('--initialization-max-frames',type=int,default=64,
                   help='Maximum chronological evidence frames for each initialization scene judge')
    p.add_argument('--video-skill',action='store_true',help='Enable the optional video-generation skill; default preserves legacy generation')
    p.add_argument('--video-skill-model',default='gpt-6-sol',help='Codex model that proposes H3-only Video Skill rules')
    p.add_argument('--video-skill-path',type=Path,help='Optional global skill JSON or snapshot directory (requires --video-skill)')
    p.add_argument('--global-video-skill-store',type=Path,
                   help='Optional cross-game Video Skill candidate store (requires --video-skill)')
    p.add_argument('--judge-backend',choices=['local','codex','api'],default='codex')
    p.add_argument('--judge-model',help='Defaults to --model')
    a=p.parse_args();root=a.workspace.resolve()
    if min(a.timeout,a.max_requests,a.initialization_rounds)<1 or a.bootstrap_repairs<0:
        p.error('Budgets must be positive (bootstrap repairs may be zero)')
    if not 1<=a.initialization_max_frames<=600:p.error('--initialization-max-frames must be 1–600')
    if a.rounds is not None and a.rounds<1:p.error('--rounds must be positive')
    if a.video_skill_path and not a.video_skill:p.error('--video-skill-path requires --video-skill')
    if a.global_video_skill_store and not a.video_skill:p.error('--global-video-skill-store requires --video-skill')
    if a.command=='prepare' and a.allow_shared_services and not a.bootstrap_only:
        p.error('--allow-shared-services requires --bootstrap-only')
    if a.command=='prepare':
        if not a.instruction:p.error('--instruction required')
        prepare(a)
    elif a.command=='run':run(root)
    elif a.command=='resume-evolution':resume_evolution(root)
    elif a.command=='check':check(root)
    else:
        if not a.request:p.error('--request required')
        (asset if a.command=='asset' else generate_worker)(root,a.request.resolve())


if __name__=='__main__':main()
