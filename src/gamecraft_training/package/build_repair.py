"""Build a complete RSI upgrade, then use three goal-aware playtest/repair passes."""
import hashlib
import json
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from . import models
from .upgrade import _build_candidate, _plan_stage, _play_version


def _goal_review(goal, episodes):
    """Check the two planned tracks without turning unobserved play into success."""
    return models.judge_call(
        'Review this playable game against BOTH planned RSI upgrade tracks. Identify '
        'what the recorded interaction actually shows for core gameplay and aesthetics. '
        'Judge meaningful player choices separately from visual motion and composition. '
        'Mark unvisited content unobserved and distinguish Player mistakes from game '
        'defects. Judge core_playable from whether the original core loop can be played, '
        'not whether the planned upgrade tracks have already succeeded. Missing coverage '
        'of an upgrade goal does not by itself make otherwise attributable replay evidence '
        'invalid. Treat failed or unobserved upgrade goals as concrete repair issues. '
        'A source-file claim is not proof of visible quality. List concrete '
        'repairable issues and note broken original controls. Return only JSON.',
        {'goal': goal, 'outcomes': [{'scenario': e['scenario'],
                                    'completion': e.get('completion'),
                                    'replay': e.get('replay', {})} for e in episodes]},
        models.content(episodes, limit=16)+models.outcome_content(episodes),
        'rsi_build_repair_goal_review', {
            'gameplay_status': {'type': 'string',
                                'enum': ['achieved', 'partial', 'failed', 'unobserved']},
            'aesthetic_status': {'type': 'string',
                                 'enum': ['achieved', 'partial', 'failed', 'unobserved']},
            'core_playable': {'type': 'boolean'},
            'critical_regression': {'type': 'boolean'},
            'evidence_valid': {'type': 'boolean'},
            'issues': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}},
            'reason': {'type': 'string'},
        }, tokens=1900)


def _repair_comparison(goal, issues, before, after):
    """Use the repair mode's matched replay principle for the complete candidate."""
    reverse = bool(int(hashlib.sha256(json.dumps(issues, sort_keys=True).encode()).hexdigest(), 16) & 1)
    versions = (('A', after if reverse else before),
                ('B', before if reverse else after))
    roles = {'A': 'candidate' if reverse else 'incumbent',
             'B': 'incumbent' if reverse else 'candidate'}
    evidence = []
    for label, episodes in versions:
        evidence.append({'type': 'input_text', 'text': 'VERSION '+label})
        evidence.extend(models.content(episodes, limit=12))
        evidence.extend(models.outcome_content(episodes))
    answer = models.judge_call(
        'Compare anonymous complete game versions using the SAME core and upgrade '
        'playtest goals. Prefer a candidate only when at least one diagnosed gameplay '
        'or aesthetic problem visibly improves, the other track and required old '
        'controls do not regress, and evidence is sufficient. A different Player '
        'policy alone is not a game improvement. Return tie or unavailable when '
        'the evidence does not establish a change. Do not require all final goals '
        'to be achieved in this intermediate repair pass. Return only JSON.',
        {'goal': goal, 'diagnosed_issues': issues,
         'version_a_episodes': len(versions[0][1]),
         'version_b_episodes': len(versions[1][1])},
        evidence, 'rsi_build_repair_pairwise', {
            'preferred': {'type': 'string', 'enum': ['A', 'B', 'tie', 'unavailable']},
            'evidence_valid': {'type': 'boolean'},
            'critical_regression': {'type': 'boolean'},
            'improved_tracks': {'type': 'array', 'items': {'type': 'string',
                                   'enum': ['gameplay', 'aesthetic', 'functional']}},
            'regressed_tracks': {'type': 'array', 'items': {'type': 'string',
                                    'enum': ['gameplay', 'aesthetic', 'functional']}},
            'reason': {'type': 'string'},
        }, tokens=1600)
    return {**answer, 'roles': roles,
            'preferred_role': roles.get(answer['preferred'], answer['preferred'])}


def _play(project, tasks, directory, *, skill, goal, adapter, rollout_fn, godot,
          player_model, player_backend, step_frames):
    return _play_version(project, tasks, directory, skill=skill, prior=(), goal=goal,
                         adapter=adapter, rollout_fn=rollout_fn, godot=godot,
                         model=player_model, backend=player_backend,
                         step_frames=step_frames,
                         max_decisions=tasks[0]['max_decisions'])


def run(*, output, original, adapter, config, plan, composition, generator, baseline,
        skill, rounds, goal, tasks, player_backend, player_model, godot, rollout_fn,
        code_editor, edit_policy, asset_python, video_skill, step_frames, services):
    out = Path(output)
    original = Path(original)
    original_digest = digest(original)
    initial_skill = skill
    working, current_runs = original, baseline
    current_plan, current_config, current_generator = plan, config, generator
    build_dir = out/'full_build'
    build_dir.mkdir()
    build_attempts = []
    build_status = 'failed'
    for number in (1, 2):
        attempt_dir = build_dir/f'attempt-{number:03}'
        attempt_dir.mkdir()
        record = {'attempt': number, 'status': 'pending'}
        write_json(out/'progress.json', {'stage': 'rsi_full_build', 'attempt': number})
        try:
            assessment = services.judge(current_config, adapter, original, baseline)
            write_json(attempt_dir/'quality_before.json', assessment)
            feedback = (build_attempts[-1].get('error') or
                        build_attempts[-1].get('review', {}).get('reason', '')) if build_attempts else ''
            option = _plan_stage(goal, 1, original, plan, config, [], assessment, baseline,
                                 attempt_feedback=feedback, phase='full_build')
            write_json(attempt_dir/'build_plan.json', option)
            candidate, candidate_plan, candidate_config, candidate_generator, edit = (
                _build_candidate(original, attempt_dir, option, plan, config, generator,
                                 goal=goal, ordinal=0, editor=code_editor,
                                 edit_policy=edit_policy, adapter=adapter,
                                 assessment=assessment, episodes=baseline, history=[],
                                 asset_python=asset_python, video_skill=video_skill))
            record['code_edit'] = edit
            evidence = _play(candidate, tasks, attempt_dir/'playtest', skill=skill,
                             goal=goal, adapter=adapter, rollout_fn=rollout_fn,
                             godot=godot, player_model=player_model,
                             player_backend=player_backend, step_frames=step_frames)
            review = _goal_review(goal, evidence)
            record.update(candidate=str(candidate), review=review)
            # Admit a playable candidate for repair, not only one that already
            # demonstrates the upgrade. Track failures and missing coverage stay
            # in the review so subsequent playtest/repair rounds can address them.
            if (review['core_playable'] and review['evidence_valid'] and
                    not review['critical_regression']):
                working, current_runs = candidate, evidence
                current_plan, current_config, current_generator = (
                    candidate_plan, candidate_config, candidate_generator)
                record['status'] = build_status = 'playable'
            else:
                record['status'] = 'rejected_unplayable'
        except Exception as exc:
            record.update(status='error', error=f'{type(exc).__name__}: {exc}')
        build_attempts.append(record)
        write_json(attempt_dir/'result.json', record)
        write_json(build_dir/'attempts.json', build_attempts)
        if build_status == 'playable':
            break
    write_json(build_dir/'result.json', {'status': build_status,
                                        'project': str(working), 'attempts': len(build_attempts)})
    history = []
    if build_status == 'playable':
        write_json(out/'current_asset_plan.json', current_plan)
        write_json(out/'current_training.json',
                   {k: v for k, v in current_config.items() if not k.startswith('_')})
    for ordinal in range(1, rounds+1) if build_status == 'playable' else ():
        rd = out/f'round-{ordinal:03}'
        rd.mkdir()
        record = {'round': ordinal, 'project_before': str(working),
                  'status': 'pending', 'action': 'playtest'}
        write_json(out/'progress.json', {'stage': 'rsi_build_repair',
                                        'round': ordinal, 'max_rounds': rounds})
        try:
            # Every round starts with an independently authored Player policy.
            before = _play(working, tasks, rd/'playtest', skill=skill, goal=goal,
                           adapter=adapter, rollout_fn=rollout_fn, godot=godot,
                           player_model=player_model, player_backend=player_backend,
                           step_frames=step_frames)
            current_runs = before
            assessment = services.judge(current_config, adapter, working, before)
            write_json(rd/'quality_before.json', assessment)
            goal_review = _goal_review(goal, before)
            write_json(rd/'goal_review.json', goal_review)
            routed = services.route(current_config, adapter, working, current_plan,
                                    composition, assessment, before, history)
            write_json(rd/'repair_route.json', routed)
            record['goal_review'] = goal_review
            record['issue_count'] = len(routed['issues'])
            needs_repair = bool(routed['issues'] or goal_review['issues'])
            if not needs_repair:
                record.update(status='reviewed', action='no_observed_issue')
            elif not goal_review['evidence_valid'] and not routed['issues']:
                record.update(status='reviewed', action='insufficient_goal_evidence')
            else:
                option = _plan_stage(goal, ordinal, working, current_plan, current_config,
                                     history, assessment, before, phase='repair',
                                     repair_context={'route': routed, 'goal_review': goal_review})
                write_json(rd/'repair_plan.json', option)
                candidate, candidate_plan, candidate_config, candidate_generator, edit = (
                    _build_candidate(working, rd, option, current_plan, current_config,
                                     current_generator, goal=goal, ordinal=ordinal,
                                     editor=code_editor, edit_policy=edit_policy,
                                     adapter=adapter, assessment=assessment, episodes=before,
                                     history=history, asset_python=asset_python,
                                     video_skill=video_skill))
                record['code_edit'] = edit
                after = _play(candidate, tasks, rd/'candidate_playtest', skill=skill,
                              goal=goal, adapter=adapter, rollout_fn=rollout_fn,
                              godot=godot, player_model=player_model,
                              player_backend=player_backend, step_frames=step_frames)
                comparison = _repair_comparison(goal, {'route': routed['issues'],
                                                       'goal': goal_review['issues']},
                                                before, after)
                write_json(rd/'comparison.json', comparison)
                record['comparison'] = comparison
                record['candidate'] = str(candidate)
                accepted = (comparison['preferred_role'] == 'candidate' and
                            comparison['evidence_valid'] and
                            not comparison['critical_regression'] and
                            bool(comparison['improved_tracks']) and
                            not comparison['regressed_tracks'])
                if accepted:
                    working, current_runs = candidate, after
                    current_plan, current_config, current_generator = (
                        candidate_plan, candidate_config, candidate_generator)
                    record.update(status='reviewed', action='accept_repair')
                    write_json(out/'current_asset_plan.json', current_plan)
                    write_json(out/'current_training.json',
                               {k: v for k, v in current_config.items() if not k.startswith('_')})
                else:
                    record.update(status='reviewed', action='reject_repair')
            try:
                proposal = services.skill(adapter, skill, current_runs, history)
                write_json(rd/'skill_proposal.json', proposal)
                if proposal['skill'].strip() and proposal['skill'].strip() != skill:
                    skill = proposal['skill'].strip()
                    record['behavior'] = 'adopt_unverified'
                else:
                    record['behavior'] = 'unchanged'
            except Exception as exc:
                record['behavior'] = 'error'
                record['behavior_error'] = f'{type(exc).__name__}: {exc}'
        except Exception as exc:
            record.update(status='error', action='error',
                          error=f'{type(exc).__name__}: {exc}')
        record['project_after'] = str(working)
        history.append(record)
        write_json(rd/'result.json', record)
        write_json(out/'history.json', history)
        print(f'[RSI build-repair] round {ordinal}/{rounds}: {record["action"]}', flush=True)

    # Select the latest version admitted by the build and round reviews.
    # No second, sparsely sampled original-versus-upgrade Judge is run.
    upgraded = working != original
    final = {'status': 'accepted' if upgraded else 'not_run',
             'selected': 'upgrade' if upgraded else 'original',
             'selection_method': 'automatic_after_round_review',
             'final_judge_run': False}
    write_json(out/'final_upgrade_review.json', final)
    selected = working if final['selected'] == 'upgrade' else original
    selected_plan = current_plan if final['selected'] == 'upgrade' else plan
    selected_config = current_config if final['selected'] == 'upgrade' else config
    write_json(out/'selected_asset_plan.json', selected_plan)
    write_json(out/'selected_training.json',
               {k: v for k, v in selected_config.items() if not k.startswith('_')})
    write_json(out/'selected_bindings.json', selected_config['assets']['bindings'])
    if digest(original) != original_digest:
        raise RuntimeError('RSI build-and-repair changed the original game')
    (out/'working_skill.txt').write_text(skill)
    (out/'best_skill.txt').write_text(skill if final['selected'] == 'upgrade' else initial_skill)
    errors = ([{'stage': 'full_build', 'reason': row['error']} for row in build_attempts
               if row['status'] == 'error'] +
              [{'round': row['round'], 'reason': row['error']} for row in history
               if row['status'] == 'error'])
    result = {'profile': 'rsi', 'rsi_mode': 'upgrade', 'execution_mode': 'build_repair',
              'planning_mode': 'evidence', 'state': 'completed',
              'upgrade_status': final['status'], 'full_build_status': build_status,
              'project': str(selected), 'original_project': str(original),
              'working_project': str(working), 'skill': str(out/'best_skill.txt'),
              'working_skill': str(out/'working_skill.txt'),
              'upgrade_plan': str(out/'rsi_upgrade_plan.json'),
              'full_build': str(build_dir/'result.json'),
              'final_review': str(out/'final_upgrade_review.json'),
              'selected_asset_plan': str(out/'selected_asset_plan.json'),
              'selected_training': str(out/'selected_training.json'),
              'rounds_completed': sum(row['status'] == 'reviewed' for row in history),
              'rounds_attempted': len(history),
              'attempts_total': len(build_attempts) + sum('candidate' in row for row in history),
              'errors': errors, 'behavior_skill_verified': False,
              'final_project_evaluated': False,
              'official_benchmark_metric': False}
    if (build_status != 'playable' or result['rounds_completed'] < rounds or errors or
            final['status'] == 'error'):
        result['state'] = 'completed_with_errors'
    if video_skill:
        from .continuation import save_selected_video_skill
        selected_generator = current_generator if upgraded else generator
        result['selected_video_skill'] = save_selected_video_skill(out, selected_generator)
    write_json(out/'result.json', result)
    write_json(out/'progress.json', {'stage': result['state'],
                                     'result': str(out/'result.json')})
    print(json.dumps(result), flush=True)
    return result
