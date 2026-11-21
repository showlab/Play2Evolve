"""Public-document plans for the HTML bootstrap and grouped initialization."""
import copy
import re


def obj(fields):
    return dict(type='object', additionalProperties=False, required=list(fields), properties=fields)


TEXT = {'type': 'string'}
STRINGS = {'type': 'array', 'items': TEXT}
ASSET = obj({
    'id': TEXT, 'kind': {'type': 'string', 'enum': ['background', 'sprite', 'animation']},
    'description': TEXT, 'prompt': TEXT, 'requirements': TEXT,
    'generation_scope': {'type': 'string', 'enum': ['single_subject', 'coupled_action', 'scene']},
    'motion': {'type': 'string', 'enum': ['static', 'loop', 'event']},
    'scope_reason': TEXT, 'includes': STRINGS, 'game_controlled': STRINGS,
    'frames': {'type': 'integer', 'minimum': 1, 'maximum': 60},
    'fps': {'type': 'integer', 'minimum': 1, 'maximum': 60},
    'size': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2, 'maxItems': 2},
    'reference_asset_id': TEXT, 'reference_prompt': TEXT,
    'requirement_ids': STRINGS, 'binding': TEXT,
})
REQUIREMENT = obj({'id': TEXT, 'description': TEXT, 'source_document': TEXT,
                   'source_quote': TEXT, 'acceptance': TEXT})
GROUP = obj({'id': TEXT, 'goal': TEXT, 'requirement_ids': STRINGS,
             'launch': {'type': ['string', 'null']}})
PLAN = obj({'style': TEXT, 'requirements': {'type': 'array', 'minItems': 1, 'items': REQUIREMENT},
            'groups': {'type': 'array', 'minItems': 1, 'items': GROUP},
            'assets': {'type': 'array', 'maxItems': 12, 'items': ASSET},
            'procedural_visuals': TEXT})
ISSUE = obj({'id': TEXT, 'problem': TEXT, 'evidence': TEXT, 'requirement_ids': STRINGS,
             'route': {'type': 'string', 'enum': ['code', 'asset', 'both', 'trajectory']},
             'asset_ids': STRINGS, 'acceptance': STRINGS})
DIAGNOSIS = obj({'issues': {'type': 'array', 'items': ISSUE}, 'missing_evidence': STRINGS,
                 'visual_richness': TEXT, 'gameplay_richness': TEXT})
CANDIDATE = obj({'id': {'type': 'string', 'enum': ['a', 'b']}, 'instructions': TEXT,
                 'issue_ids': STRINGS, 'upgrade_reason': TEXT,
                 'materials': {'type': 'array', 'maxItems': 12, 'items': ASSET}})
REPAIR = obj({'reason': TEXT, 'candidates': {'type': 'array', 'maxItems': 2, 'items': CANDIDATE}})
POLICY = obj({'skill': TEXT, 'policy': TEXT})
INDICES = {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}}
SKILL_REFERENCE = obj({'attempt': TEXT, 'frames': INDICES, 'actions': INDICES, 'errors': INDICES})
BEHAVIOR_SKILL = obj({'skill': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                      'reason': TEXT, 'uncertainties': STRINGS,
                      'evidence': {'type': 'array', 'minItems': 1, 'items': SKILL_REFERENCE}})
OBSERVATION = obj({'requirement_id': TEXT, 'observed': {'type': 'boolean'},
                   'passed': {'type': 'boolean'}, 'evidence': TEXT,
                   'frames': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}}})
ASSESSMENT = obj({'requirements': {'type': 'array', 'items': OBSERVATION},
                  'retry_needed': {'type': 'boolean'}, 'retry_reason': TEXT})
SELECTION = obj({'winner': {'type': 'string', 'enum': ['current', 'a', 'b']}, 'reason': TEXT})


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_\-]{0,63}', value):
        raise ValueError(f'Unsafe identifier: {value!r}')
    return value


def unique(rows):
    ids = [identifier(row['id']) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate identifiers')
    return set(ids)


def diagnosis_issue_ids(rows):
    """Issue labels are references within a review, not filesystem names."""
    ids = [row['id'] for row in rows]
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ValueError('Diagnosis issues need nonempty ids')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate diagnosis issue ids')
    return set(ids)


def validate_repair_targets(repair, diagnosed):
    candidates = repair['candidates']
    ids = [row['id'] for row in candidates]
    if ids not in ([], ['a'], ['a', 'b']):
        raise ValueError('Repair must provide a, optionally followed by b')
    if not candidates:
        return set()
    selected = candidates[0]['issue_ids']
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= diagnosed:
        raise ValueError('Repair candidate must target distinct diagnosed issues')
    if any(len(row['issue_ids']) != len(set(row['issue_ids'])) or
           set(row['issue_ids']) != set(selected) for row in candidates[1:]):
        raise ValueError('Repair variants must target the same selected issues')
    return set(selected)


def material_plan(plan, task):
    assets = copy.deepcopy(plan['assets'])
    for asset in assets:
        for key in ('reference_asset_id', 'reference_prompt'):
            if not asset.get(key):
                asset.pop(key, None)
        if asset['kind'] == 'animation':
            asset['loop'] = asset['motion'] == 'loop'
    # The shared generator visits assets in order; references must be accepted first.
    by_id = {asset['id']: asset for asset in assets}
    ordered, visiting, done = [], set(), set()
    def visit(aid):
        if aid in visiting:
            raise ValueError('Cyclic material references')
        if aid in done:
            return
        if aid not in by_id:
            raise ValueError('Material reference is not in the plan')
        visiting.add(aid)
        ref = by_id[aid].get('reference_asset_id')
        if ref:
            visit(ref)
        visiting.remove(aid)
        done.add(aid)
        ordered.append(by_id[aid])
    for aid in by_id:
        visit(aid)
    return dict(schema_version=1, task_id=task, style=plan['style'], assets=ordered)


def validate_plan(plan, docs, group_count, task):
    from gamecraft_assets.contract import validate_shared_style
    from gamecraft_assets.plans import validate_generic
    validate_shared_style(plan['style'])
    requirements = unique(plan['requirements'])
    if not requirements or len(plan['groups']) != group_count:
        raise ValueError('Plan must contain requirements and the configured group count')
    unique(plan['groups'])
    for row in plan['requirements']:
        quote = row['source_quote'].strip()
        document = docs.get(row['source_document'], '')
        # Markdown fences and capitalization do not change the cited words.
        # Keep the contiguous-substring check so paraphrases still fail.
        document = re.sub(r'(?m)^[ \t]*```[^\n]*$', ' ', document)
        if not quote or ' '.join(quote.split()).casefold() not in ' '.join(document.split()).casefold():
            raise ValueError(f"Requirement {row['id']!r} must cite an actual excerpt of {row['source_document']!r}")
        if not row['acceptance'].strip():
            raise ValueError('Requirement needs an observable acceptance target')
    covered = set()
    for group in plan['groups']:
        ids = set(group['requirement_ids'])
        if not ids or not ids <= requirements or not group['goal'].strip():
            raise ValueError('Group must target known requirements')
        covered |= ids
        if group['launch'] is not None:
            identifier(group['launch'])
            if f'`{group["launch"]}`' not in docs['tdd.md'] and f'"{group["launch"]}"' not in docs['tdd.md']:
                raise ValueError('Launch scenario must be declared in the public TDD')
    if covered != requirements:
        raise ValueError('All planned requirements must be covered across groups')
    if not any(group['launch'] is None for group in plan['groups']):
        raise ValueError('At least one group must start from the normal entry')
    assets = unique(plan['assets'])
    if assets:
        validate_generic(material_plan(plan, task))
    elif not plan['procedural_visuals'].strip():
        raise ValueError('A plan without generated materials must explain procedural visuals')
    for asset in plan['assets']:
        if not set(asset['requirement_ids']) <= requirements or not asset['binding'].strip():
            raise ValueError('Material needs a runtime binding and known requirement IDs')
        ref = asset.get('reference_asset_id')
        if ref and (ref not in assets or ref == asset['id']):
            raise ValueError('Material reference must name a different planned asset')
    return plan


def apply_candidate(plan, candidate, docs, task):
    result = copy.deepcopy(plan)
    existing = {a['id']: a for a in result['assets']}
    unique(candidate['materials'])
    for asset in candidate['materials']:
        existing[asset['id']] = copy.deepcopy(asset)
    result['assets'] = list(existing.values())
    validate_plan(result, docs, len(result['groups']), task)
    if candidate['id'] == 'b':
        old = {a['id']: a for a in plan['assets']}
        ranks = {'single_subject': 0, 'coupled_action': 1, 'scene': 2}
        changed = any(
            (a['id'] in old and ranks[a['generation_scope']] > ranks[old[a['id']]['generation_scope']]) or
            (a['id'] in old and old[a['id']]['motion'] == 'static' and a['motion'] != 'static') or
            (a['motion'] != 'static' and old.get(a['reference_asset_id'], {}).get('motion') == 'static')
            for a in candidate['materials'])
        if not changed or not candidate['upgrade_reason'].strip():
            raise ValueError('Candidate b must justify an actual scope or static-to-motion upgrade')
    return result


def validate_assessment(answer, group, trace):
    expected = set(group['requirement_ids'])
    rows = answer['requirements']
    if len(rows) != len(expected) or {x['requirement_id'] for x in rows} != expected:
        raise ValueError('Assessment must report every target requirement exactly once')
    for row in rows:
        if row['passed'] and not row['observed']:
            raise ValueError('Unobserved requirements cannot pass')
        if row['observed'] and (not row['frames'] or not row['evidence'].strip()):
            raise ValueError('Observed outcomes need frame references and an explanation')
        if any(type(n) is not int or not 0 <= n < len(trace['frames']) for n in row['frames']):
            raise ValueError('Assessment references a missing evidence frame')
    return answer


def validate_behavior_skill(answer, attempts):
    if not isinstance(answer.get('skill'), str) or not 1 <= len(answer['skill'].strip()) <= 8000:
        raise ValueError('Behavior Skill must contain 1–8000 characters')
    if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
        raise ValueError('Behavior Skill needs a reflection reason')
    if not isinstance(answer.get('uncertainties'), list) or any(
            not isinstance(x, str) for x in answer['uncertainties']):
        raise ValueError('Behavior Skill uncertainties must be strings')
    traces = {a['id']: a['trace'] for a in attempts}
    if not answer.get('evidence'):
        raise ValueError('Behavior Skill must cite actual playtest evidence')
    for reference in answer['evidence']:
        attempt = reference.get('attempt')
        if isinstance(attempt, str) and attempt not in traces:
            # A reflection sometimes appends a human explanation after the
            # real ID. Retain the cited attempt and validate its indices.
            named = attempt.split(':', 1)[0].strip()
            if ':' in attempt and named in traces:
                reference['attempt'] = named
                attempt = named
        trace = traces.get(attempt)
        if trace is None:
            raise ValueError('Behavior Skill references an unknown attempt')
        if not any(reference.get(key) for key in ('frames', 'actions', 'errors')):
            raise ValueError('Behavior Skill reference must identify an observed event')
        for key in ('frames', 'actions', 'errors'):
            indices = reference.get(key)
            if not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(trace.get(key, []))
                                                   for i in indices):
                raise ValueError(f'Behavior Skill references missing {key}')
    return answer
