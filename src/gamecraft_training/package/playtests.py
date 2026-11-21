"""Candidate-owned RSI acceptance policies, independent of learned Behavior Skill."""
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.program_policy import program_player


def replay_candidate(project, episodes, output, *, adapter, diagnosis, changes,
                     rollout, godot, model, backend, step_frames, max_decisions,
                     player_state, policy_factory=None):
    if player_state != 'engine':
        raise ValueError('RSI candidate acceptance policy requires engine state')
    if not episodes:
        raise ValueError('RSI candidate playtest needs baseline scenarios')
    factory = policy_factory or program_player
    project, output = Path(project), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    results = []
    for index, original in enumerate(episodes):
        directory = output / f'episode-{index:03}'
        # Policies are private to the candidate AND scenario. Never transfer the
        # training Skill or replay the incumbent's recorded action sequence.
        context = {'scenario': original['scenario'],
                   'normal_launch': bool(original.get('normal_launch', False)),
                   'diagnosis': diagnosis, 'candidate_changes': changes,
                   'goal': 'Exercise the diagnosed interactions and check visible repair effects.'}
        objective = original.get('run_objective')
        policy_options = {'run_objective': objective} if objective is not None else {}
        if objective is not None:
            context['run_objective'] = objective
            context['goal'] += ' Preserve the fixed overall objective and continuation/end conditions.'
        policy_path = output / f'policy-{index:03}.py'
        decide = factory(project, policy_path, model=model, backend=backend,
                         adapter=adapter, playtest_context=context, **policy_options)
        result = rollout(project, directory, '', decide, godot=godot, adapter=adapter,
                         scenario=original['scenario'], normal_launch=context['normal_launch'],
                         step_frames=original.get('decision_frames', step_frames),
                         max_decisions=max_decisions, player_state=player_state)
        if (not result.get('replay_verified') or (not result.get('decisions') and
                not (objective is not None and result.get('frames') == 0))
                or result.get('scenario') != original['scenario']
                or bool(result.get('normal_launch')) != context['normal_launch']
                or result.get('project_sha256') != fingerprint):
            raise RuntimeError('Candidate acceptance replay has invalid provenance or coverage')
        if digest(project) != fingerprint:
            raise RuntimeError('Candidate changed during acceptance playtest')
        if not policy_path.is_file():
            raise RuntimeError('Candidate acceptance policy was not authored')
        for key in ('trace', 'video'):
            if not result.get(key) or not Path(result[key]).is_file():
                raise RuntimeError(f'Candidate acceptance replay missing {key}')
        result['player_policy'] = str(policy_path)
        result['playtest'] = {'purpose': 'candidate_acceptance', 'uses_behavior_skill': False,
                              'context': context, 'policy': str(policy_path)}
        write_json(directory / 'result.json', result)
        results.append(result)
    write_json(output / 'result.json', {
        'status': 'replay_verified', 'project': str(project), 'project_sha256': fingerprint,
        'uses_behavior_skill': False,
        'episodes': [{'scenario': r['scenario'], 'policy': r['player_policy'],
                      'video': r.get('video'), 'trace': r.get('trace')} for r in results]})
    return results
