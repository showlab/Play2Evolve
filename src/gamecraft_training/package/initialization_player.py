"""Plan initialization playtest groups and retain version-owned reactive policies."""
import copy
import hashlib
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.program_policy import program_player
from .scenario_groups import build
from .version_player import play_version, profiles_for


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan_groups(adapter, project, tasks, requirements, *, count, backend, model):
    from .models import call
    from gamecraft_composition.core import sources
    return call(
        'Plan complementary Player profiles from public game requirements and implemented code. '
        'Return exactly group_count profiles, each with a stable observable goal and ONE launch. '
        'scenario=null means normal game entry (title/menu); prefer it for core flow and navigation. '
        'Use an existing scenario ID only to reach a targeted later-game condition efficiently. '
        'Multiple profiles may start at the same normal entry with different strategies. '
        'Do not partition or exhaustively assign old fixed traces. They are launch/intent references. '
        'Cover the important gameplay and visible interactions across profiles; do not invent '
        'scenario IDs, bypass rules or modify saves. All profiles share ONE policy per game version.',
        {'rules': adapter.instructions, 'requirements': requirements, 'scenarios': tasks,
         'game_code': sources(project), 'group_count': count}, [], 'initialization_player_groups',
        {'groups': {'type': 'array', 'minItems': count, 'maxItems': count, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['id', 'goal', 'scenario'], 'properties': {
                'id': {'type': 'string'}, 'goal': {'type': 'string'},
                'scenario': {'type': ['string', 'null'],
                             'enum': [None] + [t['id'] for t in tasks]}}}}},
        tokens=3000, backend=backend, model=model)


def validate_groups(proposal, tasks, count):
    rows = proposal.get('groups') if isinstance(proposal, dict) else None
    if isinstance(rows, list) and rows and all(isinstance(row, dict) and 'scenario' in row for row in rows):
        if len(rows) != count or not tasks:
            raise ValueError('Player must plan the configured number of profiles')
        names = set()
        by_id = {task['id']: task for task in tasks}
        normal = next((task for task in tasks if task.get('launch') == 'default'), tasks[0])
        units = []
        for row in rows:
            if (set(row) != {'id', 'goal', 'scenario'} or not isinstance(row['id'], str)
                    or not row['id'].strip() or row['id'] in names
                    or not isinstance(row['goal'], str) or not row['goal'].strip()
                    or (row['scenario'] is not None and
                        (not isinstance(row['scenario'], str) or row['scenario'] not in by_id))):
                raise ValueError('Invalid Player profile or launch scenario')
            names.add(row['id'])
            task = copy.deepcopy(normal if row['scenario'] is None else by_id[row['scenario']])
            task.update(profile=row['id'], goal=row['goal'],
                        launch='default' if row['scenario'] is None else 'scenario')
            units.append({'id': row['id'], 'goal': row['goal'],
                          'scenario_ids': [task['id']], 'members': [task]})
        return units
    # Read legacy explicit grouping plans, but new plans use one launch/profile.
    if not isinstance(rows, list) or len(rows) != min(count, len(tasks)):
        raise ValueError('Player must plan the configured number of initialization groups')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'id', 'goal', 'scenarios'}
                or not isinstance(row['goal'], str) or not row['goal'].strip()):
            raise ValueError('Each Player group needs an observable goal')
    units = build(tasks, [{k: row[k] for k in ('id', 'scenarios')} for row in rows])
    for unit, row in zip(units, rows):
        unit['goal'] = row['goal']
    return units


class InitializationPlayer:
    def __init__(self, *, adapter, rollout, godot, model, backend, step_frames,
                 max_decisions, policy_factory=None, behavior_skill_enabled=False):
        self.adapter, self.rollout, self.godot = adapter, rollout, godot
        self.model, self.backend = model, backend
        self.step_frames, self.max_decisions = step_frames, max_decisions
        self.factory = policy_factory or program_player
        self.behavior_skill_enabled = behavior_skill_enabled
        self.selected = {}
        self.latest = []
        self.units = []

    def configure(self, units):
        self.units = copy.deepcopy(units)

    def record_bootstrap(self, project, output, *, skill=''):
        """Author and record each group once before any optimization, without selecting it."""
        recordings = {}
        for index, unit in enumerate(self.units, 1):
            episodes = self.play(project, unit['members'], Path(output) / f'group-{index:03}',
                                 unit=unit, skill=skill)
            recordings[unit['id']] = episodes
        return recordings

    def record_final(self, project, output, *, skill=''):
        """Reauthor every group policy against the fully optimized game."""
        recordings = {}
        for index, unit in enumerate(self.units, 1):
            prior = self.selected.get(unit['id'], {}).get('episodes', [])
            recordings[unit['id']] = self.play(
                project, unit['members'], Path(output) / f'group-{index:03}',
                unit=unit, prior=prior, fresh=True, skill=skill)
        return recordings

    def reusable(self, project, unit):
        entry = self.selected.get(unit['id'])
        if not entry or entry['project_sha256'] != digest(project):
            return None
        if entry['unit'] != unit:
            return None
        for episode in entry['episodes']:
            for key in ('player_policy', 'trace', 'video'):
                path = Path(episode[key])
                if not path.is_file() or file_hash(path) != episode['playtest_files'][key]:
                    return None
            if any(not Path(d.get('image', '')).is_file() for d in episode['decisions']):
                return None
        return copy.deepcopy(entry['episodes'])

    def play(self, project, tasks, output, *, unit, prior=(), diagnosis=None, changes=None,
             fresh=False, skill=''):
        profiles = []
        for item in self.units or [unit]:
            profiles.extend(profiles_for(item['members'], group=item['id'], goal=item['goal']))
        tasks = [{**task, 'profile': task.get('profile', f'{unit["id"]}/{task["id"]}')} for task in tasks]
        results = play_version(project, tasks, output, profiles=profiles,
            prior=prior or self.latest, skill=skill,
            reuse=not fresh and diagnosis is None and changes is None,
            context={'phase': 'initialization', 'group': unit['id'], 'goal': unit['goal'],
                     'diagnosis': diagnosis, 'candidate_changes': changes,
                     'requirements': unit.get('requirements', []),
                     'behavior_skill_enabled': self.behavior_skill_enabled},
            adapter=self.adapter, rollout=self.rollout, godot=self.godot,
            model=self.model, backend=self.backend, step_frames=self.step_frames,
            max_decisions=self.max_decisions, factory=self.factory)
        for index, (result, task) in enumerate(zip(results, tasks)):
            result['initialization_task'] = copy.deepcopy(task)
            result['initialization_unit'] = copy.deepcopy(unit)
            write_json(Path(output) / f'episode-{index:03}' / 'result.json', result)
        return results

    def candidate(self, project, episodes, output, *, diagnosis, changes, skill=''):
        return self.play(project, [r['initialization_task'] for r in episodes], output,
                         unit=episodes[0]['initialization_unit'], prior=episodes,
                         diagnosis=diagnosis, changes=changes, skill=skill)

    def remember(self, project, unit, episodes):
        fingerprint = digest(project)
        if (len(episodes) != len(unit['members']) or any(
                not r.get('replay_verified') or r.get('project_sha256') != fingerprint
                or r.get('scenario') != task['id'] or r.get('initialization_unit') != unit
                for r, task in zip(episodes, unit['members']))):
            raise ValueError('Cannot inherit mismatched initialization Player evidence')
        self.latest = copy.deepcopy(episodes)
        self.selected[unit['id']] = copy.deepcopy({
            'project': str(project), 'project_sha256': fingerprint,
            'unit': unit, 'episodes': episodes})

    def save(self, output):
        write_json(output, {'mode': 'program', 'groups': self.selected,
                   'latest': self.latest,
                   'scope': 'One program per selected version, covering all profiles; earlier group evidence retains its original version.'})
