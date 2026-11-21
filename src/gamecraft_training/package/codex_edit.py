"""Codex edits an isolated full project; the parent owns validation and acceptance."""
import argparse
import difflib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_provider import add_arguments as add_codex_provider_arguments
from codex_provider import child_environment as codex_environment
from codex_provider import cli_args as codex_provider_args
from codex_provider import from_args as codex_provider_from_args
from codex_provider import from_environment as codex_provider_from_environment
from gamecraft_behavior.core import digest, write_json
from gamecraft_composition.core import snapshot
from .config import relative
from .evidence import sample

EXTENSIONS={'.gd','.tscn','.tres','.godot','.json','.gdshader'}
DEFAULT_CODEX_MODEL='gpt-6-sol'
SCHEMA={'type':'object','additionalProperties':False,'required':['summary','tests'],
        'properties':{'summary':{'type':'string'},'tests':{'type':'array','items':{'type':'string'}}}}
PROMPT='''You are the game-code editor for a Godot game. Read request.json and evidence/.
The unified repair plan in request.json/diagnosis owns decisions about material versus code. Follow
diagnosis.code_instructions and inspect the actual generated files already present in project/assets before
editing. If the material differs from the plan, adapt the integration to what was actually generated
and explain any unsupported instruction. When material_tool.json is present, YOU own H3 calls and code
integration: read material_tool.json and material_plan.json first. Write a complete prompt to a file and
invoke the tool EXACTLY ONCE per required asset (no retries, including failures). Run material
tool calls sequentially because they update one shared manifest. Inspect the resulting
images, then adapt code to integrate the actual outputs. Finalize all prompt revisions BEFORE generation. No separate material Judge
will approve or reject your images. Do not merely claim generation: invoke the tool. You may alternate
H3 generation, image inspection, code changes and local replay within this same execution.
After the single attempt use the available material and complete integration, or report generation failure; do not bypass the tool, rename
assets, reset its budget or call H3 directly to obtain additional attempts.
The planning agent supplies requirements and initial prompts, not pre-generated material.
For a whole-round candidate, diagnosis.boundary_plan.repair_units lists every issue group and its
material/code boundary. Implement ALL units together in this one project. Multiple groups may
change the video boundary in this candidate. Do not inspect sibling projects.
diagnosis.generated_materials lists the actual new bindings already installed in project/. Inspect those
frames before editing. Consume their declared frame count, fps, alpha, and reference identity; do not assume
the requested motion was generated perfectly. Remove duplicate rendering of video-owned subjects only
in the targeted states. Keep input timing, interruption handling, collision/state outcomes, and UI owned
by code as declared. Check the final action, landing and completion transition.
For a motion upgrade from a static sprite, bind and play the NEW animation during the
observed action, return to an appropriate idle visual afterward, and preserve code-owned world
position, facing, collision, input and game state. Verify that the game displays changing frames
rather than loading only frame zero. Do not draw the old static subject over the new animation.
You are editing only one
candidate; do not read or modify sibling variants. The parent compares complete games, not source claims.
Edit project/ directly to repair the Judge's diagnosed playtest issue.
Read the COMPLETE project as needed. You may create/refactor/delete any Godot game code,
including GDScript gameplay/state-machine/opponent logic, rendering, UI, scenes, resources
and project configuration. You may modify assets as well as code; a .gd change is not required when the repair needs only resources.
Follow the profile in quality.execution_profile. For initialization prioritize required functionality,
complete materials, correct scene roles and essential feedback; do not spend this budget on optional
beauty or polish. For RSI consider the Judge's evidenced findings across rules and controls,
challenge and strategy, content and progression, feedback and readability, and visual presentation
without a fixed category priority. When a diagnosed design problem requires changing
game rules, make the smallest justified change and preserve the game's documented goals and fair challenge.
Do not change difficulty merely to increase Agent survival or score. Check affected
screens and states, not just whether the project builds. Never fake telemetry, outcomes or tests.
Generated foreground files are canonical RGBA assets. Consume their stored alpha exactly once.
Never apply a second color-based chroma key, delete dark/gray subject pixels, or crop opaque pixels
unless the package explicitly declares such a transformation in its asset contract.
Do not change external evaluation evidence or schemas. Work only in this candidate workspace,
including project/assets and generation/. Do not read other experiments.
The diagnosis is a hypothesis: verify against code/images; explain when unsupported.
Do not force a change if the evidence does not support it. Generate new assets through the provided H3 tool when needed.
Keep unrelated behavior. The parent process independently builds and records the game.
Initialization authors candidate-specific inputs after your edit and compares each version's own playtest.
RSI replays the recorded inputs. The parent Judge decides acceptance from observed evidence;
your own tests or claims never grant acceptance.
Run scratch checks outside project/. Do not create project/tests or project/.runtime;
the parent discards newly created scratch directories before auditing the edit.
Return JSON with summary and tests (state clearly which tests were actually run).
'''

UPGRADE_PROMPT='''You are implementing one milestone of a THREE-ROUND RSI game upgrade.
Read request.json, evidence/, the full project, and material_plan.json when present.
The goal may be a substantial gameplay feature, aesthetic redesign, or both. Implement
the current milestone as a coherent playable version; do not reduce it to a minimal
bug fix. Preserve the original benchmark requirements, legal controls, and existing
playable paths. You may refactor Godot scripts, scenes, resources, UI and assets.
The project/ directory is your candidate. Do not inspect sibling candidates or
modify external evidence, tests, task rules, evaluation files, or protected paths.
If material_tool.json exists, YOU own the planned H3 requests and integration.
Write a complete prompt for each planned material to a file and invoke the tool
EXACTLY ONCE per material. Never reset its budget or call H3 outside the tool.
Inspect the actual generated frames before adapting placement, playback and state
transitions. Video may own appearance or coordinated motion; Godot must retain
input timing, collision, physics, scoring, HUD, and outcomes. Avoid duplicate
rendering of subjects represented in the generated clip. If a material request
fails, report it honestly and complete what remains possible without fabrication.
Check the relevant old and new interactions, not only Godot import. The host will
run a fresh Player-owned playtest for both core gameplay and the upgrade; your
own tests and claims do not grant acceptance. Run scratch checks outside project/.
Return JSON with summary and tests, stating only tests actually run.
'''


def validate_project(project,godot,log,timeout=120):
    """Run the real Godot importer; its exit status alone does not expose script errors."""
    project,log=Path(project).resolve(),Path(log).resolve()
    xdg=log.parent/'godot-xdg'
    for name in ('data','config','cache'): (xdg/name).mkdir(parents=True,exist_ok=True)
    env={**os.environ,'GODOT_SILENCE_ROOT_WARNING':'1','LIBGL_ALWAYS_SOFTWARE':'1','LP_NUM_THREADS':'1',
         'XDG_DATA_HOME':str(xdg/'data'),'XDG_CONFIG_HOME':str(xdg/'config'),'XDG_CACHE_HOME':str(xdg/'cache')}
    try:
        with log.open('w') as stream:
            result=subprocess.run([godot,'--headless','--editor','--path',str(project),'--import'],
                                  stdout=stream,stderr=subprocess.STDOUT,env=env,timeout=timeout)
        text=log.read_text(errors='replace')
        markers=('SCRIPT ERROR','Parse Error','Failed to load script')
        failed=result.returncode!=0 or any(marker in text for marker in markers)
        lines=[line for line in text.splitlines() if any(marker in line for marker in markers) or 'ERROR:' in line]
        reason='\n'.join(lines[-40:]) if lines else f'Godot exited {result.returncode}'
        return not failed,reason
    except Exception as exc:
        log.write_text(f'{type(exc).__name__}: {exc}\n')
        return False,f'{type(exc).__name__}: {exc}'


def policy(spec, override=None):
    value=override if override is not None else spec.get('code_edit')
    if not isinstance(value,dict) or not isinstance(value.get('protected'),list):
        raise ValueError('Codex composition requires code_edit.protected or --composition-policy JSON')
    for name in value['protected']:
        if not isinstance(name,str) or not name or name.startswith('/') or any(p in ('.','..') or p.startswith('.') for p in name.split('/')):
            raise ValueError('Protected paths must be project-relative files/directories')
    return value


def audit(original,candidate,protected):
    original,candidate=Path(original),Path(candidate)
    if any(p.is_symlink() for p in candidate.rglob('*')):raise ValueError('Candidate contains symlinks')
    before,after=digest(original),digest(candidate)
    changes=sorted(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k))
    # File differences are provenance, not a visual/code acceptance gate.
    if 'project.godot' not in after:raise ValueError('Candidate removed project.godot')
    diff=[]
    for name in changes:
        try:
            old=(original/name).read_text().splitlines(True) if name in before else []
            new=(candidate/name).read_text().splitlines(True) if name in after else []
        except UnicodeError:
            diff.append(f'Binary file changed: {name}\n');continue
        diff.extend(difflib.unified_diff(old,new,fromfile='before/'+name,tofile='after/'+name))
    return changes,''.join(diff)


def discard_new_scratch(baseline,candidate):
    """Discard worker-created test/runtime scratch, never a baseline project directory."""
    baseline,candidate=Path(baseline),Path(candidate)
    for name in ('.runtime','tests'):
        path=candidate/name
        if not (baseline/name).exists() and path.exists() and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def serve_material_requests(root):
    """Run Codex-authored material requests on the parent host with GPU access."""
    root=Path(root).resolve()
    queue=root/'material-requests'
    if not queue.is_dir():return 0
    from .material_tool import invoke
    handled=0
    for request in sorted(queue.glob('*.request.json')):
        response=request.with_name(request.name.replace('.request.json','.response.json'))
        if response.exists():continue
        try:
            data=json.loads(request.read_text())
            record=invoke(root,data['asset'],data['prompt'],host=True)
            result={'status':'completed','record':record}
        except Exception as exc:
            result={'status':'error','reason':f'{type(exc).__name__}: {exc}'}
        temporary=response.with_suffix('.tmp')
        write_json(temporary,result)
        os.replace(temporary,response)
        handled+=1
    return handled


def _model_at_capacity(output):
    """Recognize a transient Codex service failure, not arbitrary editor errors."""
    failure=''
    with Path(output).open(errors='replace') as stream:
        for line in stream:
            try: event=json.loads(line)
            except json.JSONDecodeError:continue
            if event.get('type') not in ('error','turn.failed'):continue
            failure=str(event.get('message',event.get('error','')))
    return 'selected model is at capacity' in failure.lower()


def execute_job(root,*,binary='codex',model=DEFAULT_CODEX_MODEL,timeout=900,godot=None,validation_retries=2,
                provider=None,capacity_retries=1,capacity_retry_delay=30):
    root=Path(root).resolve()
    # Atomic claim across developer-machine workers; never execute a job twice.
    with (root/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        if (root/'response.json').exists() or (root/'started.json').exists():return False
        if time.time()>json.loads((root/'job.json').read_text())['expires']:
            write_json(root/'response.json',{'status':'error','reason':'Job expired before a worker started'})
            return False
        write_json(root/'started.json',{'pid':os.getpid(),'time':time.time()})
        provider=provider or codex_provider_from_environment()
        env=codex_environment(provider)
        try:
            if validation_retries<0 or validation_retries>5:raise ValueError('validation_retries must be 0–5')
            if capacity_retries<0 or capacity_retries>3:raise ValueError('capacity_retries must be 0–3')
            if capacity_retry_delay<0:raise ValueError('capacity_retry_delay must be nonnegative')
            job_deadline=time.monotonic()+timeout
            answer=None
            failure=''
            for attempt in range(validation_retries+1):
                suffix='' if attempt==0 else f'-retry-{attempt}'
                output=root/f'codex{suffix}.jsonl';stderr=root/f'codex{suffix}.stderr'
                command=[binary,'exec','--json','--ephemeral','--skip-git-repo-check','--sandbox','workspace-write',
                         '-c','approval_policy="never"',*codex_provider_args(provider),
                         '--cd',str(root),'--output-schema',str(root/'schema.json'),
                         '--output-last-message',str(root/'answer.json')]
                if effort:=os.environ.get('GAMECRAFT_CODEX_REASONING_EFFORT'):
                    command+=['-c',f'model_reasoning_effort="{effort}"']
                if (root/'material_tool.json').exists():
                    command+=['-c','sandbox_workspace_write.network_access=true']
                if model:command+=['--model',model]
                command+=['-']
                if attempt==0:
                    request=json.loads((root/'request.json').read_text())
                    prompt=UPGRADE_PROMPT if request.get('upgrade_goal') else PROMPT
                    if godot:
                        prompt+=(f'\nThe Godot executable is {godot}. Before returning, run it headlessly against project/ '
                                 'and fix every parse/import error. The worker will independently enforce this check.\n')
                else:
                    validation_log=f'godot-validation-{attempt:03}.log'
                    prompt=(PROMPT+f'\nA previous edit failed mandatory Godot validation. Read {validation_log}, '
                            'fix every reported error in project/, rerun the available Godot executable, and return a corrected result. '
                            f'The validation failure was:\n{failure}\n')
                for capacity_attempt in range(capacity_retries+1):
                    if capacity_attempt:
                        retry_suffix=f'{suffix}-capacity-retry-{capacity_attempt}'
                        output=root/f'codex{retry_suffix}.jsonl'
                        stderr=root/f'codex{retry_suffix}.stderr'
                        retry_prompt=(prompt+'\nThe preceding Codex run ended because the model was at capacity. '
                                      'Continue from the current project/ and generation/ state. '
                                      'Inspect existing material-tool records and never repeat an H3 request already attempted.\n')
                    else:
                        retry_prompt=prompt
                    (root/'answer.json').unlink(missing_ok=True)
                    with output.open('w') as log,stderr.open('w') as err:
                        proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=err,cwd=root,env=env,start_new_session=True,text=True)
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future=pool.submit(proc.communicate,retry_prompt,None)
                            while not future.done():
                                if (root/'material_tool.json').exists():serve_material_requests(root)
                                if time.monotonic()>=job_deadline:
                                    os.killpg(proc.pid,signal.SIGKILL)
                                    future.result()
                                    raise RuntimeError('Codex edit timed out')
                                time.sleep(.2)
                            future.result()
                    if not proc.returncode:break
                    if not _model_at_capacity(output):
                        raise RuntimeError(f'Codex exited {proc.returncode}; see {stderr.name}')
                    if capacity_attempt==capacity_retries:
                        raise RuntimeError(f'Codex model remained at capacity after {capacity_attempt+1} attempts; see {output.name}')
                    if time.monotonic()+capacity_retry_delay>=job_deadline:
                        raise RuntimeError('Codex model at capacity; no time remains for a retry')
                    time.sleep(capacity_retry_delay)
                answer=json.loads((root/'answer.json').read_text())
                if not isinstance(answer.get('summary'),str) or not isinstance(answer.get('tests'),list):
                    raise ValueError('Invalid Codex summary')
                if not godot:break
                validation_log=root/f'godot-validation-{attempt+1:03}.log'
                remaining=job_deadline-time.monotonic()
                if remaining<=0:raise RuntimeError('Codex edit exhausted the timeout before Godot validation')
                ok,failure=validate_project(root/'project',godot,validation_log,timeout=min(120,remaining))
                write_json(root/f'godot-validation-{attempt+1:03}.json',
                           {'passed':ok,'godot':str(godot),'log':validation_log.name,'reason':failure})
                if ok:
                    answer['tests'].append(f'Mandatory Godot import passed with {godot}')
                    break
                if attempt==validation_retries:
                    raise RuntimeError(f'Godot validation failed after {attempt+1} Codex attempts: {failure}')
            write_json(root/'response.json',{'status':'completed','answer':answer})
        except Exception as exc:
            write_json(root/'response.json',{'status':'error','reason':f'{type(exc).__name__}: {exc}'})
        return True


class CodexEditor:
    supports_material_tools=True
    def __init__(self,*,binary='codex',model=DEFAULT_CODEX_MODEL,timeout=900,queue=None,provider=None,godot=None,
                 history_window=5):
        if type(history_window) is not int or history_window<0:
            raise ValueError('Codex editor history window must be a nonnegative integer')
        self.binary,self.model,self.timeout=binary,model,timeout
        self.history_window=history_window
        self.queue=Path(queue).resolve() if queue else None
        self.provider=provider or codex_provider_from_environment()
        self.godot=godot

    def __call__(self,project,candidate,out,*,diagnosis,evaluation,episodes,history,adapter,policy,material_execution=None):
        out=Path(out).resolve();project=Path(project).resolve()
        before=digest(project)
        # Keep a per-attempt baseline even if the caller later advances to a new world.
        baseline=out/'before'
        snapshot(project,baseline)
        write_json(out/'comparison.json',{'original_source':str(project),'before':str(baseline),
                   'candidate':str(Path(candidate).resolve()),'before_sha256':before,
                   'status':'editing','note':'Candidate generation is not acceptance; consult result.json for the training verdict.'})
        # Build privately, then publish ready marker only after every input is present.
        jobs=self.queue or out/'codex-jobs';jobs.mkdir(parents=True,exist_ok=True)
        root=jobs/uuid.uuid4().hex;root.mkdir()
        snapshot(baseline,root/'project')
        if material_execution is not None:
            from .edits import Generator
            from gamecraft_video_skill import copy_skill
            import shlex
            ctx=material_execution
            cfg={**ctx['config'],'_reference_project':str(root/'project')}
            gen=Generator(root/'generation',ctx['plan'],cfg,ctx['python'],ctx['video_skill'],1)
            if ctx['video_skill'] and Path(ctx['skill_source']).is_dir():
                shutil.rmtree(gen.root/'video_skill');copy_skill(ctx['skill_source'],gen.root)
            shared_cache=root/'shared_cache'
            if ctx.get('cache_source') and Path(ctx['cache_source']).is_dir():
                shutil.copytree(ctx['cache_source'],shared_cache)
            else:
                shared_cache.mkdir()
            write_json(root/'material_plan.json',ctx['option'])
            tool={'asset_ids':[e['asset']['id'] for e in ctx['option']['materials']],
                  'bindings':cfg['assets']['bindings'],'video_skill':ctx['video_skill'],
                  'phase':cfg['_profile']['name'],'max_attempts_per_asset':1,'max_retries_per_asset':0,
                  'host_execution':True,'host_tool_timeout':self.timeout,
                  'command':shlex.join([ctx['python'],'-m','gamecraft_training.package.material_tool',
                                       '--job',str(root)])+' --asset ASSET_ID --prompt-file PROMPT_FILE'}
            tool['shared_cache']=str(shared_cache)
            write_json(root/'material_tool.json',tool)
        (root/'evidence').mkdir()
        evidence=[]
        for i,item in enumerate(sample(episodes)):
            image=root/'evidence'/f'{i:03}.png';shutil.copyfile(item['image'],image)
            evidence.append({**{k:v for k,v in item.items() if k not in ('image','reason')},'image':str(image.relative_to(root))})
        request={'rules':adapter.instructions,'diagnosis':diagnosis,'quality':evaluation,'evidence':evidence,
                 'previous_attempts':history[-self.history_window:] if self.history_window else [],
                 'note':('Implement the current RSI upgrade milestone while preserving required old controls.'
                         if diagnosis.get('upgrade_goal') else
                         'Edit only the diagnosed issue. Preserve intended game goals and fair challenge; '
                         'a diagnosed gameplay defect may require a bounded rule change.')}
        if diagnosis.get('upgrade_goal'):
            request['upgrade_goal']=diagnosis['upgrade_goal']
            request['upgrade_round']=diagnosis['upgrade_round']
        write_json(root/'request.json',request);write_json(root/'schema.json',SCHEMA)
        evidence_digest=digest(root/'evidence')
        request_bytes=(root/'request.json').read_bytes()
        write_json(root/'job.json',{'expires':time.time()+self.timeout})
        write_json(out/'codex_job.json',{'job':str(root),'mode':'queue' if self.queue else 'direct'})
        (root/'ready').touch()
        if not self.queue:execute_job(root,binary=self.binary,model=self.model,timeout=self.timeout,
                                      provider=self.provider,godot=self.godot)
        deadline=time.monotonic()+self.timeout+30
        while not (root/'response.json').exists():
            if time.monotonic()>deadline:raise RuntimeError(f'Codex worker did not finish within timeout: {root}')
            time.sleep(1)
        response=json.loads((root/'response.json').read_text())
        if response['status']!='completed':raise RuntimeError(response['reason'])
        if digest(project)!=before:raise RuntimeError('Original project changed during editing')
        if digest(baseline)!=before:raise RuntimeError('Saved baseline changed during editing')
        if digest(root/'evidence')!=evidence_digest or (root/'request.json').read_bytes()!=request_bytes:
            raise ValueError('Codex modified evaluation evidence')
        discard_new_scratch(baseline,root/'project')
        changed,patch=audit(baseline,root/'project',policy['protected'])
        (out/'candidate.diff').write_text(patch)
        report={**response['answer'],'changed_files':changed,'job':str(root)}
        if material_execution is not None:
            calls=root/'generation/tool_calls.json'
            records=json.loads(calls.read_text()) if calls.exists() else []
            needed={e['asset']['id'] for e in material_execution['option']['materials']}
            if needed-{r['asset'] for r in records}:raise ValueError('Executor did not generate required planned materials')
            report.update(generations=records,material_plan=json.loads((root/'generation/asset_plan.json').read_text()),
                          generation_workspace=str(root/'generation'),
                          material_cache_workspace=str(root/'shared_cache'))
        write_json(out/'code_edit.json',report)
        snapshot(root/'project',candidate)
        write_json(out/'comparison.json',{'original_source':str(project),'before':str(baseline),
                   'candidate':str(Path(candidate).resolve()),'before_sha256':before,
                   'candidate_sha256':digest(candidate),'changed_files':changed,'status':'candidate_ready',
                   'note':'Candidate generation is not acceptance; consult result.json for the training verdict.'})
        return report


def main():
    p=argparse.ArgumentParser(description='Run Codex composition jobs on a machine with an authenticated Codex CLI')
    p.add_argument('--queue',required=True);p.add_argument('--once',action='store_true')
    p.add_argument('--codex-bin',default=os.getenv('CODEX_BIN','codex'));p.add_argument('--model',default=DEFAULT_CODEX_MODEL)
    add_codex_provider_arguments(p)
    p.add_argument('--timeout',type=int,default=900)
    p.add_argument('--godot',default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN'))
    p.add_argument('--validation-retries',type=int,default=2)
    a=p.parse_args();root=Path(a.queue).resolve();root.mkdir(parents=True,exist_ok=True)
    while True:
        for job in sorted(root.iterdir()):
            if job.is_dir() and (job/'ready').exists():
                execute_job(job,binary=a.codex_bin,model=a.model,timeout=a.timeout,
                            godot=a.godot,validation_retries=a.validation_retries,
                            provider=codex_provider_from_args(a))
        if a.once:return
        time.sleep(2)

if __name__=='__main__':main()
