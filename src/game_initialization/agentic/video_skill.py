"""Round-boundary learning for the optional agentic initialization video skill."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from game_initialization.batch.__main__ import read, write
from gamecraft_video_skill import (commit_patch, initialize, propose_patch, record_patch,
                                   submit_global_candidate)


def _dir(root):
    return initialize(Path(root) / 'generation')


def sync_to_draft(root):
    root = Path(root)
    source = _dir(root)
    target = root / 'draft/video_skill'
    shutil.copytree(source, target, dirs_exist_ok=True)


def begin_round(root, round_number):
    """Freeze the previous review's candidate rules for this construction round."""
    skill = _dir(root)
    active_path, pending_path = skill/'active.json', skill/'pending.json'
    if active_path.exists():
        active = read(active_path)
        active['round'] = round_number
        write(active_path, active)
        sync_to_draft(root)
        return active
    if pending_path.exists():
        active = read(pending_path)
        active.update(status='active', round=round_number,
                      activated_round=active.get('activated_round', round_number))
        write(active_path, active)
        pending_path.unlink()
        sync_to_draft(root)
        return active
    return {'schema_version': 1, 'status': 'inactive', 'round': round_number, 'patches': []}


def rules_for_assets(root, asset_ids):
    path = _dir(root)/'active.json'
    if not path.exists():
        return []
    wanted = set(asset_ids)
    return [entry['patch'] for entry in read(path).get('patches', [])
            if wanted.intersection(entry.get('asset_ids', []))]


def _was_used(root, entry, first_round, last_round):
    history_path = Path(root)/'generation/asset_history.json'
    if not history_path.exists():
        return False
    rule_id = entry['patch']['id']
    for row in read(history_path):
        context = row.get('context', {})
        recipe = row.get('recipe', {})
        used_round = context.get('round')
        if (context.get('workflow') == 'agentic_initialization'
                and isinstance(used_round, int) and first_round <= used_round <= last_round
                and rule_id in recipe.get('applied_rules', [])):
            return True
    return False


def _asset_issues(review):
    # Coupled defects include placement, timing, or game-state concerns. Keep
    # those out of an H3-only generation skill.
    return [issue for issue in review.get('issues', [])
            if issue.get('blocking', True) and issue.get('repair_side') == 'asset']


def finish_round(root, round_number, max_rounds, review, backend=None):
    """Evaluate active rules and stage new rules from the current visual review.

    A candidate is accepted only when H3 actually used it and the next visual
    review no longer reports an asset-side issue for its source asset. The final
    round's new candidates are retained as provisional evidence for later RSI.
    """
    root = Path(root)
    skill = _dir(root)
    session = read(root/'session.json') if (root/'session.json').exists() else {}
    global_store = session.get('global_video_skill_store')
    task_id = session.get('task_id')
    result_path = skill/f'round-{round_number:03d}.json'
    if result_path.exists():
        return read(result_path)
    issues = _asset_issues(review)
    active_path = skill/'active.json'
    decisions = []
    ineffective = {}
    if active_path.exists():
        active = read(active_path)
        first_round = active.get('activated_round', active.get('round', round_number))
        for entry in active.get('patches', []):
            asset_ids = set(entry.get('asset_ids', []))
            used = _was_used(root, entry, first_round, round_number)
            persists = any(not issue.get('asset_ids') or
                           asset_ids.intersection(issue.get('asset_ids', [])) for issue in issues)
            status = 'accepted' if used and not persists else ('rejected' if used else 'unused')
            evidence = {'construction_round': round_number, 'activated_round': first_round,
                        'asset_ids': sorted(asset_ids), 'used_by_h3': used,
                        'issue_persisted': persists}
            if status == 'accepted':
                commit_patch(root/'generation', entry['patch'], evidence=evidence)
                if global_store and task_id:
                    submit_global_candidate(global_store,entry['patch'],task_id=task_id,
                                            evidence={**evidence,'failures':entry.get('failures',[])})
            else:
                record_patch(root/'generation', entry['patch'], status=status, evidence=evidence)
            if status == 'rejected':
                for asset_id in asset_ids:
                    ineffective.setdefault(asset_id, []).append(
                        'A previously applied candidate instruction did not fix the issue: '
                        + entry['patch']['instruction'])
            decisions.append({'patch_id': entry['patch']['id'], 'status': status, **evidence})
        active_path.unlink()

    plan = {asset['id']: asset for asset in read(root/'generation/asset_plan.json')['assets']}
    grouped = {}
    for issue in issues:
        failure = issue.get('problem', '').strip()
        repair = issue.get('suggested_repair', '').strip()
        message = failure + (('; Suggested repair: ' + repair) if repair else '')
        for asset_id in issue.get('asset_ids', []):
            if asset_id in plan:
                grouped.setdefault(asset_id, []).append(message)
    for asset_id, failures in ineffective.items():
        if asset_id in grouped:
            grouped[asset_id].extend(failures)
    candidates, errors = [], []
    if grouped:
        if backend is None:
            from gamecraft_assets.generate import _backend
            backend = _backend()
        for asset_id, failures in grouped.items():
            try:
                patch = propose_patch(backend, plan[asset_id], failures, workspace=root/'generation')
                if patch is not None:
                    candidates.append({'patch': patch, 'asset_ids': [asset_id],
                                       'failures': failures, 'source_round': round_number})
            except Exception as exc:
                errors.append({'asset_id': asset_id, 'error': f'{type(exc).__name__}: {exc}'})

    candidate_status = None
    if candidates:
        bundle = {'schema_version': 1, 'created_at': time.time(), 'source_round': round_number,
                  'patches': candidates}
        if round_number < max_rounds:
            bundle.update(status='pending', activate_round=round_number+1,
                          activated_round=round_number+1)
            write(skill/'pending.json', bundle)
            candidate_status = 'pending'
        else:
            bundle['status'] = 'provisional_final'
            write(skill/'provisional.json', bundle)
            for entry in candidates:
                record_patch(root/'generation', entry['patch'], status='provisional',
                             evidence={'construction_round': round_number,
                                       'asset_ids': entry['asset_ids'],
                                       'reason': 'No later initialization round available for validation'})
            candidate_status = 'provisional_final'
    result = {'schema_version': 1, 'round': round_number, 'decisions': decisions,
              'candidate_status': candidate_status,
              'candidate_ids': [x['patch']['id'] for x in candidates], 'errors': errors}
    write(result_path, result)
    return result
