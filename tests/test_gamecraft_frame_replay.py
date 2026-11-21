import unittest
from gamecraft_behavior.frame_replay import equivalent, validate_trace


class FrameReplayTests(unittest.TestCase):
    def test_state_and_physics_must_match(self):
        state = {'state': 'dead', 'score': 0, 'survival_seconds': 3.8,
                 'bird_y': 321.6666666667, 'velocity': -150.0}
        self.assertTrue(equivalent(state, dict(state)))
        for key, value in [('state', 'playing'), ('score', 1), ('survival_seconds', 3.8 + 1/60),
                           ('bird_y', 322), ('velocity', -149)]:
            self.assertFalse(equivalent(state, {**state, key: value}), key)

    def test_only_unambiguous_bounded_input_traces(self):
        event = {'frame': 0, 'type': 'key_press', 'keycode': 'SPACE'}
        validate_trace({'duration_frames': 12, 'events': [event]})
        for events in [[event, event], [{**event, 'frame': -1}], [{**event, 'frame': 12}],
                       [{**event, 'keycode': 'ENTER'}], [{**event, 'type': 'key_down'}]]:
            with self.assertRaises(ValueError):
                validate_trace({'duration_frames': 12, 'events': events})
        with self.assertRaises(ValueError):
            validate_trace({'duration_frames': 601, 'events': []})


if __name__ == '__main__':
    unittest.main()
