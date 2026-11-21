"""Evidence-based, game-local reflection independent of the Player author."""
from pathlib import Path
import subprocess

from . import contracts as c
from .__main__ import digest, read_json, write_json


def reflect_playtest(services, version, group, output, evidence, *, prior=None, changes=None):
    """Summarize both attempts once, without replaying or changing the selected policy."""
    if evidence.get('behavior_skill'):
        return evidence
    attempts, inputs = [], []
    for number in (1, 2):
        run = output / f'attempt-{number}'
        paths = [run / 'policy.json', run / 'assessment.json', run / 'replay/trace.json']
        if not all(path.is_file() for path in paths):
            continue
        trace = read_json(paths[2])
        attempts.append(dict(id=run.name, path=str(run), selected=str(run) == evidence['path'],
                             policy=read_json(paths[0]), assessment=read_json(paths[1]), trace=trace))
        inputs += paths
        inputs += [run / 'replay' / frame['image'] for frame in trace['frames']]
    if not attempts or not any(attempt['selected'] for attempt in attempts):
        raise ValueError('Behavior Skill reflection needs the selected playtest evidence')
    hashes = {str(path.relative_to(output)): digest(path) for path in inputs}
    directory = output / 'skill-reflection'
    directory.mkdir(exist_ok=True)
    failure = None
    try:
        answer = services.summarize_skill(read_json(version / 'plan.json'), group, directory,
                                         attempts, prior=prior, changes=changes)
        c.validate_behavior_skill(answer, attempts)
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        failure = str(exc)
        write_json(directory / 'error.json', {'error': failure})
    if any(digest(output / name) != sha for name, sha in hashes.items()):
        raise ValueError('Reflection changed its source playtest evidence')
    if failure is not None:
        answer = dict(skill=evidence['policy']['skill'], reason='Reflection unavailable; retain executed skill.',
                      uncertainties=[failure], evidence=[])
    record = dict(format='gameasg-behavior-skill-v1', version_hash=evidence['version_hash'],
                  group_id=group['id'], selected_attempt=Path(evidence['path']).name,
                  verification='unverified' if failure else 'pending_next_playtest',
                  previous_skill=(prior or {}).get('skill', ''),
                  input_artifacts=hashes, **answer)
    write_json(directory / 'behavior_skill.json', record)
    (directory / 'behavior_skill.md').write_text(answer['skill'].strip() + '\n')
    # Keep the actually executed policy.json and policy.js unchanged. Only the
    # guidance handed to the NEXT author is updated after observing this run.
    evidence = {**evidence, 'policy': {**evidence['policy'], 'skill': answer['skill'].strip(),
                    'skill_source': 'executed_policy' if failure else 'post_play_reflection',
                    'skill_verification': record['verification'],
                    'skill_uncertainties': answer['uncertainties']},
                'behavior_skill': record,
                'artifacts': {**evidence['artifacts'], **hashes}}
    for name in ('behavior_skill.json', 'behavior_skill.md'):
        path = directory / name
        evidence['artifacts'][str(path.relative_to(output))] = digest(path)
    write_json(output / 'evidence.json', evidence)
    return evidence
