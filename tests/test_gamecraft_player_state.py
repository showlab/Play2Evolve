import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gamecraft_behavior.runtime import player
from gamecraft_training.adapter import Game


class PlayerStateTests(unittest.TestCase):
    def test_engine_state_keeps_images_and_old_reasons(self):
        fake = SimpleNamespace(img=lambda path: {'image': path},
                               call=Mock(return_value={'action': 'wait', 'reason': 'rising'}))
        recent = [{'frame': i*6, 'action': 'tap', 'reason': 'old downward claim',
                   'image': f'{i}.png'} for i in range(4)]
        state = {'screen_height': 720, 'bird_center_y': 166.667,
                 'velocity_y_pixels_per_second': -150}
        with patch.dict(sys.modules, llm=fake):
            player()('now.png', '', recent, 6, current_state=state)
        content = fake.call.call_args.args[2]
        self.assertEqual([c['image'] for c in content if 'image' in c],
                         ['2.png', '3.png', 'now.png'])
        self.assertEqual(json.loads(content[0]['text'])['recent_actions'][0]['reason'],
                         'old downward claim')
        self.assertIn(json.dumps(state), content[1]['text'])
        self.assertIn('Negative velocity_y means upward', content[1]['text'])
        self.assertNotIn('You see only', fake.call.call_args.args[1])

    def test_pixels_mode_does_not_supply_state(self):
        fake = SimpleNamespace(img=lambda p: {'image': p}, call=Mock())
        with patch.dict(sys.modules, llm=fake):
            player()('now.png', '', [], 6)
        content = fake.call.call_args.args[2]
        self.assertFalse(any('engine values' in c.get('text', '') for c in content))
        self.assertIn('You see only', fake.call.call_args.args[1])

    def test_codex_player_uses_selected_model_without_changing_judge(self):
        fake = SimpleNamespace(img=lambda p: {'image': p},
                               call=Mock(return_value={'action': 'wait', 'reason': 'safe'}))
        with patch.dict(sys.modules, llm=fake):
            player(model='gpt-6-luna', backend='codex')('now.png', '', [], 6)
        self.assertEqual(fake.call.call_args.args[0], 'gpt-6-luna')
        self.assertEqual(fake.call.call_args.kwargs['backend'], 'codex')

    def test_optimizer_receives_observed_state_without_rewriting_reason(self):
        d = {'frame': 6, 'action': 'wait', 'reason': 'rising',
             'image': 'now.png', 'player_state': {'bird_center_y': 311.667,
                                                'velocity_y_pixels_per_second': -150}}
        result = json.loads(Game.trajectory({'replay': {}, 'decisions': [d]}))
        self.assertEqual(result['decisions'][0]['player_state'], d['player_state'])
        self.assertEqual(result['decisions'][0]['reason'], d['reason'])
