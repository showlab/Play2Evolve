import json
import hashlib
import math
import shutil
import subprocess
from pathlib import Path
from gamecraft_behavior.core import write_json, digest
from gamecraft_composition.core import snapshot
from .config import relative

ASSET_REPAIR_CANDIDATES = 4


def apply_parameters(project, specification, changes):
    if not isinstance(changes,list) or not 1<=len(changes)<=8:raise ValueError('Expected 1–8 parameter changes')
    pending={};seen=set()
    for change in changes:
        name=change['parameter'];value=change['value']
        if name not in specification['parameters'] or name in seen:raise ValueError('Unknown or duplicate parameter')
        seen.add(name);p=specification['parameters'][name]
        if type(value) not in (int,float) or not math.isfinite(value) or not p['min']<=value<=p['max']:
            raise ValueError('Parameter value outside declared range')
        if p['type']=='int' and int(value)!=value:raise ValueError('Integer parameter required')
        value=int(value) if p['type']=='int' else value
        file=relative(project,p['file'])
        data=pending.setdefault(file,json.loads(file.read_text()))
        node=data
        for key in p['path'][:-1]:node=node[key]
        last=p['path'][-1]
        if isinstance(node,dict) and last not in node:raise ValueError('Parameter does not exist')
        node[last]=value
    for file,data in pending.items():write_json(file,data)


def graft(project, workspace, asset, bindings, output):
    item=json.loads((Path(workspace)/'asset_manifest.json').read_text())['assets'][asset]
    files=item['files'];targets=bindings[asset]
    if len(files)!=len(targets):raise ValueError('Generation changed the animation interface frame count')
    snapshot(project,output)
    before=digest(output)
    for src,dst in zip(files,targets):shutil.copyfile(relative(workspace,src),relative(output,dst))
    after=digest(output)
    if any(before.get(k)!=after.get(k) for k in set(before)|set(after) if k not in targets):
        raise ValueError('Asset update changed non-bound files')
    return output


def graft_many(project, workspace, assets, bindings, output):
    """Atomically install all assets owned by one diagnosed issue."""
    if not assets or len(set(assets)) != len(assets):
        raise ValueError('Expected unique diagnosed assets')
    manifest=json.loads((Path(workspace)/'asset_manifest.json').read_text())['assets']
    plan=json.loads((Path(workspace)/'asset_plan.json').read_text())
    planned={item['id']:item for item in plan['assets']}
    snapshot(project,output)
    before=digest(output);targets=[]
    for asset in assets:
        if asset not in bindings or asset not in manifest:
            raise ValueError('Missing generated asset or binding')
        files=manifest[asset]['files'];owned=bindings[asset]
        if len(files)!=len(owned):
            raise ValueError('Generation changed the animation interface frame count')
        from gamecraft_assets.contract import validate_bound
        validate_bound(workspace,planned[asset],files)
        targets.extend(owned)
        for src,dst in zip(files,owned):
            shutil.copyfile(relative(workspace,src),relative(output,dst))
        validate_bound(output,planned[asset],owned)
    after=digest(output)
    if any(before.get(k)!=after.get(k) for k in set(before)|set(after) if k not in targets):
        raise ValueError('Asset update changed non-bound files')
    return output


class Generator:
    def __init__(self,root,plan,config,python,video_skill=False,candidates=ASSET_REPAIR_CANDIDATES):
        if type(candidates) is not int or not 1<=candidates<=4:
            raise ValueError('Asset candidates must be 1–4')
        self.root=Path(root);self.python=python;self.config=config;self.video_skill=video_skill
        self.candidates=candidates
        self.root.mkdir(parents=True,exist_ok=False)
        write_json(self.root/'asset_plan.json',plan);write_json(self.root/'base_plan.json',plan)
        # A scoped bootstrap material may use another accepted material as its
        # H3 keyframe. Keep an immutable local copy so later repairs can resolve
        # the same reference without reaching into the frozen package by path.
        planned={item['id']:item for item in plan['assets']}
        baseline_references={}
        for source_id in {item['reference_asset_id'] for item in plan['assets'] if item.get('reference_asset_id')}:
            source_plan=planned.get(source_id)
            bound=config['assets']['bindings'].get(source_id)
            if source_plan is None or not bound:
                raise ValueError(f'Missing bound reference material: {source_id}')
            source=(relative(config['_reference_project'],bound[0]) if config.get('_reference_project')
                    else relative(config['_package_root'], 'game/'+bound[0]))
            target=self.root/'baseline_references'/source_id/'frame0000.png'
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            baseline_references[source_id]={
                'files':[str(target.relative_to(self.root))],
                'plan_sha256':hashlib.sha256(json.dumps(source_plan,sort_keys=True).encode()).hexdigest()}
        write_json(self.root/'asset_manifest.json',{'task_id':plan['task_id'],'assets':baseline_references})
        if video_skill:
            from gamecraft_video_skill import initialize, copy_skill
            source=Path(config['_package_root'])/'video_skill'
            copy_skill(source,self.root) if source.is_dir() else initialize(self.root)
        for asset in plan['assets']:
            ref=config['assets'].get('references',{}).get(asset['id'])
            if ref:
                source=relative(config['_package_root'],ref)
                target=self.root/'generated'/asset['id']/'reference.png'
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,target)
                write_json(target.with_suffix('.json'),{'prompt':asset['reference_prompt'].format(desc=asset['description'])})

    def __call__(self,asset,problem,out):
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        planned_prompt=problem.get('prompt') if isinstance(problem,dict) else None
        problem_text=problem.get('problem','') if isinstance(problem,dict) else problem
        request={'workspace':str(self.root),'asset':asset,'problem':problem_text,'result':str(out/'generation.json'),
                 'quality':self.config['assets'].get('quality',{}),'video_skill':self.video_skill,
                 'phase':self.config.get('_profile',{}).get('name','rsi'),
                 'candidates':self.candidates}
        if planned_prompt is not None:request['planned_prompt']=planned_prompt
        instruction=Path(self.config['_package_root'])/'instruction.md'
        if instruction.is_file():request['instruction_path']=str(instruction)
        write_json(out/'request.json',request)
        with (out/'generation.log').open('w') as log:
            subprocess.run([self.python,'-m','gamecraft_training.package.asset_worker',str(out/'request.json')],
                           cwd=Path(__file__).resolve().parents[2],stdout=log,stderr=log,check=True,timeout=2400)
        return json.loads((out/'generation.json').read_text())
