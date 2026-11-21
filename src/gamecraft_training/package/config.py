import json
import math
from pathlib import Path, PurePosixPath
from gamecraft_assets.plans import validate_generic


def relative(root, name):
    p=PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(x in ('.','..') or x.startswith('.') for x in p.parts):
        raise ValueError(f'Expected a relative package path: {name}')
    result=Path(root)/name
    if Path(root).resolve() not in result.resolve().parents or result.is_symlink():
        raise ValueError('Path escapes root')
    return result


def load(root, adapter, scenarios, *, profile='rsi'):
    from .profiles import resolve as resolve_profile
    execution=resolve_profile(profile)
    root=Path(root)
    config=json.loads((root/'training.json').read_text())
    assets=json.loads((root/'asset_plan.json').read_text())
    composition=json.loads((root/'composition_spec.json').read_text())
    validate_generic(assets)
    if config.get('schema_version')!=1 or assets['task_id']!=adapter.task_id:
        raise ValueError('Training schema or task mismatch')
    purposes=('initialization','train','validation','confirmation','test')
    groups={p:[c for c in scenarios if c['purpose']==p] for p in purposes}
    required=execution['scenario_groups']
    if not all(groups[p] for p in required):
        raise ValueError(profile+' profile needs nonempty disjoint '+('/'.join(required))+' scenarios')
    names=[c['id'] for group in groups.values() for c in group]
    if len(set(names))!=len(names):raise ValueError('Scenario split overlap')
    metrics=config.get('behavior',{}).get('metrics',[])
    if metrics and not callable(getattr(adapter,'behavior_metrics',None)):
        raise ValueError('Adapter must define behavior_metrics(result) when reporting metrics are configured')
    if len({m['id'] for m in metrics})!=len(metrics):raise ValueError('Reporting behavior metrics must be unique')
    for m in metrics:
        gain=m.get('min_gain',0)
        if m['direction'] not in ('max','min') or type(gain) not in (int,float) or not math.isfinite(gain) or gain<0:
            raise ValueError('Invalid behavior objective')
    selection=config.get('selection',{'mode':'adaptive_pairwise_v1','max_reviews':2})
    if (selection.get('mode') not in ('adaptive_pairwise_v1','blind_pairwise_v1','legacy') or
            selection.get('max_reviews',selection.get('reviews',2)) not in (1,2)):
        raise ValueError('Invalid candidate selection protocol')
    verification=config.get('repair_verification',{'mode':'blind_issue_v1'})
    if verification.get('mode') not in ('blind_issue_v1','legacy'):
        raise ValueError('Invalid repair verification protocol')
    criteria=config['quality']['criteria']
    ids=[c['id'] for c in criteria]
    if not ids or len(set(ids))!=len(ids) or any(not c['description'] for c in criteria):raise ValueError('Quality criteria required')
    if any(config['quality']['targets'][side] not in ids for side in ('asset','composition')):raise ValueError('Invalid quality targets')
    for field,default in [('min_gain',.05),('regression_tolerance',.02)]:
        value=config['quality'].get(field,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:raise ValueError('Invalid quality threshold')
    repair_criteria=config['quality'].get('repair_criteria',[])
    repair_min_gain=config['quality'].get('repair_min_gain',.05)
    if (not isinstance(repair_criteria,list)
            or any(not isinstance(x,str) for x in repair_criteria)
            or len(set(repair_criteria))!=len(repair_criteria)
            or any(x not in ids for x in repair_criteria)
            or type(repair_min_gain) not in (int,float) or not math.isfinite(repair_min_gain)
            or not 0<=repair_min_gain<=1):
        raise ValueError('Invalid repair criteria or threshold')
    for key,value in config['assets'].get('quality',{}).items():
        if key not in ('min_usable','min_identity','min_matte','min_coverage','min_in_frame','max_bbox_fill') or type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
            raise ValueError(f'Invalid asset quality threshold {key}: expected a number in [0, 1], got {value!r}')
    if config['assets'].get('quality',{}).get('min_coverage',.02)>.25:
        raise ValueError('min_coverage above .25 conflicts with isolated sprites and generous framing')
    for asset,file in config['assets'].get('references',{}).items():
        if asset not in config['assets']['bindings'] or not relative(root,file).is_file():
            raise ValueError('Invalid asset identity reference')
    for a in assets['assets']:
        files=config['assets']['bindings'][a['id']]
        if len(files)!=a['frames']:raise ValueError('Asset binding frame count mismatch')
        for file in files:
            if not relative(root/'game',file).is_file():raise ValueError(f'Missing bound asset {file}')
    bound=[f for fs in config['assets']['bindings'].values() for f in fs]
    if len(bound)!=len(set(bound)):raise ValueError('Two assets cannot own the same file')
    for name,p in composition['parameters'].items():
        if not relative(root/'game',p['file']).is_file():raise ValueError('Missing composition file')
        if p['file'] in bound or p['file'] in composition.get('fixed',[]):raise ValueError('Mutable path is protected')
        if (p['type'] not in ('int','float') or
            any(type(p[k]) not in (int,float) or not math.isfinite(p[k]) for k in ('min','max')) or
            p['min']>p['max'] or not isinstance(p['path'],list) or not p['path']):
            raise ValueError('Invalid composition parameter')
        value=json.loads(relative(root/'game',p['file']).read_text())
        for key in p['path']:value=value[key]
        if type(value) not in (int,float) or not math.isfinite(value) or not p['min']<=value<=p['max']:
            raise ValueError('Initial composition parameter outside declared range')
    config['_profile']=execution
    return config,assets,composition,groups


def aggregate(results, adapter, config):
    if not results or any(not r['replay_verified'] for r in results):raise ValueError('Verified episodes required')
    if not config.get('behavior',{}).get('metrics',[]):return {}
    rows=[adapter.behavior_metrics(r) for r in results]
    out={}
    for m in config['behavior']['metrics']:
        values=[row[m['id']] for row in rows]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise ValueError('Invalid behavior metric')
        out[m['id']]=sum(values)/len(values)
    return out


def better(new, old, config):
    # Lexicographic objective; never trade the primary metric for a secondary one.
    for m in config['behavior']['metrics']:
        delta=(new[m['id']]-old[m['id']])*(1 if m['direction']=='max' else -1)
        if abs(delta)<1e-9:continue
        return delta>0 and delta+1e-9>=m.get('min_gain',0)
    return False
