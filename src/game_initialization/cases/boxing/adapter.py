from pathlib import Path
from gamecraft_behavior.adapters import GameAdapter


class BoxingAdapter(GameAdapter):
    def __init__(self):
        super().__init__('boxing-video-lab',
            'You control the left fighter. Choose jab (J), block (K), or wait. A jab takes 0.8s and hits at 0.4s for 10 HP. '
            'Block lasts 0.6s and prevents an incoming hit during that window. Jab costs 12 stamina, block costs 6; stamina regenerates. '
            'Actions during your active animation/recovery are ignored; wait advances time. Read opponent attack tells, block, then counter. '
            'The bout lasts at most 12s. Win by reducing opponent HP to zero or ending with more HP. Do not change game code.',
            {'jab': 'J', 'block': 'K', 'wait': None}, Path(__file__).with_name('bridge.gd'),
            {k:k for k in ('player_hp','opponent_hp','player_stamina','opponent_stamina','player_action','opponent_action')},
            'Left is the player, right is the opponent. HP/stamina range from 0 to 100. Current poses are observed; future opponent actions are not exposed.',
            ('player_hp','opponent_hp','player_stamina','opponent_stamina','player_action','opponent_action','player_age','opponent_age','ticks','terminal','outcome'),
            numeric_fields=('player_stamina','opponent_stamina'), default_scenario='bout_seed_100')

    def validate_scenario(self, scenario):
        if not isinstance(scenario,str) or not scenario.startswith('bout_seed_') or not scenario[10:].isdigit():
            raise ValueError('Expected bout_seed_<integer>')

    def seed(self, project, scenario):
        return int(scenario[10:])

    def improved(self, before, after, minimum_gain=.25):
        def score(s):
            return ({'loss':0,'playing':1,'draw':1,'win':2}[s['outcome']], s['player_hp']-s['opponent_hp'])
        return score(after)>score(before)


def create_adapter():
    return BoxingAdapter()
