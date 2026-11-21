from pathlib import Path
from gamecraft_behavior.adapters import GameAdapter


class CounterAdapter(GameAdapter):
    def __init__(self):
        super().__init__('test-counter', 'Choose jab (+2 points), block (-1 point), or wait. Reach 3 points.',
                         {'jab': 'J', 'block': 'L', 'wait': None},
                         Path(__file__).with_suffix('.gd'), {'current_points': 'points'},
                         'Larger points are better.', ('points', 'ticks', 'terminal'),
                         default_scenario='counter')

    def improved(self, before, after, minimum_gain=.25):
        return after['points'] > before['points']
