import json
from pathlib import Path
from .base import GameAdapter

SYSTEM = '''You play Dread Wings, a one-button endless flyer. You see only the current game screenshot and your recent actions.
The bird moves right through scrolling columns. SPACE gives an upward impulse; gravity pulls it down.
Pass through gaps, avoid the ceiling, floor and columns. Choose tap (one SPACE press) or wait.
The game is PAUSED while you think. Your action is followed by the indicated amount of game time.
Infer position and vertical motion by comparing screenshots; time your flap to the next gap.
Never change code, assets, difficulty or scoring. Respond with a brief reason and then action.
SKILL:\n'''

class DreadWingsAdapter(GameAdapter):
    def __init__(self):
        super().__init__(
            task_id='platformer-dread-wings', instructions=SYSTEM,
            actions={'tap': 'SPACE', 'wait': None},
            bridge_script=Path(__file__).with_suffix('.gd'),
            observation_fields={'bird_center_y': 'bird_y', 'velocity_y_pixels_per_second': 'velocity'},
            state_description='Screen y=0 at top, increases downward. Negative velocity_y means upward motion; positive means downward motion.',
            replay_fields=('state', 'score', 'survival_seconds', 'bird_y', 'velocity'),
            numeric_fields=('survival_seconds', 'bird_y', 'velocity'))

    def system(self, engine_state=False):
        if engine_state:
            return SYSTEM.replace('You see only the current game screenshot and your recent actions.',
                                  'You see game screenshots, recent actions, and the current engine position and velocity.')
        return SYSTEM

    def observation(self, status):
        return {'screen_height': self.viewport[1], **super().observation(status)}

    def policy_observation(self, status):
        # These fields describe visible play state, not the hidden pipe seed.
        result = {**self.observation(status), 'phase': status['state'], 'score': status['score']}
        for source, name in (('first_pipe_x', 'next_pipe_x'),
                             ('first_gap_center', 'next_gap_center_y'),
                             ('speed', 'pipe_speed')):
            if source in status:
                result[name] = status[source]
        return result

    def policy_state_fields(self):
        return super().policy_state_fields() | {
            'screen_height', 'phase', 'score', 'next_pipe_x', 'next_gap_center_y', 'pipe_speed'}

    def validate_scenario(self, scenario):
        if scenario != 'ready' and not (isinstance(scenario, str) and scenario.startswith('ready_seed_') and scenario[11:].isdigit()):
            raise ValueError('Expected ready or ready_seed_<integer> scenario')

    def seed(self, project, scenario):
        return int(scenario[11:]) if scenario.startswith('ready_seed_') else json.loads((Path(project) / 'game_plan.json').read_text())['seed']

    def terminal(self, status):
        return status['state'] == 'dead'

    def improved(self, before, after, minimum_gain=.25):
        return (after['score'] > before['score'] or
                (after['score'] == before['score'] and
                 after['survival_seconds'] >= before['survival_seconds'] + minimum_gain))
