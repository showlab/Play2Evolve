"""One version-owned reactive program, with explicitly bound episode profiles.

The Python runtime is retained; profile dispatch is injected into observations.
Policies are never mutated to switch profiles, so saved programs remain replayable.
"""
import copy
import hashlib
from pathlib import Path

from gamecraft_behavior.core import digest, write_json


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def profiles_for(tasks, *, group='', goal='Explore the core gameplay through legal controls.'):
    return [{'profile': task.get('profile', f'{group}/{task["id"]}' if group else task['id']),
             'scenario': task['id'], 'normal_launch': task.get('launch', 'scenario') == 'default',
             'goal': task.get('goal', goal)} for task in tasks]


def prior_context(episodes):
    if not episodes:
        return {'prior_policy': None, 'prior_observations': None}
    paths = {r.get('player_policy') for r in episodes}
    if len(paths) != 1 or None in paths:
        # Legacy interactive episodes do not carry an inheritable program.
        if all(not r.get('player_policy') for r in episodes):
            return {'prior_policy': None, 'prior_observations': None}
        raise RuntimeError('Selected episodes must share one version-owned policy')
    path = Path(next(iter(paths)))
    for row in episodes:
        expected = row.get('playtest_files', {}).get('player_policy')
        if not path.is_file() or (expected and file_hash(path) != expected):
            raise RuntimeError('Selected Player policy is missing or changed')
    return {'prior_policy': path.read_text(), 'prior_observations': [
        {'profile': r.get('player_profile'), 'scenario': r['scenario'],
         'final_state': r.get('replay', {}),
         'actions': [d['action'] for d in r['decisions']]} for r in episodes]}


def play_version(project, tasks, output, *, profiles, prior=(), context=None,
                 skill='', reuse=False, adapter, rollout, godot, model, backend,
                 step_frames, max_decisions, factory):
    project, output = Path(project), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    context = {**(context or {}), **prior_context(prior), 'profiles': copy.deepcopy(profiles)}
    if len({p['profile'] for p in profiles}) != len(profiles):
        raise ValueError('Player profiles must have unique labels')
    policy_path = output / 'policy.py'
    if (reuse and prior and all(r.get('project_sha256') == fingerprint for r in prior)
            and all(r.get('player_plan') == profiles and r.get('player_skill', '') == skill for r in prior)):
        context['reuse_source'] = context['prior_policy']
    write_json(output / 'plan.json', {'profiles': profiles, 'project_sha256': fingerprint})
    decide = factory(project, policy_path, model=model, backend=backend,
                     adapter=adapter, playtest_context=context)
    results = []
    known = {p['profile']: p for p in profiles}
    for index, task in enumerate(tasks):
        profile = task.get('profile', task['id'])
        spec = known[profile]
        if spec['scenario'] != task['id'] or spec['normal_launch'] != (task.get('launch', 'scenario') == 'default'):
            raise ValueError('Profile launch does not match episode')
        # A fresh rollout provides empty history. The compiled program is pure;
        # only the profile binding changes between episodes (RGC reset analogue).
        if hasattr(decide, 'set_profile'):
            decide.set_profile(profile)
        directory = output / f'episode-{index:03}'
        result = rollout(project, directory, skill, decide, godot=godot, adapter=adapter,
                         scenario=task['id'], normal_launch=spec['normal_launch'],
                         step_frames=task.get('decision_frames', step_frames),
                         max_decisions=task.get('max_decisions', max_decisions),
                         player_state='engine')
        if (digest(project) != fingerprint or result.get('project_sha256') != fingerprint
                or not result.get('replay_verified') or not result.get('decisions')
                or result.get('scenario') != task['id']
                or bool(result.get('normal_launch')) != spec['normal_launch']):
            raise RuntimeError('Player produced invalid version/episode evidence')
        result.update(player_policy=str(policy_path), player_profile=profile,
                      player_plan=copy.deepcopy(profiles), player_skill=skill)
        for key in ('player_policy', 'trace', 'video'):
            path = Path(result.get(key, ''))
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError(f'Player missing {key}')
        result['playtest_files'] = {k: file_hash(result[k]) for k in ('player_policy', 'trace', 'video')}
        result['playtest'] = {'purpose': context.get('phase', 'candidate_acceptance'),
                             'uses_behavior_skill': bool(skill and context.get('behavior_skill_enabled')),
                             'context': {k: v for k, v in context.items() if k != 'reuse_source'},
                             'policy': str(policy_path), 'profile': profile, 'goal': spec['goal']}
        write_json(directory / 'result.json', result)
        results.append(result)
    write_json(output / 'result.json', {'status': 'replay_verified', 'project_sha256': fingerprint,
               'uses_behavior_skill': bool(skill and context.get('behavior_skill_enabled')),
               'policy': str(policy_path), 'profiles': profiles,
               'episodes': [{**r, 'policy': r['player_policy']} for r in results]})
    return results
