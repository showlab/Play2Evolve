"""Shared H3 generation worker. All identities and requirements come from a plan."""
import hashlib
import fcntl
import json
import sys
from pathlib import Path
from gamecraft_assets.generate import generate, repair_viable
from gamecraft_assets.plans import validate_generic, write_json
from gamecraft_assets.prompt_repair import rewrite_prompt


def main():
    request=json.loads(Path(sys.argv[1]).read_text())
    candidates=request.get('candidates',1)
    if type(candidates) is not int or not 1 <= candidates <= 4:
        raise ValueError('Asset repair candidates must be an integer from 1 to 4')
    root=Path(request['workspace'])
    plan=json.loads((root/'asset_plan.json').read_text())
    validate_generic(plan)
    asset=next(a for a in plan['assets'] if a['id']==request['asset'])
    # Request history persists even if the resulting world candidate is rejected.
    # Keep the shared ledger lock short; Codex and H3 calls happen outside it.
    with (root/'.asset-request.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=root/'requests.json'
        requests=json.loads(path.read_text()) if path.exists() else []
        requests.append({'asset':asset['id'],'problem':request['problem']})
        write_json(path,requests)
        plan=json.loads((root/'asset_plan.json').read_text())
        asset=next(a for a in plan['assets'] if a['id']==request['asset'])
        revision=sum(r['asset']==asset['id'] for r in requests)
        failures=[r['problem'] for r in requests if r['asset']==asset['id']][-3:]
        original_prompt=asset['prompt']
        style=plan['style']
        asset_context=dict(asset)
    brief_path=Path(request['instruction_path']) if request.get('instruction_path') else None
    task_brief=brief_path.read_text() if brief_path and brief_path.is_file() else ''
    if 'planned_prompt' in request:
        prompt=request['planned_prompt']
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>4000:
            raise ValueError('Unified repair plan supplied an invalid H3 prompt')
        rewrite={'repairable':True,'prompt':prompt.strip(),
                 'reason':'Unified repair planner selected this material and prompt',
                 'model':'unified_repair_planner'}
    else:
        rewrite=rewrite_prompt(asset_context,style=style,problem=request['problem'],
                               history=failures,task_brief=task_brief)
    if not rewrite['repairable']:
        write_json(request['result'],{'status':'rejected','reason':rewrite['reason'],
                                      'prompt_rewrite':{'old_prompt':original_prompt,**rewrite}})
        print(json.dumps({'status':'rejected','asset':asset['id'],'reason':rewrite['reason']}),flush=True)
        return
    with (root/'.asset-request.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        plan=json.loads((root/'asset_plan.json').read_text())
        asset=next(a for a in plan['assets'] if a['id']==request['asset'])
        if asset.get('revision',0)>=revision:
            raise RuntimeError('A newer prompt revision already exists for this asset')
        asset['prompt']=rewrite['prompt']
        asset['revision']=revision
        write_json(root/'asset_plan.json',plan)
    def gate(verdict,metrics,kind):
        return repair_viable(verdict,metrics,kind)
    pending=generate(root,only=[asset['id']],rounds=1,candidates=candidates,validator=validate_generic,quality_gate=gate,
                     video_skill=request.get('video_skill',False),phase=request.get('phase','rsi'),
                     feedback=request['problem'] if request.get('video_skill',False) else None,auto_commit=False)
    history=json.loads((root/'asset_history.json').read_text())
    generated=[r for r in history if r['asset']==asset['id']][-candidates:]
    if pending:
        recent=generated[-1]
    else:
        selected_version=json.loads((root/'asset_manifest.json').read_text())['assets'][asset['id']]['version']
        recent=next(r for r in reversed(generated) if r['version']==selected_version)
    record={'status':'rejected' if pending else 'generated','candidate':recent}
    record['prompt_rewrite']={'old_prompt':original_prompt,**rewrite}
    record['selection']={'requested_candidates':candidates,
                         'generated_versions':[r['version'] for r in generated],
                         'eligible_versions':[r['version'] for r in generated if r.get('eligible')],
                         'selected_version':None if pending else recent['version'],
                         'criterion':'highest usable score among renderable candidates; integrated A/B decides promotion'}
    if recent.get('video_skill_patch'):
        record['video_skill_patch']=recent['video_skill_patch']
    if recent.get('error'):
        record.update(status='error',reason=recent['error'])
    if recent.get('files'):
        signature=hashlib.sha256(b''.join(hashlib.sha256((root/p).read_bytes()).digest() for p in recent['files'])).hexdigest()
        seen_path=root/'candidate_hashes.json'
        with (root/'.candidate-hashes.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            seen=json.loads(seen_path.read_text()) if seen_path.exists() else []
            if signature in seen:record.update(status='duplicate',reason='Identical generated frames already evaluated')
            else:seen.append(signature)
            write_json(seen_path,seen)
        record['sha256']=signature
    write_json(request['result'],record)
    print(json.dumps({'status':record['status'],'asset':asset['id'],'seed':recent.get('seed')}),flush=True)


if __name__=='__main__':main()
