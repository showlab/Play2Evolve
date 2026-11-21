"""The old game protocol, backed by the Godot screenshot/replay adapter."""
import json
import hashlib
import shutil
from pathlib import Path

from gamecraft_behavior.core import write_json
from gamecraft_behavior.runtime import rollout


class Game:
    DESC = 'plays Dread Wings using screenshots and tap/wait actions'
    EXTRA = ('Use visual decision rules only. Do not change mechanics or assets. '
             'These are repeated episodes of one fixed scenario, not independent seeds.')
    # Names denote loop phases, NOT held-out benchmark splits.
    TRAIN, VALID, TEST, CONFIRM = ['train'], ['valid'], ['report'], ['confirm']
    DEMO = 'demo'

    def __init__(self, project, decide, godot, step_frames=6, max_decisions=60, player_state='pixels'):
        self.player_state = player_state
        self.project = Path(project).resolve()
        self.decide, self.godot = decide, godot
        self.step_frames, self.max_decisions = step_frames, max_decisions

    @staticmethod
    def task_name(task):
        return f'{task[0]}_s{task[1]}' if isinstance(task, tuple) else task

    def configure_splits(self, train=8, valid=12, confirm=12, test=24):
        from .seeds import splits
        groups = splits(train, valid, confirm, test)
        self.TRAIN, self.VALID = groups['train'], groups['valid']
        self.CONFIRM, self.TEST = groups['confirm'], groups['test']
        self.DEMO = ('demo', 9000)
        self.EXTRA = ('Use general rules based on screenshots and current engine position/velocity; '
                      if self.player_state == 'engine' else 'Use general visual rules; ') + 'never mention seeds or hardcode action sequences.'
        return groups

    @staticmethod
    def rules():
        return ('Observe the current and two previous screenshots. SPACE/tap gives an upward impulse; '
                'wait allows gravity to act. Avoid floor, ceiling and columns; pass through gaps. '
                'The scene is paused during inference. Reasons are player interpretations, not ground truth.')

    def run(self, task, skill, out, video=False):
        import llm
        before = dict(llm.USAGE)
        scenario = f'ready_seed_{task[1]}' if isinstance(task, tuple) else 'ready'
        result = rollout(self.project, out, skill, self.decide, godot=self.godot,
                         step_frames=self.step_frames, max_decisions=self.max_decisions, scenario=scenario,
                         player_state=self.player_state)
        if not result['replay_verified']:
            raise RuntimeError(f'Independent physics-frame replay disagrees with rollout: {out}')
        result['usage'] = {k: llm.USAGE[k] - before[k] for k in before}
        result['phase'] = task
        write_json(Path(out) / 'result.json', result)
        if video:
            shutil.copyfile(result['video'], Path(out) / 'game.mp4')
        return result

    @staticmethod
    def score(results):
        if not results or any(not r['replay_verified'] for r in results):
            raise ValueError('Scoring requires verified replays')
        # No mixing of different project versions or time budgets in one batch.
        keys = ('project_sha256', 'decision_frames')
        if any(any(r[k] != results[0][k] for k in keys) for r in results):
            raise ValueError('Episodes have incompatible worlds or scenarios')
        points = sum(r['replay']['score'] for r in results) / len(results)
        survival = sum(r['replay']['survival_seconds'] for r in results) / len(results)
        context = {k: results[0][k] for k in keys}
        scenarios = {r['scenario'].split('_seed_')[0] for r in results}
        if len(scenarios) != 1:
            raise ValueError('Episodes have incompatible scenario families')
        context.update(scenario=scenarios.pop(), seed=sorted(r['seed'] for r in results))
        context['project_sha256'] = hashlib.sha256(json.dumps(context['project_sha256'], sort_keys=True).encode()).hexdigest()
        return {'soft': points * 1000 + survival, 'points': points, 'survival_seconds': survival,
                **context}

    @staticmethod
    def better(new, current):
        if any(new[k] != current[k] for k in ('project_sha256', 'scenario', 'seed', 'decision_frames')):
            raise ValueError('Remeasure behavior after a world change')
        return (new['points'] > current['points'] or
                (new['points'] == current['points'] and
                 new['survival_seconds'] >= current['survival_seconds'] + .25))

    @staticmethod
    def worst(results):
        return sorted(results, key=lambda r: (r['replay']['score'], r['replay']['survival_seconds']))[:3]

    @staticmethod
    def trajectory(result):
        return json.dumps({'outcome': result['replay'], 'decisions': [
            {k: d[k] for k in ('frame', 'action', 'reason', 'player_state') if k in d} for d in result['decisions']]})
