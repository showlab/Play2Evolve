"""Bounded, evidence-linked advice for the Codex agent that plans H3 usage.

This store never changes H3 parameters or the game-playing Behavior Skill.  A
lesson is a planning hypothesis; the game replay remains the authority on whether
the resulting game improved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from gamecraft_assets.plans import write_json

SCOPES = {'any', 'single_subject', 'coupled_action', 'scene'}
KINDS = {'any', 'sprite', 'animation', 'background'}
MAX_LOCAL_LESSONS = 64
MAX_RETRIEVED = 4
_WORDS = re.compile(r'[a-z][a-z0-9_]{3,}', re.I)
_CJK = re.compile(r'[\u3400-\u9fff]+')


def _read(path):
    path = Path(path)
    if not path.exists():
        return {'schema_version': 1, 'lessons': []}
    value = json.loads(path.read_text())
    if value.get('schema_version') != 1 or not isinstance(value.get('lessons'), list):
        raise ValueError('Invalid Video Skill decision library')
    return value


def _short(value, length=500):
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise ValueError('Video Skill decision text must be nonempty and bounded')
    return value.strip()


def _validate(lesson):
    if lesson.get('kind') not in KINDS or lesson.get('scope') not in SCOPES:
        raise ValueError('Invalid Video Skill decision applicability')
    for key in ('trigger', 'decision', 'reason'):
        _short(lesson.get(key))
    if lesson.get('outcome') not in {'improved', 'failed'}:
        raise ValueError('Video Skill decision needs a measured outcome')
    return lesson


def _identifier(lesson):
    fields = {key: lesson[key].strip().lower() for key in ('kind', 'scope', 'trigger', 'decision')}
    return 'decision_' + hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


def _tokens(value):
    words = set(_WORDS.findall(value.lower()))
    for segment in _CJK.findall(value):
        words.update(segment[index:index+2] for index in range(len(segment)-1))
    return words


def _score(lesson, *, kinds, scopes, query):
    if lesson['kind'] != 'any' and kinds and lesson['kind'] not in kinds:
        return None
    if lesson['scope'] != 'any' and scopes and lesson['scope'] not in scopes:
        return None
    words = _tokens(lesson['trigger'] + ' ' + lesson['decision'])
    query_words = _tokens(query)
    overlap = len(words & query_words)
    if query_words and not overlap:
        return None
    support = min(lesson.get('positive', 0), 3) - min(lesson.get('negative', 0), 3)
    return overlap * 3 + support + (2 if lesson['kind'] != 'any' else 0) + (1 if lesson['scope'] != 'any' else 0)


def retrieve(workspace, *, assets=(), problems=(), task_id=None, global_store=None,
             limit=MAX_RETRIEVED):
    """Return only relevant decisions, with provenance and outcome counts.

    Cross-game evidence is advisory even after a successful source-game replay;
    it is never silently promoted to an H3 prompt rule.
    """
    if not 1 <= limit <= MAX_RETRIEVED:
        raise ValueError('Video Skill retrieval limit must be 1–4')
    assets = list(assets)
    kinds = {a.get('kind') for a in assets}
    scopes = {a.get('generation_scope') for a in assets}
    query = ' '.join([*map(str, problems), *(a.get('description', '') + ' ' + a.get('requirements', '') for a in assets)])
    local = _read(Path(workspace) / 'video_skill' / 'decisions.json')['lessons']
    candidates = [dict(row, transfer='local') for row in local]
    if global_store:
        path = Path(global_store) / 'decision_evidence.jsonl'
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines()[-1000:] if line.strip()]
            grouped = {}
            for row in rows:
                if row.get('task_id') == task_id:
                    continue
                lesson = row.get('lesson', {})
                try:
                    _validate(lesson)
                except ValueError:
                    continue
                if lesson['outcome'] != 'improved':
                    continue
                ident = _identifier(lesson)
                entry = grouped.setdefault(ident, dict(lesson, id=ident, positive=0,
                                                        negative=0, source_games=[]))
                if row['task_id'] not in entry['source_games']:
                    entry['source_games'].append(row['task_id'])
                    entry['positive'] += 1
            candidates.extend(dict(row, transfer='cross_game_hypothesis') for row in grouped.values())
    ranked = []
    for lesson in candidates:
        score = _score(lesson, kinds=kinds, scopes=scopes, query=query)
        if score is not None:
            ranked.append((score, lesson))
    ranked.sort(key=lambda item: (-item[0], -item[1].get('positive', 0), item[1]['id']))
    result = []
    seen = set()
    for score, lesson in ranked:
        if lesson['id'] in seen:
            continue
        seen.add(lesson['id'])
        result.append({'id': lesson['id'], 'trigger': lesson['trigger'],
                       'decision': lesson['decision'], 'reason': lesson['reason'],
                       'kind': lesson['kind'], 'scope': lesson['scope'],
                       'positive': lesson.get('positive', 0),
                       'negative': lesson.get('negative', 0),
                       'guidance_status': ('caution' if lesson.get('negative', 0) >= lesson.get('positive', 0)
                                           else 'supported'),
                       'transfer': lesson['transfer'], 'source_games': lesson.get('source_games', []),
                       'retrieval_score': score})
        if len(result) == limit:
            break
    return result


def learn(workspace, *, task_id, phase, problems, decisions, outcome,
          evidence=None, global_store=None, caller=None):
    """Distill replay-tested planning choices with Codex; retain bounded local memory.

    Failed choices are retained as counterexamples, but only improvements are
    shared across games.  No lesson is made when no candidate was compared.
    """
    if outcome not in {'improved', 'failed'} or not decisions:
        return []
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['lessons'],
              'properties': {'lessons': {'type': 'array', 'maxItems': 2, 'items': {
                  'type': 'object', 'additionalProperties': False,
                  'required': ['kind', 'scope', 'trigger', 'decision', 'reason'],
                  'properties': {'kind': {'type': 'string', 'enum': sorted(KINDS)},
                                 'scope': {'type': 'string', 'enum': sorted(SCOPES)},
                                 'trigger': {'type': 'string'}, 'decision': {'type': 'string'},
                                 'reason': {'type': 'string'}}}}}}
    if caller is None:
        import llm
        def caller(model, instructions, data, response_schema):
            return llm.call(model, instructions,
                            [{'type': 'input_text', 'text': json.dumps(data, ensure_ascii=False)}],
                            'video_skill_decision_learning', response_schema,
                            backend='codex', max_tokens=1200)
    model = os.environ.get('GAMECRAFT_VIDEO_SKILL_MODEL', 'gpt-6-sol')
    answer = caller(model,
        'Distill at most two reusable decisions about how a Codex game-building agent should PLAN or REPAIR H3 '
        'materials. Use only the supplied diagnosed problem, actual planning choice, and matched game replay outcome. '
        'A decision may concern when an observed static subject needs internal motion, what to generate, '
        'single-subject/coupled-action/scene granularity, reference '
        'conditioning, or whether a defect belongs to H3 or game integration. Never claim that a combined code '
        'and material change proves which component caused the gain. A successful code-only repair shows that '
        'this route worked; it does not prove an H3 alternative would fail. For a failed outcome, describe the attempted '
        'choice as a caution. State observable trigger conditions and actionable planning advice. Generalize away '
        'game names, asset IDs, prompts, colors, code paths, scores and scenario names. Do not recommend changing '
        'H3 model parameters or gameplay policy. Return no lesson if the evidence is inconclusive or too specific.',
        {'phase': phase, 'problems': list(problems)[:8], 'decisions': decisions,
         'outcome': outcome, 'evidence': evidence or {}}, schema)
    root = Path(workspace) / 'video_skill'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'decisions.json'
    library = _read(path)
    learned = []
    for item in answer['lessons']:
        item = _validate({**item, 'outcome': outcome})
        ident = _identifier(item)
        existing = next((row for row in library['lessons'] if row['id'] == ident), None)
        if existing:
            existing['positive' if outcome == 'improved' else 'negative'] += 1
            existing['last_seen'] = time.time()
        else:
            library['lessons'].append({**item, 'id': ident,
                                       'positive': int(outcome == 'improved'),
                                       'negative': int(outcome == 'failed'),
                                       'source_games': [task_id], 'last_seen': time.time()})
        learned.append(ident)
        if global_store and outcome == 'improved':
            store = Path(global_store)
            store.mkdir(parents=True, exist_ok=True)
            ledger = store / 'decision_evidence.jsonl'
            row = {'id': ident, 'task_id': task_id, 'phase': phase, 'lesson': item,
                   'evidence': evidence or {}}
            existing_rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] if ledger.exists() else []
            if not any(old['id'] == ident and old['task_id'] == task_id for old in existing_rows):
                with ledger.open('a') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    supported = [row for row in library['lessons'] if row['positive'] > row['negative']]
    cautions = [row for row in library['lessons'] if row['positive'] <= row['negative']]
    supported.sort(key=lambda row: (row['positive'] - row['negative'], row['last_seen']), reverse=True)
    cautions.sort(key=lambda row: (row['negative'] - row['positive'], row['last_seen']), reverse=True)
    # Preserve counterexamples even after many successful games; the memory
    # budget is for useful decisions, not just the most positive outcomes.
    selected = supported[:48] + cautions[:16]
    remaining = supported[48:] + cautions[16:]
    library['lessons'] = (selected + remaining[:MAX_LOCAL_LESSONS-len(selected)])[:MAX_LOCAL_LESSONS]
    write_json(path, library)
    return learned


def round_case(repair_plan, record):
    """Extract one replayed choice about using or avoiding H3 in a round."""
    problems = [item.get('problem', '') for item in repair_plan.get('issues', [])]
    replayed = []
    for group in record.get('groups', []):
        options = {option['kind']: option for option in group['plan'].get('candidates', [])}
        compared = [candidate for candidate in group.get('candidates', [])
                    if candidate.get('replay') and candidate.get('action') in {'accept', 'reject'}
                    and candidate.get('baseline_reviews')
                    and candidate.get('kind') in options]
        replayed.extend((group, options, candidate) for candidate in compared)
    if replayed:
        group, options, selected = next(
            (item for item in replayed if item[2]['action'] == 'accept'), replayed[0])
        option = options[selected['kind']]
        outcome = 'improved' if selected.get('action') == 'accept' else 'failed'
        decision = {'scope': option['scope'], 'kind': selected['kind'],
                    'upgrade_axes': selected.get('upgrade_axes',[]),
                    'h3_used': bool(option['materials']),
                    'video_controls': option.get('video_controls', []),
                    'code_controls': option.get('code_controls', []),
                    'material_roles': [entry['asset'].get('description', '') for entry in option['materials']],
                    'repair_units': [{'root_cause': unit.get('root_cause'),
                                      'scope': unit.get('scope'),
                                      'upgrade_axis': unit.get('upgrade_axis')}
                                     for unit in option.get('repair_units', [])]}
        return problems, [decision], outcome, {
            'comparison': selected.get('baseline_reviews', []),
            'group': group['id'], 'candidate': selected['kind']}
    asset = record.get('sides', {}).get('asset', {})
    if asset.get('action') in {'accept', 'reject'} and asset.get('pairwise_reviews'):
        decisions = repair_plan.get('decisions') or [
            {'route': item['route'], 'asset_ids': item.get('asset_ids', []),
             'problem': item.get('problem', '')}
            for item in repair_plan.get('issues', []) if item.get('route') in {'asset', 'both'}]
        if decisions:
            return problems, decisions, ('improved' if asset['action'] == 'accept' else 'failed'), {
                'comparison': asset['pairwise_reviews']}
    composition = record.get('sides', {}).get('composition', {})
    if composition.get('action') == 'accept' and composition.get('pairwise_reviews'):
        code_only = [{'route': item.get('route'), 'problem': item.get('problem', ''),
                      'h3_used': False}
                     for item in repair_plan.get('issues', [])
                     if item.get('route') == 'composition']
        if code_only:
            return problems, code_only, 'improved', {'comparison': composition['pairwise_reviews']}
    return None
