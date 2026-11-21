"""H3 tool used explicitly by the joint executor, without a source-quality Judge."""
import argparse
import fcntl
import time
import json
import shutil
import hashlib
import os
import uuid
from pathlib import Path
from gamecraft_assets.generate import generate
from gamecraft_assets.plans import validate_generic
from gamecraft_assets.contract import validate_bound
from gamecraft_behavior.core import write_json
from .config import relative


def _cache_key(root, plan, item, prompt, video_skill, phase):
    """Reuse only an identical request with identical conditioning inputs."""
    manifest=json.loads((root/'asset_manifest.json').read_text())['assets']
    inputs=[]
    if ref:=item.get('reference_asset_id'):
        for name in manifest[ref]['files']:
            inputs.append(hashlib.sha256(relative(root,name).read_bytes()).hexdigest())
    else:
        reference=root/'generated'/item['id']/'reference.png'
        if reference.is_file():
            inputs.append(hashlib.sha256(reference.read_bytes()).hexdigest())
    skill=root/'video_skill'
    if video_skill and skill.is_dir():
        for path in sorted(p for p in skill.rglob('*') if p.is_file()):
            inputs.append((path.relative_to(skill).as_posix(),hashlib.sha256(path.read_bytes()).hexdigest()))
    request={'asset':item,'prompt':prompt,'style':plan['style'],'phase':phase,
             'video_skill':video_skill,'inputs':inputs}
    return hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()


def _host_request(job, asset_id, prompt, timeout):
    """Ask the parent worker to run GPU generation outside the Codex sandbox."""
    queue=job/'material-requests';queue.mkdir(exist_ok=True)
    name=uuid.uuid4().hex
    request=queue/f'{name}.request.json'
    response=queue/f'{name}.response.json'
    temporary=queue/f'{name}.request.tmp'
    temporary.write_text(json.dumps({'asset':asset_id,'prompt':prompt}))
    os.replace(temporary,request)
    deadline=time.monotonic()+timeout
    while not response.is_file():
        if time.monotonic()>deadline:
            raise TimeoutError('Host material worker did not answer; see material-requests/')
        time.sleep(.2)
    result=json.loads(response.read_text())
    if result['status']!='completed':raise RuntimeError(result['reason'])
    return result['record']


def invoke(job, asset_id, prompt, *, host=False):
    job=Path(job).resolve()
    context=json.loads((job/'material_tool.json').read_text())
    if asset_id not in context['asset_ids']:raise ValueError('Asset outside this candidate plan')
    if not prompt.strip() or len(prompt)>4000:raise ValueError('Expected a complete H3 prompt (1–4000 chars)')
    if context.get('host_execution') and not host:
        return _host_request(job,asset_id,prompt,float(context.get('host_tool_timeout',1800)))
    root=job/'generation'
    plan=json.loads((root/'asset_plan.json').read_text())
    item=next(a for a in plan['assets'] if a['id']==asset_id)
    item['prompt']=prompt.strip();item['revision']=item.get('revision',0)+1
    validate_generic(plan)
    # Reserve before the request: failures and concurrent calls consume the same budget.
    with (root/'.h3-budget.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ledger=root/'h3_attempts.json'
        attempts=json.loads(ledger.read_text()) if ledger.exists() else []
        if sum(r['asset']==asset_id for r in attempts)>=1:
            raise RuntimeError('H3 budget exhausted: one generation per asset/candidate, no retries (including failures). Integrate available output or report failure; do not regenerate.')
        attempts.append({'asset':asset_id,'started':time.time()})
        write_json(ledger,attempts)
    write_json(root/'asset_plan.json',plan)
    reused=False
    cache_root=Path(context['shared_cache']).resolve() if context.get('shared_cache') else None
    key=_cache_key(root,plan,item,item['prompt'],context['video_skill'],context['phase'])
    cache=cache_root/key if cache_root else None
    if cache_root:
        cache_root.mkdir(parents=True,exist_ok=True)
        with (cache_root/'.lock').open('a') as cache_lock:
            fcntl.flock(cache_lock,fcntl.LOCK_EX)
            if cache.is_dir() and (cache/'record.json').is_file():
                record=json.loads((cache/'record.json').read_text())
                source_prefix=Path('generated')/asset_id
                if all((cache/'generated'/Path(name).relative_to(source_prefix)).is_file()
                       for name in record['files']):
                    target=root/'generated'/asset_id
                    if target.exists():shutil.rmtree(target)
                    shutil.copytree(cache/'generated',target)
                    manifest=json.loads((root/'asset_manifest.json').read_text())
                    manifest['assets'][asset_id]=record
                    write_json(root/'asset_manifest.json',manifest)
                    reused=True
            if not reused:
                pending=generate(root,only=[asset_id],rounds=1,candidates=1,validator=validate_generic,
                                 judge=False,video_skill=context['video_skill'],phase=context['phase'],
                                 auto_commit=False,propose_video_patch=False)
                if pending:raise RuntimeError('H3 failed to produce readable material; inspect generation/asset_history.json')
                record=json.loads((root/'asset_manifest.json').read_text())['assets'][asset_id]
                if cache.exists():shutil.rmtree(cache)
                cache.mkdir()
                shutil.copytree(root/'generated'/asset_id,cache/'generated')
                write_json(cache/'record.json',record)
    else:
        pending=generate(root,only=[asset_id],rounds=1,candidates=1,validator=validate_generic,
                         judge=False,video_skill=context['video_skill'],phase=context['phase'],
                         auto_commit=False,propose_video_patch=False)
        if pending:raise RuntimeError('H3 failed to produce readable material; inspect generation/asset_history.json')
    entry=json.loads((root/'asset_manifest.json').read_text())['assets'][asset_id]
    files=entry['files'];targets=context['bindings'][asset_id]
    if len(files)!=len(targets):raise ValueError('Generated frame count differs from planned binding')
    validate_bound(root,item,files)
    for src,dst in zip(files,targets):
        target=relative(job/'project',dst);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(relative(root,src),target)
    record={'asset':asset_id,'status':'generated','version':entry['version'],
            'files':targets,'quality_status':'executor_review_required','reused':reused}
    history=root/'tool_calls.json'
    records=json.loads(history.read_text()) if history.exists() else []
    records.append(record);write_json(history,records)
    return record


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--job',default='.')
    parser.add_argument('--asset',required=True)
    parser.add_argument('--prompt-file',required=True)
    args=parser.parse_args()
    print(json.dumps(invoke(args.job,args.asset,Path(args.prompt_file).read_text()),ensure_ascii=False))

if __name__=='__main__':main()
