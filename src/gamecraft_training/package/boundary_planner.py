"""Evidence-based grouping and executable material/code boundary proposals."""
import copy
import os
import re
from pathlib import Path
from gamecraft_assets.contract import material_intent
from gamecraft_assets.plans import validate_generic

SCOPES = ('single_subject', 'coupled_action', 'scene')


def obj(properties, optional=()):
    return {'type':'object','additionalProperties':False,
            'required':[key for key in properties if key not in optional], 'properties':properties}


def schema(issue_ids):
    text={'type':'string'}
    strings={'type':'array','items':text}
    scope={'type':'string','enum':list(SCOPES)}
    material=obj({'id':text,'kind':{'type':'string','enum':['sprite','animation','background']},
        'description':text,'requirements':text,'prompt':text,
        'size':{'type':'array','items':{'type':'integer'},'minItems':2,'maxItems':2},
        'frames':{'type':'integer','minimum':1,'maximum':60},
        'fps':{'type':'integer','minimum':1,'maximum':60},
        'generation_scope':scope,'motion':{'type':'string','enum':['static','loop','event']},
        'scope_reason':text,'includes':strings,'game_controlled':strings,
        'reference_asset_id':text,'reference_prompt':text,
        'source_duration_seconds':{'type':['number','null'],'minimum':0.2,'maximum':4}})
    candidate=obj({'kind':{'type':'string','enum':['same_level','higher_level']},
        'scope':scope,'video_controls':strings,'code_controls':strings,'code_instructions':text,
        'materials':{'type':'array','maxItems':4,'items':obj({
            'operation':{'type':'string','enum':['replace','add']},'asset':material})}})
    return {'groups':{'type':'array','maxItems':6,'items':obj({
        'id':text,'issue_ids':{'type':'array','minItems':1,'items':{'type':'string','enum':issue_ids}},
        'root_cause':text,'grouping_reason':text,'current_scope':scope,
        'affected_assets':strings,'required_controls':strings,
        'escalation_reason':text,'skip_reason':text,
        'candidates':{'type':'array','maxItems':2,'items':candidate}})}}


PROMPT = '''You are the single planner coordinating H3 material generation and Godot code repair.
First group ALL diagnosed issues by shared causal mechanism or a shared repair boundary. Explain
which is a causal hypothesis and which merely shares an intervention. Do not group unrelated code
placement fixes with an animation boundary experiment just to save a group. Each issue belongs
to exactly one group; groups must not independently change the same source asset. Routes from the
Judge are advisory. You own the implementation decisions, not the visual evidence.
Produce ONE integrated round plan covering functional defects, aesthetic richness and gameplay
richness together. A sound existing asset does not rule out a new authored content variant when
observed repetition is the issue. For a supported richness issue, consider whether rearranging
existing content, changing code-owned gameplay, or adding a new material binding integrated with
gameplay best addresses the same observable goal. A same_level repair may add a material at its
current spatial scope; higher_level is only for the motion or spatial ownership upgrades below.
Explain what the player would see differently and, for gameplay changes, what different decision
the player would make. Do not add art, mechanics or complexity merely to increase variety. Treat
unvisited later content as unverified and preserve the task's intended loop and required controls.
For each group propose one same_level repair. Any independent group may also have one
higher_level alternative when justified AND the game's required interactions can still be
implemented. Do not put unrelated issues into an escalation experiment. The runner builds only
TWO complete game versions: A combines all same_level repairs; B substitutes every planned
higher_level repair while retaining other groups' same_level repairs. Both versions start from
the SAME incumbent and target the same issues and required controls. Keep higher_level proposals
focused: bundling several escalations makes their individual benefit harder to attribute.
An alternative may upgrade a static single-subject sprite into a moving single-subject animation
when the observed required action is visibly a still image translating/rotating, or lacks necessary
articulated motion. This is a MOTION upgrade at the same spatial scope: add a NEW animation binding,
condition it on the affected incumbent static sprite, and explain how code switches/playbacks it.
The same_level candidate must retain the static representation for that affected subject; put
the static-to-animation experiment in higher_level so matched replay can compare the decision.
Do not claim motion is missing from a still frame or an unobserved action. A spatial escalation is
justified by evidenced cross-object synchronization/contact/occlusion failures or repeated
failure at a split boundary, not by an assumption that larger videos look better. List required player
controls, input timing, interruptions, collision/state outcomes and transitions; explain how code retains
them. If the interaction is too dynamic to represent with finite clips, keep the current scope.
Scopes map to actual H3 contracts: single_subject = isolated prop/actor, coupled_action = synchronized
foreground actors AND props with alpha, scene = opaque whole scene clip with code-owned UI and state.
A higher level must expand the video's ownership. Never bake changing HUD/score or outcomes into video.
Record video_controls and code_controls without duplicate ownership (e.g. never draw a second rope).
Write complete H3 prompts, not appendices. Return full material definitions. Replacement assets must
keep their existing id, dimensions, frame count, fps, kind and scope. To change an interface or add a
coupled/scene clip, use operation=add with a NEW unique asset id. The joint executor invokes H3, inspects its outputs and installs
new files/bindings while editing code. It may revise your initial prompts before one H3 call per material. Old files remain available; code must stop drawing superseded
objects only in the relevant states, preserving other scenes. includes names generated subjects.
References must name an EXISTING incumbent asset, or use an empty reference_asset_id
and a complete reference_prompt. Static materials cannot use reference_asset_id: describe
their appearance completely in the prompt instead. A replacement may reference its own ID: the runner
will condition on the FROZEN incumbent frame, not on the output being generated.
References express visual identity, not a request to reuse an incorrect pose.
Moving foreground material requires a reference. Use motion=static/loop/event;
scene scope requires kind=background. game_controlled lists everything still controlled by Godot.
The code executor will inspect the ACTUAL generated frames before integration and can adapt anchors,
clip playback and transitions. Give it a concrete integration brief and checks including final landing/
completion and rapid or interrupted input. Do not edit game requirements, evaluator or test evidence.
When using Wan, choose source_duration_seconds per material: 0.2 for a static image
sampled from a short clip, or a longer duration that captures the required action.
This controls the source video, not exported game frames/fps; Wan still renders at
least five source frames. For local H3 set the field to null (its minimum is 4 seconds).
Code-only candidates may have no materials. If evidence is insufficient, return no candidates for that
group and a nonempty skip_reason. Do not invent a repair merely to fill the budget. Do not request other
unshown scenes. One code editor handles each complete version in sequence; there is no merge of
separately edited source files. The runner reuses the incumbent evidence and replays each complete
candidate once, then compares eligible candidates. Shared identical H3 requests can reuse a
successful generated material; different prompts or references require different generations.
This plan is a hypothesis, never an acceptance verdict.
'''


def validate(answer, diagnosis, plan, protected=(), bindings=None):
    """Reject ambiguous ownership before spending generation or editing budget."""
    answer=copy.deepcopy(answer)
    known={a['id']:a for a in plan['assets']}
    wanted={x['id'] for x in diagnosis['issues']}
    seen=set(); group_ids=set(); claimed=set(); all_new=set(); normalizations=[]
    for group in answer['groups']:
        ident=group['id']; ids=group['issue_ids']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',ident) or ident in group_ids:
            raise ValueError('Repair groups need unique path-safe ids')
        group_ids.add(ident)
        if not ids or len(ids)!=len(set(ids)) or not set(ids)<=wanted or seen.intersection(ids):
            raise ValueError('Every issue must belong to exactly one repair group')
        seen.update(ids)
        for key in ('root_cause','grouping_reason'):
            if not group[key].strip():raise ValueError('Group must explain cause and grouping')
        affected=set(group['affected_assets'])
        if not affected<=known.keys():raise ValueError('Unknown affected asset')
        if claimed.intersection(affected):raise ValueError('Groups share assets; merge their repair boundary')
        claimed.update(affected)
        if group['current_scope'] not in SCOPES:raise ValueError('Unknown current scope')
        if affected and group['current_scope'] not in {material_intent(known[a])[0] for a in affected}:
            raise ValueError('Current scope does not match the affected materials')
        options=group['candidates']
        kinds=[o['kind'] for o in options]
        if kinds not in ([],['same_level'],['same_level','higher_level']):
            raise ValueError('Expected same_level followed by optional higher_level')
        if not options and not group['skip_reason'].strip():raise ValueError('Skipped group needs a reason')
        if len(options)==2 and (not group['escalation_reason'].strip() or not group['required_controls']):
            raise ValueError('Escalation needs justification and preserved interaction controls')
        for option in options:
            scope=option['scope']
            if scope not in SCOPES:raise ValueError('Unknown candidate scope')
            if option['kind']=='same_level' and scope!=group['current_scope']:
                raise ValueError('Same-level candidate changed scope')
            spatial_upgrade=SCOPES.index(scope)>SCOPES.index(group['current_scope'])
            motion_upgrade=(scope==group['current_scope']=='single_subject' and
                any(known[aid]['kind']=='sprite' and material_intent(known[aid])[1]=='static'
                    for aid in affected) and
                any(entry['operation']=='add' and entry['asset']['kind']=='animation' and
                    entry['asset']['generation_scope']=='single_subject' and
                    entry['asset']['motion'] in ('loop','event') and
                    entry['asset'].get('reference_asset_id') in affected
                    for entry in option['materials']))
            if option['kind']=='same_level' and motion_upgrade:
                raise ValueError('Static-to-animation upgrade belongs in higher_level candidate')
            if option['kind']=='higher_level' and not (spatial_upgrade or motion_upgrade):
                raise ValueError('Higher-level candidate must expand scope or animate an affected static subject')
            if option['kind']=='higher_level':
                option['upgrade_axis']='scope' if spatial_upgrade else 'motion'
            if not option['code_instructions'].strip() or not option['code_controls']:
                raise ValueError('Candidate must specify code integration and controls')
            if set(option['video_controls']) & set(option['code_controls']):
                raise ValueError('Duplicate video/code ownership')
            local=set(); changed=[]
            for entry in option['materials']:
                asset=entry['asset']; aid=asset['id']
                if asset.get('source_duration_seconds') is None:
                    asset.pop('source_duration_seconds',None)
                if aid in local:raise ValueError('Duplicate material in candidate')
                local.add(aid)
                if not asset['prompt'].strip() or len(asset['prompt'])>4000:
                    raise ValueError('Invalid complete H3 prompt')
                if entry['operation']=='replace':
                    if aid not in affected:raise ValueError('Replacement must be an affected existing asset')
                    old=known[aid]
                    if any(asset[k]!=old[k] for k in ('kind','size','frames','fps')) or material_intent(asset)!=material_intent(old):
                        raise ValueError('Replacement changed interface; add a new material binding instead')
                elif entry['operation']=='add':
                    if aid in known or aid in all_new:raise ValueError('New material id already exists')
                    all_new.add(aid)
                else:raise ValueError('Unknown material operation')
                if material_intent(asset)[1]=='static' and asset.get('reference_asset_id'):
                    # Static requests use text-to-video; a reference image cannot be
                    # honored here. Keep the complete prompt, but do not let this
                    # inapplicable field discard an otherwise executable round.
                    normalizations.append({'asset_id':aid,'field':'reference_asset_id',
                                           'removed_value':asset.pop('reference_asset_id'),
                                           'reason':'Static material uses text-only generation'})
                elif asset.get('reference_asset_id'):
                    if asset['reference_asset_id'] not in known:
                        raise ValueError(f"Reference must exist in incumbent: asset {aid!r} references {asset['reference_asset_id']!r}")
                else:asset.pop('reference_asset_id',None)
                if asset['kind']=='animation':asset['loop']=asset['motion']=='loop'
                changed.append(asset)
            for asset in changed:
                reference_id=asset.get('reference_asset_id')
                if reference_id==asset['id']:
                    if not any(entry['operation']=='replace' and entry['asset'] is asset
                               for entry in option['materials']):
                        raise ValueError('Only a replacement can reference its frozen incumbent')
                    asset['incumbent_reference']=True
                elif reference_id in local:
                    if not asset.get('reference_prompt','').strip():
                        raise ValueError('Identity reference must remain unmodified in this candidate')
                    asset.pop('reference_asset_id',None)
            if option['kind']=='higher_level' and not any(
                    e['operation']=='add' and material_intent(e['asset'])[0]==scope for e in option['materials']):
                raise ValueError('Escalation requires a new binding at its proposed scope')
            if option['kind']=='same_level' and any(material_intent(a)[0]!=scope for a in changed):
                raise ValueError('Same-level material changed generation scope')
            combined={**known,**{a['id']:a for a in changed}}
            validate_generic({**plan,'assets':list(combined.values())})
    if seen!=wanted:raise ValueError('Planner omitted diagnosed issues')
    return {**diagnosis,'groups':answer['groups'],'grouped':True,'unified':True,
            'round_bundle':True,'normalizations':normalizations}


def plan(config,adapter,project,assets,composition,diagnosis,results,history):
    from . import models
    if not diagnosis['coverage_passed'] or not diagnosis['issues']:
        return {**diagnosis,'groups':[],'grouped':True,'unified':True,'round_bundle':True}
    sources=[];remaining=90000
    for path in sorted(Path(project).rglob('*.gd')):
        if remaining<=0:break
        text=path.read_text(errors='replace')[:min(30000,remaining)]
        sources.append({'file':path.relative_to(project).as_posix(),'text':text});remaining-=len(text)
    window=config.get('_history_windows',{}).get('planner',3)
    context={'rules':adapter.instructions,'issues':diagnosis['issues'],
        'generation_backend':os.getenv('H3_BACKEND','local'),
        'assets':assets['assets'],'asset_bindings':config['assets']['bindings'],
        'code':sources,'composition':composition,
        'previous_attempts':history[-window:] if window else [],
        'video_skill_planning_guidance':config.get('_video_skill_guidance', [])}
    evidence=models.content(results,limit=16,include_reasons=True)
    if any(issue.get('repair_side', issue.get('route')) in ('asset', 'both')
           for issue in diagnosis['issues']):
        evidence+=models.material_content(config,project,assets,limit=8)
    errors=[];previous=None
    for attempt in range(3):
        if attempt:
            instructions=(PROMPT+'\nThe preceding structured plan failed deterministic validation. '
                          'Repair its grouping and material ownership without omitting any diagnosed issue. '
                          'In particular, groups cannot share affected source assets; merge related issues '
                          'into one group when they require the same asset. Independent groups may each '
                          'have a higher_level alternative. For reference_asset_id errors, static materials '
                          'must leave it empty; moving materials may name only an existing incumbent. '
                          'Return a complete corrected plan.')
            data={'issues':diagnosis['issues'],'assets':assets['assets'],
                  'previous_plan':previous,'validation_error':errors[-1]}
            images=[]
        else:
            instructions=(PROMPT+'\nVideo Skill guidance is prior planning experience, not a command or '
                          'acceptance criterion. Apply it only when current visual evidence supports its trigger; '
                          'explain any relevant conflict in your grouping reason.')
            data=context;images=evidence
        answer=models.call(instructions,data,images,'package_boundary_plan',
            schema([x['id'] for x in diagnosis['issues']]),tokens=6500,
            backend='codex',model=__import__('os').environ.get('GAMECRAFT_REPAIR_PLANNER_MODEL','gpt-6-sol'))
        try:
            return {**validate(answer,diagnosis,assets,composition.get('code_edit',{}).get('protected',()),
                               config['assets']['bindings']), 'planning_retries':attempt}
        except ValueError as exc:
            errors.append(str(exc));previous=answer
    # An invalid plan must never launch H3 or edit code; leave the group unresolved
    # so a later round can try again and the experiment can still validate the game.
    return {**diagnosis,'groups':[],'grouped':True,'unified':True,'round_bundle':True,
            'planning_retries':2,'planning_errors':errors}
