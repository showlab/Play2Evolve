"""Version-owned, step-by-step initialization playtests.

Unlike program policies, the configured model chooses each action after seeing
the latest frame. The same verified rollout and selection code is used.
"""
import copy
import hashlib
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.runtime import player


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InteractiveInitializationPlayer:
    def __init__(self, *, adapter, rollout, godot, model, backend, step_frames,
                 max_decisions, mode='agent', factory=None, game_brief=''):
        self.adapter, self.rollout, self.godot = adapter, rollout, godot
        self.model, self.backend = model, backend
        self.step_frames, self.max_decisions = step_frames, max_decisions
        self.mode = mode
        self.factory = factory or player
        self.game_brief = game_brief
        self.selected = {}
        self.latest = []

    def reusable(self, project, unit):
        entry = self.selected.get(unit['id'])
        if not entry or entry['project_sha256'] != digest(project) or entry['unit'] != unit:
            return None
        for episode in entry['episodes']:
            for key in ('trace', 'video'):
                path = Path(episode.get(key, ''))
                if not path.is_file() or _hash(path) != episode['playtest_files'][key]:
                    return None
            if any(not Path(d.get('image', '')).is_file() for d in episode['decisions']):
                return None
        return copy.deepcopy(entry['episodes'])

    def play(self, project, tasks, output, *, unit, prior=(), diagnosis=None, changes=None,
             skill=''):
        project, output = Path(project), Path(output)
        output.mkdir(parents=True, exist_ok=False)
        fingerprint = digest(project)
        acting_adapter = self.adapter
        replaced = getattr(self.adapter, 'pointer_replaces', ())
        if replaced:
            acting_adapter = copy.copy(self.adapter)
            acting_adapter.actions = {k:v for k,v in self.adapter.actions.items() if k not in replaced}
        context = {'phase': 'initialization', 'mode': self.mode,
                   'group': unit['id'], 'goal': unit.get('goal', unit.get('purpose', 'Explore the game')),
                   'diagnosis': diagnosis, 'candidate_changes': changes}
        if prior:
            context['previous_observations'] = [
                {'scenario': r['scenario'], 'actions': [{k:d[k] for k in ('action','pointer') if k in d}
                                                     for d in r['decisions']],
                 'final_state': r.get('replay', {})} for r in prior]
        results = []
        brief = project.parent / 'instruction.md'
        public_brief = self.game_brief or (brief.read_text(errors='replace')[:6000]
                                          if brief.is_file() else '')
        for index, task in enumerate(tasks):
            goal = task.get('goal') or context['goal']
            guidance = ('Initialization playtest. Exercise the stated game requirements through legal controls. '
                        'Prioritize observable coverage over a high score. '
                        'Do not assume an intended interaction succeeded; inspect the next observation. '
                        f'Current goal: {goal}.')
            if public_brief:
                guidance += '\nGAME BRIEF (public requirements):\n' + public_brief
            opening = [action for action in task.get('actions', [])
                       if isinstance(action, str) and action in acting_adapter.allowed_actions][:8]
            if opening:
                guidance += ('\nDECLARED DEMO OPENING (reference only; adapt to the current screen): '
                             + ', '.join(opening) + '.')
            if skill:
                guidance += '\nBEHAVIOR SKILL (apply when its visible triggers occur):\n' + skill
            if diagnosis is not None:
                guidance += f' Candidate repair to examine: {str(diagnosis.get("problem", ""))[:600]}.'
            if changes is not None:
                guidance += f' Candidate changes: {str(changes)[:600]}.'
            previous = next((r for r in prior if r['scenario'] == task['id']), None)
            if previous is not None:
                guidance += (' Earlier version actions (guidance, not a fixed script): '
                             + ', '.join(d['action'] for d in previous['decisions'][:40]) + '.')
            decide = self.factory(model=self.model, backend=self.backend, adapter=acting_adapter)
            result = self.rollout(project, output / f'episode-{index:03}', guidance, decide,
                                  godot=self.godot, adapter=acting_adapter, scenario=task['id'],
                                  normal_launch=task.get('launch', 'scenario') == 'default',
                                  step_frames=self.step_frames, max_decisions=self.max_decisions,
                                  player_state='engine')
            if (digest(project) != fingerprint or result.get('project_sha256') != fingerprint
                    or not result.get('replay_verified') or not result.get('decisions')
                    or result.get('scenario') != task['id']
                    or bool(result.get('normal_launch')) != (task.get('launch', 'scenario') == 'default')):
                raise RuntimeError('Interactive Player produced invalid version/episode evidence')
            for key in ('trace', 'video'):
                path = Path(result.get(key, ''))
                if not path.is_file() or not path.stat().st_size:
                    raise RuntimeError(f'Interactive Player missing {key}')
            result['playtest_files'] = {key: _hash(result[key]) for key in ('trace', 'video')}
            result['initialization_task'] = copy.deepcopy(task)
            result['initialization_unit'] = copy.deepcopy(unit)
            result['playtest'] = {'purpose': 'initialization', 'mode': self.mode,
                                  'uses_behavior_skill': bool(skill), 'context': context,
                                  'goal': goal, 'backend': self.backend, 'model': self.model}
            write_json(output / f'episode-{index:03}' / 'result.json', result)
            results.append(result)
        write_json(output / 'result.json', {'status': 'replay_verified', 'mode': self.mode,
                   'project_sha256': fingerprint, 'uses_behavior_skill': bool(skill),
                   'episodes': [{'scenario': r['scenario'], 'video': r['video'],
                                 'trace': r['trace']} for r in results]})
        return results

    def candidate(self, project, episodes, output, *, diagnosis, changes, skill=''):
        return self.play(project, [r['initialization_task'] for r in episodes], output,
                         unit=episodes[0]['initialization_unit'], prior=episodes,
                         diagnosis=diagnosis, changes=changes, skill=skill)

    def remember(self, project, unit, episodes):
        fingerprint = digest(project)
        tasks = unit.get('members', [unit])
        if (len(episodes) != len(tasks) or any(
                not r.get('replay_verified') or r.get('project_sha256') != fingerprint
                or r.get('scenario') != task['id'] or r.get('initialization_unit') != unit
                for r, task in zip(episodes, tasks))):
            raise ValueError('Cannot inherit mismatched interactive Player evidence')
        self.latest = copy.deepcopy(episodes)
        self.selected[unit['id']] = copy.deepcopy({
            'project': str(project), 'project_sha256': fingerprint,
            'unit': unit, 'episodes': episodes})

    def save(self, output):
        write_json(output, {'mode': self.mode, 'groups': self.selected,
                   'latest': self.latest,
                   'scope': 'Selected version-owned interactive playtests; earlier group evidence retains its original version.'})
