"""Prepare offline; execute sequentially on the explicitly selected training host."""
import argparse
from play2evolve.runtime import benchmark_root, godot_binary, asset_python
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from codex_provider import add_arguments as add_codex_provider_arguments
from codex_provider import child_environment as codex_environment
from codex_provider import cli_args as codex_provider_args
from codex_provider import from_args as codex_provider_from_args

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or root.resolve() not in path.resolve().parents or path.is_symlink():
        raise ValueError(f'Unsafe relative path: {name}')
    return path


def command(args, log, *, cwd=None, timeout=7200, stdin=None, env=None):
    """Kill only this command's process group on timeout/interruption; never a service."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w') as stream:
        p = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=stream, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            p.communicate(stdin, timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            raise
        if p.returncode:
            raise RuntimeError(f'Command exited {p.returncode}; see {log}')


def prepare(args):
    tasks = read(args.tasks)
    if not isinstance(tasks, list) or not tasks or len(set(tasks)) != len(tasks):
        raise ValueError('Expected unique nonempty task list')
    briefs = {}
    for task in tasks:
        if not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*', task):
            raise ValueError('Unsafe task ID')
        path = args.bench.resolve() / 'tasks' / task / 'instruction.md'
        raw = path.read_text()
        if 'Godot 4' not in raw:
            raise ValueError(f'{task}: this runner currently supports Godot 4 only')
        briefs[task] = path
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(HERE/'contract.txt', args.output/'contract.txt')
    for task, path in briefs.items():
        dest = args.output/task
        (dest/'generation').mkdir(parents=True)
        (dest/'draft').mkdir()
        shutil.copy2(path, dest/'instruction.md')
        if getattr(args,'video_skill',False):
            from gamecraft_video_skill import initialize as initialize_video_skill, copy_skill
            source=getattr(args,'video_skill_path',None)
            copy_skill(source,dest/'generation') if source and Path(source).is_dir() else initialize_video_skill(dest/'generation',source)
        write(dest/'state.json', {'stage':'prepared'})
    config = {'schema_version':1, 'model':args.model, 'host':args.host, 'tasks':tasks,
              'brief_sha256':{t:sha(p) for t,p in briefs.items()}, 'repo':str(REPO),
              'contract_sha256':sha(args.output/'contract.txt'),
              'codex':args.codex, 'asset_python':str(args.asset_python), 'godot':str(args.godot),
              'h3_url':args.h3_url, 'h3_backend':getattr(args,'h3_backend','local'),
              'local_url':args.local_url,
              'asset_judge':not getattr(args, 'no_asset_judge', False), 'h3_rounds':args.h3_rounds, 'candidates':args.candidates, 'repairs':args.repairs,
              'video_skill':getattr(args,'video_skill',False),
              'timeout':args.timeout, 'created_at':time.time(),
              'framework_sha256':framework_hash(), 'official_benchmark_metric':False}
    config.update(codex_provider_from_args(args))
    if getattr(args, 'codex_queue', None):
        config['codex_queue'] = str(args.codex_queue.resolve())
    write(args.output/'batch.json', config)
    print(f'Prepared {len(tasks)} games at {args.output}; no models called.')


def framework_hash():
    files = [REPO/'llm.py', REPO/'gen_assets.py', REPO/'h3_minimax_api.py',
             REPO/'codex_provider.py', REPO/'wan_video_backend.py',
             REPO/'play2evolve/runtime.py', REPO/'play2evolve/cli.py']
    for folder in ('game_initialization','gamecraft_assets','gamecraft_behavior','gamecraft_training','gamecraft_composition','gamecraft_video_skill'):
        files.extend((REPO/folder).rglob('*.py'))
    files.append(REPO/'gamecraft_video_skill/default_skill.json')
    return hashlib.sha256(''.join(str(p.relative_to(REPO))+sha(p) for p in sorted(files)).encode()).hexdigest()


def get_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=20) as r:
        return json.load(r)


def needs_local_service(config):
    """Require Qwen only for workflows that actually call the local backend."""
    if config.get('initialization_profile') == 'shared-rsi-v1':
        # This profile runs fixed replays, never the local acting Player.
        judge_local = config.get('judge_backend', 'local') == 'local'
        repair_local = config.get('asset_prompt_repair_backend', 'local') == 'local'
        return judge_local or (not config.get('bootstrap_only', False) and repair_local)
    return (config.get('asset_judge', True)
            or not config.get('bootstrap_only', False)
            or config.get('judge_backend') == 'local')


def preflight(root, config):
    if socket.gethostname() != config['host']:
        raise RuntimeError(f'Wrong host: {socket.gethostname()}; expected {config["host"]}')
    if config['framework_sha256'] != framework_hash():
        raise RuntimeError('Framework changed since preparation; prepare a new batch')
    executables = [config['asset_python'], config['godot'], 'ffmpeg', 'Xvfb', 'xdotool']
    if not config.get('codex_queue'):
        executables.append(config['codex'])
    for executable in executables:
        if not shutil.which(executable):
            raise RuntimeError(f'Missing executable: {executable}')
    if config.get('codex_queue'):
        worker = read(Path(config['codex_queue'])/'worker_state.json')
        if worker['state'] != 'ready' or time.time()-worker['time']>30:
            raise RuntimeError('Developer Codex worker is not ready')
    elif config.get('codex_provider', 'openai') == 'openai':
        command([config['codex'],'login','status'], root/'preflight-login.log', timeout=30)
    else:
        codex_environment(config)
    command([config['asset_python'],'-c','import PIL, numpy, cv2, openai, httpx'], root/'preflight-python.log',timeout=30)
    command([config['godot'],'--version'], root/'preflight-godot.log', timeout=30)
    busy = []
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            parts = p.read_bytes().split(b'\0')
            if any(x in parts for x in (b'gamecraft_training', b'gamecraft_training.package', b'skillopt_lite.py')):
                busy.append(int(p.parent.name))
        except OSError:
            pass
    model = None
    if needs_local_service(config):
        model = get_json(config['local_url'].removesuffix('/v1')+'/get_model_info').get('model_path','')
        if not model.endswith('/Qwen3-VL-235B-A22B-Instruct'):
            raise RuntimeError('Unexpected local asset Judge model')
    if config.get('h3_backend','local') == 'wan':
        from wan_video_backend import preflight as wan_preflight
        wan_preflight()
        active = []
    elif config.get('h3_backend','local') == 'minimax':
        if not os.environ.get('MINIMAX_API_KEY'):
            raise RuntimeError('H3_BACKEND=minimax requires MINIMAX_API_KEY')
        active = []
    else:
        jobs = get_json(config['h3_url']+'/v1/videos')
        if isinstance(jobs, dict):
            jobs = jobs.get('data', jobs.get('videos'))
        if not isinstance(jobs, list):
            raise RuntimeError('Unrecognized H3 job response')
        active = [j.get('id') for j in jobs if j.get('status') not in ('completed','failed','canceled','cancelled')]
    gpu = subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.used,memory.total,utilization.gpu','--format=csv'], text=True)
    write(root/'preflight.json', {'host':socket.gethostname(),'model':model,
          'local_service_required':needs_local_service(config),
          'training_pids':busy,'h3_jobs':active,'gpu':gpu,'time':time.time()})
    if (busy or active) and not (config.get('allow_shared_services',False) and config.get('bootstrap_only',False)):
        raise RuntimeError('Existing training/H3 work; retry later. No process was stopped.')


def codex(task, config, stage, prompt, *, schema=None, images=None):
    if config.get('codex_queue'):
        if images:
            raise ValueError('Image review requires the local Codex CLI')
        from .bridge import submit
        return submit(task, config, stage, prompt, schema)
    logs = task/'logs'
    logs.mkdir(exist_ok=True)
    (logs/f'{stage}.prompt.txt').write_text(prompt)
    answer = logs/f'{stage}.answer.json'
    args = [config['codex'],'exec','--model',config['model'],'--ephemeral','--json',
            '--skip-git-repo-check','--sandbox','read-only' if schema else 'workspace-write',
            '-c','approval_policy="never"',*codex_provider_args(config),
            '--cd',str(task),'--output-last-message',str(answer)]
    if config.get('reasoning_effort') is not None:
        effort=config['reasoning_effort']
        if effort not in {'low','medium','high','xhigh'}:
            raise ValueError('Invalid Codex reasoning effort')
        args += ['-c', 'model_reasoning_effort='+json.dumps(effort)]
    if schema:
        write(logs/f'{stage}.schema.json', schema)
        args += ['--output-schema',str(logs/f'{stage}.schema.json')]
    for image in images or []:
        args += ['--image', str(image)]
    if config.get('tool_network'):
        args += ['-c', 'sandbox_workspace_write.network_access=true']
    args.append('-')
    # Keep only the explicitly configured provider credential; never inherit an
    # unrelated API route from the shared GameCraft environment.
    env = codex_environment(config)
    command(args, logs/f'{stage}.events.jsonl', cwd=task, timeout=config['timeout'], stdin=prompt, env=env)
    return read(answer) if schema else None


def validate_plan(task):
    from gamecraft_assets.plans import validate_generic
    plan = read(task/'generation/asset_plan.json')
    validate_generic(plan)
    if plan['task_id'] != task.name or not plan.get('style'):
        raise ValueError('Asset plan task/style mismatch')
    requirements = read(task/'requirements.json')['requirements']
    ids = [r['id'] for r in requirements]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Unique requirement IDs required')
    brief = (task/'instruction.md').read_text()
    assets = {a['id'] for a in plan['assets']}
    used = set()
    for r in requirements:
        if not r['quote'].strip() or r['quote'] not in brief or not r['verification'].strip():
            raise ValueError('Requirements must quote the brief and specify verification')
        if not isinstance(r['asset_ids'], list) or set(r['asset_ids']) - assets:
            raise ValueError('Unknown requirement asset')
        used.update(r['asset_ids'])
    if assets-used:
        raise ValueError('All generated assets must map to public requirements')
    return plan


def assets_worker(task, rounds, candidates, *, judge=True, video_skill=False):
    from gamecraft_assets.generate import generate
    from gamecraft_assets.plans import validate_generic
    root = task/'generation'
    plan = validate_plan(task)
    if not judge:
        from .unjudged import reuse
        reuse(root, plan)
    references = {}
    pending = []
    for asset in plan['assets']:
        # Match a character's reference across clips instead of inventing a new
        # identity per motion. The existing generator validates/caches the image.
        key = None
        if asset['kind'] == 'animation':
            key = asset['reference_prompt'].format(desc=asset['description'])
            target = root/'generated'/asset['id']/'reference.png'
            if key in references and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(references[key], target)
                shutil.copy2(references[key].with_suffix('.json'), target.with_suffix('.json'))
        pending.extend(generate(root, only=[asset['id']], rounds=rounds, candidates=candidates,
                                validator=validate_generic, judge=judge, video_skill=video_skill,
                                phase='initialization', auto_commit=True))
        if key and target.is_file() and target.with_suffix('.json').is_file():
            references[key] = target
    # One bounded retry for technical failures; never discard successful assets.
    retries_path = root/'technical_retries.json'
    retries = read(retries_path) if retries_path.exists() else {}
    for asset_id in list(pending):
        if not judge and retries.get(asset_id, 0) < 1:
            retries[asset_id] = retries.get(asset_id, 0) + 1
            write(retries_path, retries)
            if not generate(root, only=[asset_id], rounds=1, candidates=1, validator=validate_generic, judge=False,
                            video_skill=video_skill,phase='initialization',auto_commit=True):
                pending.remove(asset_id)
    write(root/'pending_assets.json', {'pending':pending, 'quality_judge':judge})
    if pending and judge:
        raise RuntimeError('H3 assets pending: '+', '.join(pending))


def install_assets(task):
    manifest = read(task/'generation/asset_manifest.json')
    bindings = {}
    missing = []
    for asset in validate_plan(task)['assets']:
        if asset['id'] not in manifest['assets']:
            missing.append(asset['id'])
            bindings[asset['id']] = [f'assets/{asset["id"]}/frame{i:04d}.png' for i in range(asset['frames'])]
            continue
        record = manifest['assets'][asset['id']]
        if len(record['files']) != asset['frames']:
            raise ValueError('Generated frame count mismatch')
        bindings[asset['id']] = []
        for i, name in enumerate(record['files']):
            src = inside(task/'generation', name)
            target = task/'draft/game/assets'/asset['id']/f'frame{i:04d}.png'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            bindings[asset['id']].append(str(target.relative_to(task/'draft/game')))
    for name in ('asset_plan.json','asset_manifest.json'):
        shutil.copy2(task/'generation'/name, task/'draft'/name)
    if (task/'generation/video_skill').is_dir():
        shutil.copytree(task/'generation/video_skill',task/'draft/video_skill',dirs_exist_ok=True)
    write(task/'draft/missing_assets.json', {'missing':missing, 'ready_for_training':not missing})
    write(task/'draft/asset_bindings.json', bindings)
    write(task/'immutable_inputs.json', guard_files(task))


def verify_requirements(task):
    rows = read(task/'draft/verification.json')['requirements']
    ids = [r['id'] for r in rows]
    expected = {r['id'] for r in read(task/'requirements.json')['requirements']}
    if set(ids) != expected or len(ids) != len(set(ids)):
        raise ValueError('Requirement coverage mismatch: missing='
                         +str(sorted(expected-set(ids)))+', extra='
                         +str(sorted(set(ids)-expected))+', duplicates='
                         +str(sorted({i for i in ids if ids.count(i)>1})))
    for r in rows:
        if r['status'] != 'implemented' or not r['code'] or not r['evidence'] or not r['explanation'].strip():
            raise ValueError(f'Incomplete requirement {r["id"]}')
        for name in r['code'] + r['evidence']:
            if not inside(task/'draft', name).is_file():
                raise ValueError(f'Missing requirement evidence: {name}')
    demos = list((task/'draft/game/demo_outputs').glob('*.json'))
    if not 1 <= len(demos) <= 10:
        raise ValueError('Need 1–10 public demo traces')
    for p in demos:
        d = read(p)
        if type(d.get('duration_frames')) is not int or not 1 <= d['duration_frames'] <= 600 or not d.get('events'):
            raise ValueError('Invalid public demo trace')


def guard_files(task):
    paths = [task/'instruction.md',task/'requirements.json',task/'generation/asset_plan.json',
             task/'generation/asset_manifest.json',task/'draft/asset_plan.json',task/'draft/asset_bindings.json']
    paths += list((task/'draft/game/assets').rglob('*.png'))
    paths += list((task/'draft/video_skill').rglob('*')) if (task/'draft/video_skill').is_dir() else []
    paths = [p for p in paths if p.is_file()]
    return {str(p):sha(p) for p in paths}


REVIEW_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['passed','requirements','missing_requirements','limitations'],
    'properties':{'passed':{'type':'boolean'},'missing_requirements':{'type':'array','items':{'type':'string'}},
      'limitations':{'type':'array','items':{'type':'string'}},
      'requirements':{'type':'array','items':{'type':'object','additionalProperties':False,
        'required':['id','passed','evidence','reason'], 'properties':{
          'id':{'type':'string'},'passed':{'type':'boolean'},'evidence':{'type':'array','items':{'type':'string'}},'reason':{'type':'string'}}}}}}


def review_passed(task, report):
    expected = {r['id'] for r in read(task/'requirements.json')['requirements']}
    rows = report['requirements']
    if not report['passed'] or report['missing_requirements'] or len(rows)!=len(expected) or {r['id'] for r in rows}!=expected:
        return False
    return all(r['passed'] and r['evidence'] and all(inside(task, f).is_file() for f in r['evidence']) for r in rows)


def initialize(task, config, contract):
    state = read(task/'state.json')
    if state['stage'] == 'code_ready_missing_assets':
        state = {'stage':'planned'}
        write(task/'state.json',state)
    if state['stage'] == 'ready':
        from game_initialization.package import inspect_package
        inspect_package(task/'initial-v0')
        return
    if sha(task/'instruction.md') != config['brief_sha256'][task.name]:
        raise RuntimeError('Public brief changed')
    refs = '\nFramework references (read-only): '+str(REPO/'game_initialization/package.py')+'; '+str(REPO/'gamecraft_behavior/adapters/base.py')+'; '+str(REPO/'gamecraft_behavior/runtime.py')+'; '+str(REPO/'gamecraft_training/package/config.py')
    base = contract+'\nTask: '+task.name+refs+'\nGodot binary: '+config['godot']
    if config.get('codex_queue'):
        base += '\nYou run on the developer host. The parent runs H3, Godot import/start and replay checks on the training host after you return. Do not require local Xvfb or start remote jobs. Inspect shared validation logs on repair attempts.'
    if not config.get('asset_judge', True):
        base += '\nUSER OVERRIDE: Initial H3 assets are used as generated without visual quality judging. Do not reject or regenerate for appearance, identity, motion quality or matte scores. Review only functional implementation, requirement coverage in code, actual asset bindings and runtime/replay correctness. Preserve unjudged provenance; visual improvements belong to later training.'
    if state['stage'] == 'prepared':
        codex(task, config, 'plan', base+'\nExecute PLAN STAGE only. Read instruction.md in full.')
        validate_plan(task)
        write(task/'generation/asset_manifest.json', {'schema_version':1,'task_id':task.name,'assets':{}})
        state = {'stage':'planned'}
        write(task/'state.json', state)
    if state['stage'] == 'planned':
        env = dict(os.environ, PYTHONPATH=str(REPO), H3_URL=config['h3_url'],
                   H3_BACKEND=config.get('h3_backend','local'), LOCAL_URL=config['local_url'],
                   LLM_BACKEND='local', OPTIMIZER_BACKEND='local')
        command([config['asset_python'],'-m','game_initialization.batch','assets-worker','--output',str(task),
                 '--h3-rounds',str(config['h3_rounds']),'--candidates',str(config['candidates'])] +
                ([] if config.get('asset_judge', True) else ['--no-asset-judge']) +
                (['--video-skill'] if config.get('video_skill',False) else []),
                task/'logs/h3.log',env=env,timeout=config['timeout']*4)
        install_assets(task)
        state = {'stage':'assets_ready','next_attempt':0}
        write(task/'state.json',state)
    missing_path = task/'draft/missing_assets.json'
    missing = read(missing_path)['missing'] if missing_path.exists() else []
    if missing:
        base += '\nSome initial files failed technical processing: '+json.dumps(missing)+'. Continue implementing all independent game logic and UI, using the planned paths and explicit load guards. Do not invent substitute assets, shrink requirements or mark the package complete. The parent will retain this as code_ready_missing_assets until missing files are resolved.'
    protected = read(task/'immutable_inputs.json')
    if guard_files(task) != protected:
        raise RuntimeError('Immutable inputs changed since asset installation')
    for attempt in range(state.get('next_attempt',0), config['repairs']+1):
        write(task/'state.json', {'stage':'implementing','next_attempt':attempt+1})
        try:
            feedback = (task/'last_error.txt').read_text() if (task/'last_error.txt').exists() else ''
            codex(task,config,f'implement-{attempt}',base+'\nExecute IMPLEMENT STAGE. Fix any prior issues:\n'+feedback)
            if guard_files(task) != protected:
                raise RuntimeError('Codex modified immutable plan/assets/evidence inputs')
            if missing:
                write(task/'state.json', {'stage':'code_ready_missing_assets','missing':missing,'next_attempt':attempt+1,'ready_for_training':False})
                return
            verify_requirements(task)
            write(task/'draft/initialization.json', {'schema_version':1,'task_id':task.name,'version':'v0','state':'draft',
                  'origin':{'instruction_sha256':config['brief_sha256'][task.name]},'generator':config['model'],
                  'asset_generator':'H3','video_skill':config.get('video_skill',False),'official_benchmark_metric':False})
            from game_initialization.package import inspect_package, freeze
            from gamecraft_training.package.config import load
            _, adapter, cases = inspect_package(task/'draft',sealed=False)
            load(task/'draft',adapter,cases)
            command([config['godot'],'--headless','--path',str(task/'draft/game'),'--editor','--quit'],
                    task/f'logs/import-{attempt}.log',timeout=120)
            command([config['godot'],'--headless','--path',str(task/'draft/game'),'--quit-after','5'],
                    task/f'logs/start-{attempt}.log',timeout=60)
            for kind in ('import','start'):
                if re.search(r'(^|\n)(SCRIPT ERROR|ERROR):', (task/f'logs/{kind}-{attempt}.log').read_text()):
                    raise RuntimeError(f'Godot {kind} reported errors')
            candidate = task/f'checks/attempt-{attempt}'
            freeze(task/'draft',candidate,config['godot'])
            report = codex(task,config,f'review-{attempt}',base+'\nExecute REVIEW STAGE only. Replay evidence: '+str(candidate/'checks')+
                           '\nReport evidence paths relative to this task workspace. Explicitly check brief requirements missing from the author checklist.',schema=REVIEW_SCHEMA)
            if not review_passed(task,report):
                raise RuntimeError('Independent review failed: '+json.dumps(report,ensure_ascii=False))
            inspect_package(candidate)
            shutil.copytree(candidate,task/'initial-v0')
            write(task/'state.json', {'stage':'ready','model':config['model'],'review':f'logs/review-{attempt}.answer.json',
                                    'package':'initial-v0','scope':'automatic initialization checks; not official benchmark evaluation'})
            return
        except Exception as exc:
            (task/'last_error.txt').write_text(str(exc))
            if guard_files(task) != protected:
                raise RuntimeError('Immutable inputs changed; refusing automatic retry') from exc
    raise RuntimeError('Repair budget exhausted; see '+str(task/'last_error.txt'))


def run(args):
    root = args.output.resolve()
    config = read(root/'batch.json')
    with (root/'.batch.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Serializes this runner across different batch roots on the same host.
        with open('/tmp/play2evolve-game-initialization.lock','w') as host_lock:
            fcntl.flock(host_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            preflight(root, config)
            if args.preflight_only:
                print('Runtime preflight passed. Model access is checked by the first Codex call.')
                return
            if sha(root/'contract.txt') != config['contract_sha256']:
                raise RuntimeError('Initialization contract changed')
            # One small read-only call checks this exact model/auth route before H3 spending.
            probe = codex(root,config,'model-probe','Return {"ok": true}. Do not use tools or read files.',
                  schema={'type':'object','additionalProperties':False,'required':['ok'],
                          'properties':{'ok':{'type':'boolean','enum':[True]}}})
            if probe != {'ok':True}:
                raise RuntimeError('Codex model probe returned an invalid answer')
            selected = args.only or config['tasks']
            if set(selected)-set(config['tasks']):
                raise ValueError('Unknown task selection')
            summary = {}
            for task in selected:
                try:
                    initialize(root/task,config,(root/'contract.txt').read_text())
                    summary[task] = read((root/task)/'state.json')['stage']
                except Exception as exc:
                    summary[task] = str(exc)
                    write(root/'summary.json',summary)
                    # Fail fast: do not spend ten tasks retrying auth/service errors.
                    raise
                write(root/'summary.json',summary)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','run','assets-worker'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bench',type=Path,default=benchmark_root(required=False))
    p.add_argument('--tasks',type=Path,default=HERE/'tasks.json')
    p.add_argument('--model',default='gpt-6-sol')
    p.add_argument('--host',default=socket.gethostname())
    p.add_argument('--codex',default=os.getenv('CODEX_BIN','codex'))
    add_codex_provider_arguments(p)
    p.add_argument('--codex-queue',type=Path,help='Shared job directory for developer Codex worker')
    p.add_argument('--asset-python',type=Path,default=asset_python())
    p.add_argument('--godot',type=Path,default=godot_binary())
    p.add_argument('--h3-url',default=os.getenv('H3_URL','http://127.0.0.1:30010'))
    p.add_argument('--h3-backend',choices=['local','minimax','wan'],default='local')
    p.add_argument('--local-url',default='http://127.0.0.1:30020/v1')
    p.add_argument('--h3-rounds',type=int,choices=range(1,4),default=2)
    p.add_argument('--candidates',type=int,choices=range(1,5),default=1)
    p.add_argument('--repairs',type=int,choices=range(0,6),default=2)
    p.add_argument('--timeout',type=int,default=7200)
    p.add_argument('--only',nargs='+')
    p.add_argument('--preflight-only',action='store_true')
    p.add_argument('--no-asset-judge',action='store_true')
    p.add_argument('--video-skill',action='store_true',help='Enable the optional video-generation skill; default preserves legacy generation')
    p.add_argument('--video-skill-path',type=Path,help='Optional global skill JSON or snapshot directory (requires --video-skill)')
    args = p.parse_args()
    if args.timeout < 1:
        p.error('timeout must be positive')
    if args.video_skill_path and not args.video_skill:
        p.error('--video-skill-path requires --video-skill')
    if args.command == 'prepare':prepare(args)
    elif args.command == 'run':run(args)
    else:assets_worker(args.output,args.h3_rounds,args.candidates,judge=not args.no_asset_judge,video_skill=args.video_skill)


if __name__ == '__main__':
    main()
