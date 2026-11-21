from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from gamecraft_assets.plans import write_json

DEFAULT = Path(__file__).with_name('default_skill.json')
KINDS = {'animation', 'sprite', 'background'}


def validate_skill(skill):
    if skill.get('schema_version') != 1 or not isinstance(skill.get('rules'), list):
        raise ValueError('Expected video skill schema_version=1 and rules')
    seen = set()
    for rule in skill['rules']:
        if set(rule) - {'id', 'applies_when', 'instruction', 'source', 'evidence'}:
            raise ValueError('Unknown video skill rule field')
        if not isinstance(rule.get('id'), str) or not rule['id'] or rule['id'] in seen:
            raise ValueError('Video skill rule IDs must be unique')
        seen.add(rule['id'])
        applies = rule.get('applies_when')
        if not isinstance(applies, dict) or set(applies) - {'kind', 'loop', 'asset_id', 'generation_scope', 'motion'}:
            raise ValueError('Video skill supports only material-role applicability')
        if 'kind' in applies and applies['kind'] not in KINDS:
            raise ValueError('Invalid video skill asset kind')
        if 'loop' in applies and type(applies['loop']) is not bool:
            raise ValueError('Video skill loop condition must be boolean')
        if 'asset_id' in applies and (not isinstance(applies['asset_id'],str) or not applies['asset_id']):
            raise ValueError('Video skill asset_id condition must be a non-empty string')
        if 'generation_scope' in applies and applies['generation_scope'] not in {'single_subject','coupled_action','scene'}:
            raise ValueError('Invalid video skill generation scope')
        if 'motion' in applies and applies['motion'] not in {'static','loop','event'}:
            raise ValueError('Invalid video skill motion')
        if not isinstance(rule.get('instruction'), str) or not rule['instruction'].strip() or len(rule['instruction']) > 1200:
            raise ValueError('Video skill instruction must contain 1–1200 characters')
    return skill


def _read(path):
    return validate_skill(json.loads(Path(path).read_text()))


def initialize(workspace, source=None):
    """Create an isolated skill snapshot. Existing snapshots are never reset."""
    root = Path(workspace) / 'video_skill'
    root.mkdir(parents=True, exist_ok=True)
    global_path = root / 'global.json'
    local_path = root / 'local.json'
    if not global_path.exists():
        skill = _read(source or DEFAULT)
        write_json(global_path, skill)
    else:
        _read(global_path)
    if not local_path.exists():
        write_json(local_path, {'schema_version': 1, 'rules': []})
    else:
        _read(local_path)
    decisions_path = root / 'decisions.json'
    if not decisions_path.exists():
        write_json(decisions_path, {'schema_version': 1, 'lessons': []})
    return root


def copy_skill(source, workspace):
    source = Path(source)
    target = Path(workspace) / 'video_skill'
    if target.exists():
        raise FileExistsError(target)
    if source.is_dir():
        shutil.copytree(source, target)
        _read(target/'global.json'); _read(target/'local.json')
        if not (target/'decisions.json').exists():
            write_json(target/'decisions.json', {'schema_version': 1, 'lessons': []})
    else:
        initialize(workspace, source)
    return target


def install_layers(workspace, *, global_source=None, local_source=None):
    """Install explicit read-only global and game-local starting layers."""
    root=initialize(workspace)
    for name,source in (('global.json',global_source),('local.json',local_source)):
        if source is None:
            continue
        source=Path(source)
        if source.is_dir():source=source/name
        skill=_read(source)
        write_json(root/name,skill)
    return root


def _matches(rule, asset):
    from gamecraft_assets.contract import material_intent
    scope, motion = material_intent(asset)
    def value(key):
        # Skill rules use the explicit name ``asset_id`` while asset plans use
        # the generic schema's ``id``.  Treat them as the same identity.
        if key == 'asset_id':
            return asset.get('id')
        if key == 'generation_scope':
            return scope
        if key == 'motion':
            return motion
        if key == 'loop':
            return motion == 'loop'
        return asset.get(key, False if key == 'loop' else None)
    return all(value(key) == expected
               for key, expected in rule['applies_when'].items())


def _version(global_skill, local_skill, extra_rules=()):
    raw = json.dumps([global_skill, local_skill, list(extra_rules)], sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def resolve(workspace, asset, style, *, phase, extra_rules=()):
    from gamecraft_assets.contract import compile_generation_prompt, generation_contract
    root = initialize(workspace)
    global_skill, local_skill = _read(root/'global.json'), _read(root/'local.json')
    rules = global_skill['rules'] + local_skill['rules'] + list(extra_rules)
    for rule in extra_rules:
        validate_skill({'schema_version': 1, 'rules': [rule]})
    # Keep H3 requests short and predictable as the library grows.  An active
    # round's explicit proposal is always included; local exact-asset lessons
    # precede broader inherited constraints.
    matched = [r for r in rules if _matches(r, asset)]
    explicit_ids = {r['id'] for r in extra_rules}
    proposed = [r for r in matched if r['id'] in explicit_ids][-4:]
    inherited = [r for r in matched if r['id'] not in explicit_ids]
    inherited.sort(key=lambda r: (r['applies_when'].get('asset_id') != asset['id'],
                                  -len(r['applies_when']), r['id']))
    applied = inherited[:max(0, 4-len(proposed))] + proposed
    instructions = [r['instruction'].strip() for r in applied]
    contract = generation_contract(asset)
    prompt = compile_generation_prompt(style, asset, instructions)
    return {
        'schema_version': 1, 'phase': phase, 'asset_id': asset['id'],
        'method': contract['method'],
        'prompt': prompt, 'reference_policy': contract['reference_policy'],
        'duration_seconds': asset.get('source_duration_seconds', 4),
        'aspect_ratio': contract['aspect_ratio'],
        'output_contract': contract,
        'postprocess': {'frames': asset['frames'], 'chroma_key': contract['chroma_key'],
                        'shared_crop': contract['shared_crop']},
        'applied_rules': [r['id'] for r in applied],
        'skill_version': _version(global_skill, local_skill, extra_rules),
    }


VIDEO_SKILL_MODEL = 'gpt-6-sol'
_NON_ASSET_CONTROL = re.compile(
    r'\b(?:code|script|engine|runtime|gameplay|game\s+state|input|damage|health|hp|score|'
    r'collision|hitbox|trigger|register(?:ed|ing)?|opponent|enemy|target|hud|ui)\b',
    re.IGNORECASE)


def _validate_generation_instruction(instruction):
    """Reject rules that ask H3 to control the game or another composited object."""
    instruction = instruction.strip()
    if not instruction or len(instruction) > 1200:
        raise ValueError('Invalid video skill patch instruction')
    match = _NON_ASSET_CONTROL.search(instruction)
    if match:
        raise ValueError(f'Video Skill contains non-asset control language: {match.group(0)}')
    return instruction


def creator_guidance(workspace, asset):
    """Return validated cross-game optimizer guidance relevant to one asset."""
    root=initialize(workspace)
    rules=_read(root/'global.json')['rules']
    return [rule['evidence']['creator_guidance'] for rule in rules
            if _matches(rule,asset) and isinstance(rule.get('evidence'),dict)
            and isinstance(rule['evidence'].get('creator_guidance'),str)][:4]


def propose_patch(g, asset, failures, *, workspace=None):
    """Ask Codex for one H3-only rule; return None when H3 cannot fix the defect."""
    schema = {'type': 'object', 'additionalProperties': False,
              'required': ['actionable', 'instruction', 'reason'],
              'properties': {'actionable': {'type': 'boolean'},
                             'instruction': {'type': 'string'}, 'reason': {'type': 'string'}}}
    model = os.environ.get('GAMECRAFT_VIDEO_SKILL_MODEL', VIDEO_SKILL_MODEL)
    result = g.llm.call(model,
        'Decide whether the observed defect can be fixed solely by changing how H3 generates one standalone video asset. '
        'H3 controls only pixels inside that asset: its subject appearance and motion, camera, framing, background, temporal '
        'continuity, and loop closure. H3 cannot control game code, rules, state, damage, collision, scoring, user input, actor '
        'placement, synchronization between separately rendered assets, or when a game displays an effect. Return actionable=false '
        'for any defect that needs those controls. If actionable=true, write one concise, positive generation instruction that is '
        'for the current asset request. Describe only observable properties inside '
        'the standalone generated video. Do not mention unrequested actors, interaction outcomes, character or game names, colors, file '
        'paths, scores, seeds, code concepts, or the unwanted artifact as content to depict. Preserve the requested asset purpose.',
        [{'type': 'input_text', 'text': json.dumps({'asset_kind': asset['kind'],
                                                    'generation_scope': asset.get('generation_scope'),
                                                    'motion': asset.get('motion'),
                                                    'loop': asset.get('loop', False),
                                                    'requirements': asset['requirements'], 'failures': failures[-3:],
                                                    'validated_global_creator_guidance':(
                                                        creator_guidance(workspace,asset) if workspace is not None else [])})}],
        'video_skill_patch', schema, backend='codex', max_tokens=500)
    if not result['actionable']:
        return None
    instruction = _validate_generation_instruction(result['instruction'])
    ident = hashlib.sha256(json.dumps({'kind': asset['kind'], 'loop': asset.get('loop', False),
                                       'instruction': instruction}, sort_keys=True).encode()).hexdigest()[:12]
    return {'id': 'learned_' + ident,
            'applies_when': {'kind': asset['kind'], 'asset_id':asset['id'],
                             **({'loop': asset['loop']} if asset['kind'] == 'animation' else {})},
            'instruction': instruction, 'source': 'codex',
            'evidence': {'asset': asset['id'], 'failures': failures[-3:], 'reason': result['reason'],
                         'proposer': {'backend': 'codex', 'model': model}}}


def record_patch(workspace, patch, *, status, evidence=None):
    root = initialize(workspace)
    path = root/'evidence.jsonl'
    row = {'status': status, 'patch': patch, 'evidence': evidence or {}}
    with path.open('a') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def commit_patch(workspace, patch, *, evidence=None):
    validate_skill({'schema_version': 1, 'rules': [patch]})
    root = initialize(workspace)
    global_skill = _read(root/'global.json')
    local = _read(root/'local.json')
    existing = {r['id'] for r in global_skill['rules'] + local['rules']}
    if patch['id'] not in existing:
        local['rules'].append(patch)
        write_json(root/'local.json', local)
    record_patch(workspace, patch, status='accepted', evidence=evidence)


def initialize_global_store(store, source=None):
    """Create a persistent cross-game store without adding any built-in rule."""
    root=Path(store);root.mkdir(parents=True,exist_ok=True)
    path=root/'global.json'
    if not path.exists():write_json(path,_read(source or DEFAULT))
    else:_read(path)
    pending=root/'pending_global.json'
    if not pending.exists():
        write_json(pending,{'schema_version':1,'candidates':[]})
    else:
        _read_pending(pending)
    # One-time import for stores created by the former exact-ID implementation.
    legacy=root/'candidates.jsonl';ledger=root/'local_evidence.jsonl'
    if legacy.exists() and not ledger.exists():
        with ledger.open('w') as target:
            for old in _read_jsonl(legacy):
                payload={'task_id':old['task_id'],'patch':old['patch'],'evidence':old.get('evidence',{})}
                evidence_id='evidence_'+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:16]
                row={'schema_version':1,'evidence_id':evidence_id,
                     'created_at':old.get('created_at',time.time()),**payload,'migrated_from':'candidates.jsonl'}
                target.write(json.dumps(row,ensure_ascii=False)+'\n')
    return root


def submit_global_candidate(store, patch, *, task_id, evidence=None):
    """Record one accepted local repair as evidence; this never promotes a rule."""
    if not isinstance(task_id,str) or not task_id:
        raise ValueError('Global video-skill evidence needs a task_id')
    validate_skill({'schema_version':1,'rules':[patch]})
    root=initialize_global_store(store)
    payload={'task_id':task_id,'patch':patch,'evidence':evidence or {}}
    evidence_id='evidence_'+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:16]
    row={'schema_version':1,'evidence_id':evidence_id,'created_at':time.time(),**payload}
    if any(x.get('evidence_id')==evidence_id for x in _read_jsonl(root/'local_evidence.jsonl')):
        return row
    with (root/'local_evidence.jsonl').open('a') as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    return row


def _read_jsonl(path):
    path=Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _read_pending(path):
    value=json.loads(Path(path).read_text())
    if value.get('schema_version')!=1 or not isinstance(value.get('candidates'),list):
        raise ValueError('Expected pending global Video Skill schema_version=1 and candidates')
    for item in value['candidates']:
        required={'id','applies_when','instruction','creator_guidance','source_evidence_ids',
                  'source_games','reason','status'}
        if not required.issubset(item) or item['status'] not in ('pending','promoted','rejected'):
            raise ValueError('Invalid pending global Video Skill candidate')
        validate_skill({'schema_version':1,'rules':[{
            'id':item['id'],'applies_when':item['applies_when'],
            'instruction':item['instruction'],'source':'codex_merge'}]})
    return value


def _global_merge_call(model, instructions, data, schema):
    import llm
    return llm.call(model,instructions,[{'type':'input_text','text':json.dumps(data,ensure_ascii=False)}],
                    'global_video_skill_merge',schema,backend='codex',max_tokens=1800)


def _canonical_global_scope(patch):
    """Drop the game-local asset identity before comparing cross-game evidence."""
    return {key:value for key,value in patch['applies_when'].items() if key!='asset_id'}


def merge_global_candidates(store, *, min_games=2, model=None, caller=None):
    """Semantically merge accepted local evidence into pending cross-game rules.

    The output remains pending. Source-game acceptance is evidence for proposing a
    transferable rule, not evidence that the merged wording transfers.
    """
    if type(min_games) is not int or min_games<2:
        raise ValueError('Global merge requires at least two games')
    root=initialize_global_store(store)
    rows=_read_jsonl(root/'local_evidence.jsonl')
    scopes={}
    for row in rows:
        patch=row['patch'];validate_skill({'schema_version':1,'rules':[patch]})
        scope=json.dumps(_canonical_global_scope(patch),sort_keys=True)
        scopes.setdefault(scope,[]).append(row)
    eligible=[]
    for scope,items in scopes.items():
        if len({x['task_id'] for x in items})>=min_games:
            eligible.extend(items)
    if not eligible:
        return []
    pending=_read_pending(root/'pending_global.json')
    already_used={evidence_id for item in pending['candidates']
                  for evidence_id in item['source_evidence_ids']}
    fresh=[row for row in eligible if row['evidence_id'] not in already_used]
    # Old evidence is included as context so Codex can update a semantic cluster
    # when a later game contributes a new paraphrase or counterexample.
    if not fresh:
        return []
    compact=[{'evidence_id':x['evidence_id'],'game':x['task_id'],
              'applies_when':_canonical_global_scope(x['patch']),'local_instruction':x['patch']['instruction'],
              'observed_failure':x.get('evidence',{}).get('problem') or x['patch'].get('evidence',{}).get('failures',[]),
              'before':x.get('evidence',{}).get('before'),'after':x.get('evidence',{}).get('after')}
             for x in eligible[-80:]]
    item_schema={'type':'object','additionalProperties':False,
        'required':['source_evidence_ids','instruction','creator_guidance','reason'],
        'properties':{
            'source_evidence_ids':{'type':'array','minItems':2,'items':{'type':'string'}},
            'instruction':{'type':'string'},'creator_guidance':{'type':'string'},'reason':{'type':'string'}}}
    schema={'type':'object','additionalProperties':False,'required':['candidates'],
            'properties':{'candidates':{'type':'array','maxItems':8,'items':item_schema}}}
    model=model or os.environ.get('GAMECRAFT_VIDEO_SKILL_MODEL',VIDEO_SKILL_MODEL)
    invoke=caller or _global_merge_call
    answer=invoke(model,
        'Merge empirically accepted local H3 video-generation lessons into conservative cross-game candidates. '
        'This is semantic synthesis: combine paraphrases that express the same transferable cause, and keep distinct causes separate. '
        'Every candidate must cite evidence from at least two different games with exactly the same applies_when scope, and must cite '
        'at least one ID listed in fresh_evidence_ids. '
        'instruction is a concise positive constraint that can be appended directly to an H3 prompt. creator_guidance explains how a future '
        'optimizer should recognize when to use that constraint. Both fields may discuss only pixels within one standalone generated asset: '
        'subject appearance or motion, camera, framing, background, temporal continuity, or loop closure. Never mention code, engines, runtime '
        'state, gameplay, damage, collision, scoring, input, actor placement, synchronization, game names, character names, or another asset. '
        'Do not output a candidate when the evidence is contradictory, game-specific, or too weak.',
        {'accepted_local_evidence':compact,'fresh_evidence_ids':[x['evidence_id'] for x in fresh]},schema)
    by_id={x['evidence_id']:x for x in eligible};created=[]
    known_ids={x['id'] for x in pending['candidates']}
    for merged in answer['candidates']:
        ids=list(dict.fromkeys(merged['source_evidence_ids']))
        if any(ident not in by_id for ident in ids):
            raise ValueError('Global merge cited unknown local evidence')
        if not set(ids).intersection(x['evidence_id'] for x in fresh):
            raise ValueError('Global merge ignored all fresh local evidence')
        selected=[by_id[ident] for ident in ids]
        if len({x['task_id'] for x in selected})<min_games:
            raise ValueError('Global merge candidate lacks independent games')
        scopes={json.dumps(_canonical_global_scope(x['patch']),sort_keys=True) for x in selected}
        if len(scopes)!=1:
            raise ValueError('Global merge combined incompatible scopes')
        instruction=_validate_generation_instruction(merged['instruction'])
        guidance=_validate_generation_instruction(merged['creator_guidance'])
        applies=_canonical_global_scope(selected[0]['patch'])
        ident='global_'+hashlib.sha256(json.dumps({'scope':applies,'instruction':instruction},sort_keys=True).encode()).hexdigest()[:12]
        if ident in known_ids:
            continue
        candidate={'id':ident,'applies_when':applies,'instruction':instruction,
                   'creator_guidance':guidance,'source_evidence_ids':ids,
                   'source_games':sorted({x['task_id'] for x in selected}),
                   'reason':merged['reason'],'status':'pending','created_at':time.time(),
                   'proposer':{'backend':'codex','model':model}}
        pending['candidates'].append(candidate);known_ids.add(ident);created.append(ident)
    write_json(root/'pending_global.json',pending)
    with (root/'merge_history.jsonl').open('a') as stream:
        stream.write(json.dumps({'created_at':time.time(),'model':model,'input_evidence_ids':[x['evidence_id'] for x in eligible],
                                 'created_candidate_ids':created},ensure_ascii=False)+'\n')
    return created


def record_global_validation(store, candidate_id, *, task_id, same_seed, improved,
                             no_new_defects, evidence=None, mode='heldout_ab'):
    """Record an independent A/B result for one pending global candidate."""
    if any(type(value) is not bool for value in (same_seed,improved,no_new_defects)):
        raise ValueError('Global A/B flags must be booleans')
    if not isinstance(evidence or {},dict):raise ValueError('Global validation evidence must be an object')
    root=initialize_global_store(store);pending=_read_pending(root/'pending_global.json')
    candidate=next((x for x in pending['candidates'] if x['id']==candidate_id),None)
    if candidate is None:raise ValueError('Unknown pending global candidate')
    if task_id in candidate['source_games']:
        raise ValueError('Global validation game must be independent of source games')
    if mode!='heldout_ab':raise ValueError('Global validation must use heldout_ab mode')
    row={'schema_version':1,'created_at':time.time(),'candidate_id':candidate_id,'task_id':task_id,
         'mode':mode,'same_seed':bool(same_seed),'improved':bool(improved),
         'no_new_defects':bool(no_new_defects),'evidence':evidence or {}}
    with (root/'global_validations.jsonl').open('a') as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    return row


def promote_global_candidates(store, *, min_validations=1):
    """Promote only pending rules with independent, same-seed held-out A/B gains."""
    if type(min_validations) is not int or min_validations<1:
        raise ValueError('Global promotion requires at least one validation')
    root=initialize_global_store(store);pending=_read_pending(root/'pending_global.json')
    validations=_read_jsonl(root/'global_validations.jsonl')
    skill=_read(root/'global.json');known={r['id'] for r in skill['rules']};promoted=[]
    for item in pending['candidates']:
        if item['status']!='pending':continue
        good=[x for x in validations if x['candidate_id']==item['id'] and x['mode']=='heldout_ab'
              and x['same_seed'] and x['improved'] and x['no_new_defects']
              and x['task_id'] not in item['source_games']]
        if len({x['task_id'] for x in good})<min_validations:continue
        if item['id'] not in known:
            skill['rules'].append({'id':item['id'],'applies_when':item['applies_when'],
                'instruction':item['instruction'],'source':'codex_merge',
                'evidence':{'source_games':item['source_games'],
                            'validation_games':sorted({x['task_id'] for x in good}),
                            'creator_guidance':item['creator_guidance']}})
            known.add(item['id'])
        item['status']='promoted';item['promoted_at']=time.time();promoted.append(item['id'])
    if promoted:
        write_json(root/'global.json',skill);write_json(root/'pending_global.json',pending)
    return promoted
