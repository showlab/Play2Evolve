import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from gamecraft_behavior.adapters import resolve
from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter
from gamecraft_behavior.core import HeldKeyState, action, trace, prepare, gain, train
from gamecraft_behavior.frame_replay import validate_trace, equivalent
from gamecraft_behavior.runtime import player, rollout
from gamecraft_behavior.adapters.base import GameAdapter

sys.path.insert(0, str(Path(__file__).parent / 'fixtures'))
from counter_adapter import CounterAdapter


class AdapterTests(unittest.TestCase):
    def test_pointer_actions_are_bounded_and_replayable_without_changing_key_contract(self):
        a = GameAdapter(task_id='pointer', instructions='Place pieces',
                        actions={'wait': None, 'confirm': 'ENTER'},
                        bridge_script=Path(__file__).parent / 'fixtures/counter_adapter.gd',
                        observation_fields={'points': 'points'}, state_description='points',
                        replay_fields=('points',), pointer_actions=('click', 'drag'))
        self.assertEqual(CounterAdapter().action_schema['required'], ['reason', 'action'])
        pointer = {'x': .2, 'y': .3, 'to_x': .8, 'to_y': .7}
        click = a.validate_action({'action': 'click', 'pointer': pointer, 'reason': 'Select'})
        self.assertEqual(a.events(click)[0]['type'], 'mouse_click')
        self.assertEqual(a.events({'action': 'drag', 'pointer': pointer, 'reason': 'Place'})[0]['type'], 'mouse_drag')
        self.assertEqual(a.scripted_action({'action': 'click', 'pointer': pointer})['action'], 'click')
        self.assertEqual(a.scripted_action('confirm')['action'], 'confirm')
        data = trace([{'frame': 0, **click}], 6, a)
        validate_trace(data, a)
        self.assertNotIn('pointer', a.validate_action({'action': 'confirm', 'pointer': pointer, 'reason': ''}))
        for bad in ({'action': 'drag', 'reason': ''},
                    {'action': 'click', 'pointer': {**pointer, 'x': 1.1}, 'reason': ''}):
            with self.assertRaises(ValueError):
                a.validate_action(bad)
        with self.assertRaises(ValueError):
            validate_trace(data, CounterAdapter())

    def test_held_input_actions_share_the_same_replay_contract(self):
        a = GameAdapter(task_id='held', instructions='Move a piece',
                        actions={'wait': None, 'confirm': 'ENTER'},
                        bridge_script=Path(__file__).parent / 'fixtures/counter_adapter.gd',
                        observation_fields={'points': 'points'}, state_description='points',
                        replay_fields=('points',), pointer_actions=('down', 'move', 'up'),
                        key_actions={'start_right': ('key_down', 'RIGHT'),
                                     'stop_right': ('key_up', 'RIGHT')})
        pointer = {'x': .2, 'y': .3, 'to_x': .2, 'to_y': .3}
        decisions = [
            {'frame': 0, 'action': 'start_right', 'reason': ''},
            {'frame': 6, 'action': 'stop_right', 'reason': ''},
            {'frame': 12, 'action': 'down', 'pointer': pointer, 'reason': ''},
            {'frame': 18, 'action': 'move', 'pointer': {**pointer, 'x': .8, 'to_x': .8}, 'reason': ''},
            {'frame': 24, 'action': 'up', 'pointer': {**pointer, 'x': .8, 'to_x': .8}, 'reason': ''},
        ]
        data = trace(decisions, 30, a)
        self.assertEqual([e['type'] for e in data['events']],
                         ['key_down', 'key_up', 'mouse_down', 'mouse_move', 'mouse_up'])
        validate_trace(data, a)
        with self.assertRaises(ValueError):
            validate_trace(data, CounterAdapter())

    def test_repeated_held_requests_replay_only_the_events_sent_to_godot(self):
        adapter = GameAdapter(task_id='held', instructions='Move',
            actions={'wait': None, 'confirm': 'ENTER'},
            bridge_script=Path(__file__).parent / 'fixtures/counter_adapter.gd',
            observation_fields={'points': 'points'}, state_description='points',
            replay_fields=('points',),
            key_actions={'right_down': ('key_down', 'RIGHT'),
                         'right_up': ('key_up', 'RIGHT'),
                         'left_down': ('key_down', 'LEFT'),
                         'left_up': ('key_up', 'LEFT')})
        state = HeldKeyState()
        names = ['right_up', 'right_down', 'right_down', 'confirm', 'wait',
                 'right_up', 'right_up', 'right_down', 'left_down',
                 'right_up', 'left_up']
        decisions = []
        for frame, name in enumerate(names):
            decision = {'frame': frame * 6, 'action': name, 'reason': ''}
            sent, issues = state.dispatch(adapter.events(decision))
            decision['emitted_events'] = sent
            if issues:
                decision['input_issues'] = issues
            decisions.append(decision)
        data = trace(decisions, 66, adapter)
        self.assertEqual([(e['frame'], e['type'], e['keycode']) for e in data['events']],
                         [(6, 'key_down', 'RIGHT'), (18, 'key_press', 'ENTER'),
                          (30, 'key_up', 'RIGHT'), (42, 'key_down', 'RIGHT'),
                          (48, 'key_down', 'LEFT'), (54, 'key_up', 'RIGHT'),
                          (60, 'key_up', 'LEFT')])
        self.assertEqual([d['input_issues'][0]['type'] for d in decisions if 'input_issues' in d],
                         ['key_up_without_down', 'key_up_without_down'])
        validate_trace(data, adapter)
        fresh = HeldKeyState()
        self.assertEqual(fresh.dispatch(adapter.events(decisions[1]))[0],
                         [{'type': 'key_down', 'keycode': 'RIGHT'}])

    def test_rollout_records_the_same_held_events_it_dispatches(self):
        adapter = CounterAdapter()
        adapter.key_actions = {'right_down': ('key_down', 'RIGHT'),
                               'right_up': ('key_up', 'RIGHT')}
        actions = iter(['right_down', 'right_down', 'jab', 'right_up', 'right_up'])
        sent = []
        observation = {'points': 0, 'ticks': 0, 'terminal': False, 'image': 'frame.png'}

        def observe(path, predicate, proc=None, timeout=30):
            command = Path(path).parent / 'command.json'
            if not command.exists():
                return {'id': -1, **observation}
            value = json.loads(command.read_text())
            sent.append(value['events'])
            return {'id': value['id'], **observation}

        def verify(project, trace_path, output, godot, *, adapter, max_frames=None):
            data = json.loads(Path(trace_path).read_text())
            validate_trace(data, adapter)
            self.assertEqual(data['events'], [
                {'frame': frame * 6, **event}
                for frame, events in enumerate(sent) for event in events])
            return adapter.measured(observation), str(Path(output) / 'replay.mp4')

        class Process:
            def poll(self):
                return None

        verifier = ModuleType('gamecraft_bench.verifier')
        verifier.replay = SimpleNamespace(_start_xvfb=lambda *_args, **_kwargs: (Process(), 77),
                                          _stop=lambda *_args: None)
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(sys.modules, {'gamecraft_bench': ModuleType('gamecraft_bench'),
                                         'gamecraft_bench.verifier': verifier}), \
                patch('gamecraft_behavior.runtime.prepare', return_value=Path(__file__).parent / 'fixtures/counter_game'), \
                patch('gamecraft_behavior.runtime.subprocess.run'), \
                patch('gamecraft_behavior.runtime.subprocess.Popen', return_value=Process()), \
                patch('gamecraft_behavior.runtime.wait_json', side_effect=observe), \
                patch('gamecraft_behavior.frame_replay.replay', side_effect=verify):
            result = rollout(Path(__file__).parent / 'fixtures/counter_game',
                Path(td) / 'episode', '',
                lambda *_args, **_kwargs: {'action': next(actions), 'reason': ''},
                godot='unused', adapter=adapter, max_decisions=5)
        self.assertTrue(result['replay_verified'])
        self.assertEqual(sent, [
            [{'type': 'key_down', 'keycode': 'RIGHT'}], [],
            [{'type': 'key_press', 'keycode': 'J'}],
            [{'type': 'key_up', 'keycode': 'RIGHT'}], []])
        self.assertEqual(result['decisions'][-1]['input_issues'],
                         [{'type': 'key_up_without_down', 'keycode': 'RIGHT'}])

    def test_prompt_schema_state_and_history_follow_selected_game(self):
        adapter = CounterAdapter()
        fake = SimpleNamespace(img=lambda p: {'image': str(p)}, call=Mock())
        observation = adapter.observation({'points': 2, 'private_opponent_plan': 'secret'})
        with patch.dict(sys.modules, llm=fake):
            player(adapter=adapter)('now.png', 'Preserve stamina',
                [{'frame': 0, 'action': 'jab', 'reason': 'opening', 'image': 'old.png'}],
                6, current_state=observation)
        args = fake.call.call_args.args
        self.assertEqual(args[4]['properties']['action']['enum'], ['jab', 'block', 'wait'])
        self.assertIn('Preserve stamina', args[1])
        self.assertNotIn('Dread', args[1])
        encoded = json.dumps(args[2])
        self.assertIn('current_points', encoded)
        self.assertIn('opening', encoded)
        self.assertNotIn('secret', encoded)
        self.assertNotIn('bird', encoded)

    def test_action_execution_and_replay_share_mapping(self):
        a = CounterAdapter()
        decisions = [{'frame': i*6, 'action': v, 'reason': ''}
                     for i, v in enumerate(['jab', 'block', 'wait'])]
        data = trace(decisions, 18, a)
        self.assertEqual([v['keycode'] for v in data['events']], ['J', 'L'])
        self.assertEqual(data['scenario'], 'counter')
        validate_trace(data, a)
        with self.assertRaises(ValueError): validate_trace(data, DreadWingsAdapter())
        for bad in ['tap', 'shell', [], None]:
            with self.assertRaises(ValueError): action({'action': bad, 'reason': ''}, a)
        self.assertFalse(equivalent({'points': 2, 'ticks': 1, 'terminal': False},
                                    {'points': 2, 'ticks': 2, 'terminal': False}, a))

    def test_prepare_installs_task_bridge_without_touching_source(self):
        fixture = Path(__file__).parent / 'fixtures/counter_game'
        with tempfile.TemporaryDirectory() as td:
            out = prepare(fixture, Path(td)/'runtime', CounterAdapter())
            self.assertIn('game.points', (out/'behavior_game_adapter.gd').read_text())
            self.assertNotIn('bird_y', (out/'behavior_bridge.gd').read_text())
            with self.assertRaises(ValueError): resolve(fixture)
            with self.assertRaises(ValueError): resolve(fixture, DreadWingsAdapter())
            with self.assertRaises(ValueError):
                rollout(fixture, Path(td)/'bad', '', player(), godot='unused', adapter=CounterAdapter())

    def test_task_specific_objective_used_in_both_training_gates(self):
        def result(points):
            return {'replay_verified': True, 'replay': {'points': points}, 'decisions': []}
        adapter = CounterAdapter()
        self.assertTrue(gain(result(0), result(1), adapter=adapter))
        values = iter([result(0), result(2), result(0), result(2)])
        with tempfile.TemporaryDirectory() as td:
            r = train('', Path(td)/'train', lambda *_: next(values),
                      lambda *_: {'skill': 'new rule', 'reason': 'better'}, adapter=adapter)
        self.assertTrue(r['history'][0]['accepted'])
