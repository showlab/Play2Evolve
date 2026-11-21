"""Task contract for the shared screenshot/decision/Godot execution loop.

Only explicit observation fields reach the model. Replay telemetry can include
additional private fields; it is never automatically exposed to the player.
"""
from dataclasses import dataclass
from pathlib import Path
import math


@dataclass
class GameAdapter:
    task_id: str
    instructions: str
    actions: dict[str, str | None]  # semantic action -> one key press; None = wait
    bridge_script: Path  # RefCounted: status(game), terminal(status), execute_event(game,event)
    observation_fields: dict[str, str]  # public label -> status key
    state_description: str
    replay_fields: tuple[str, ...]
    numeric_fields: tuple[str, ...] = ()
    viewport: tuple[int, int] = (1280, 720)
    default_scenario: str = 'ready'
    pointer_actions: tuple[str, ...] = ()  # opt-in normalized screen click/drag
    pointer_replaces: tuple[str, ...] = ()  # fixed-target keys hidden from interactive Player
    key_actions: dict[str, tuple[str, str]] | None = None  # opt-in held-key down/up IDs

    def __post_init__(self):
        if not self.task_id or (not self.actions and not self.pointer_actions and not self.key_actions) or not self.replay_fields:
            raise ValueError('Adapter requires task identity, actions and replay fields')
        allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') | {
            'SPACE', 'ENTER', 'ESCAPE', 'TAB', 'BACKSPACE', 'DELETE',
            'SHIFT', 'CTRL', 'ALT', 'UP', 'DOWN', 'LEFT', 'RIGHT'}
        if any(not isinstance(a, str) or not a or (k is not None and k not in allowed)
               for a, k in self.actions.items()):
            raise ValueError('Actions must map to a supported single key press or None')
        if not set(self.numeric_fields) <= set(self.replay_fields):
            raise ValueError('Numeric replay fields must be included in replay_fields')
        if (not isinstance(self.pointer_actions, tuple) or
                not set(self.pointer_actions) <= {'click', 'drag', 'move', 'down', 'up'} or
                len(set(self.pointer_actions)) != len(self.pointer_actions) or
                set(self.pointer_actions) & set(self.actions)):
            raise ValueError('Pointer actions must be unique pointer IDs separate from key actions')
        self.key_actions = self.key_actions or {}
        if (set(self.key_actions) & (set(self.actions) | set(self.pointer_actions)) or
                any(not isinstance(spec, tuple) or len(spec) != 2 or
                    spec[0] not in ('key_down', 'key_up') or spec[1] not in allowed
                    for spec in self.key_actions.values())):
            raise ValueError('Held-key actions need unique IDs and supported down/up keycodes')
        if (not isinstance(self.pointer_replaces, tuple) or
                len(set(self.pointer_replaces)) != len(self.pointer_replaces) or
                not set(self.pointer_replaces) <= set(self.actions) or
                (self.pointer_replaces and not self.pointer_actions)):
            raise ValueError('Pointer replacements must name existing key actions in a pointer-enabled adapter')

    @property
    def allowed_actions(self):
        return list(self.actions) + list(self.key_actions) + list(self.pointer_actions)

    @property
    def action_schema(self):
        schema = {'type': 'object', 'additionalProperties': False, 'required': ['reason', 'action'],
                  'properties': {'reason': {'type': 'string'},
                                 'action': {'type': 'string', 'enum': self.allowed_actions}}}
        if self.pointer_actions:
            coordinate = {'type': 'number', 'minimum': 0, 'maximum': 1}
            schema['properties']['pointer'] = {'anyOf': [
                {'type': 'null'},
                {'type': 'object', 'additionalProperties': False,
                 'required': ['x', 'y', 'to_x', 'to_y'],
                 'properties': {k: coordinate for k in ('x', 'y', 'to_x', 'to_y')}}]}
            schema['required'].append('pointer')
        return schema

    def system(self, engine_state=False):
        names = ', '.join(self.allowed_actions)
        pointer = (' For pointer actions supply normalized x,y,to_x,to_y coordinates '
                   'in [0,1] relative to the visible game screen. Only drag uses a different destination; '
                   'for click, move, down and up set to_x=x and to_y=y. Down/move/up can hold a drag across decisions. '
                   'When the target position matters, prefer a pointer action over a shortcut with a fixed target. '
                   'For keyboard or wait actions set pointer=null. Use the actual visible target, '
                   'not a fixed shortcut for a different cell.' if self.pointer_actions else '')
        return (self.instructions + '\nThe game is paused while you think. Choose only an allowed semantic action ID. '
                f'The action field must be exactly one of: {names}. '
                + pointer + ' '
                'Never put a keyboard key or button letter in the action field. If a learned skill mentions a '
                'physical key, follow its stated meaning and translate it to the corresponding semantic action ID; '
                'do not guess from the key letter.\nSKILL:\n')

    def observation(self, status):
        return {label: status[key] for label, key in self.observation_fields.items()}

    def policy_observation(self, status):
        """Structured state exposed to generated policies; adapters may extend it."""
        return self.observation(status)

    def policy_state_fields(self):
        """Names a policy may read, including fields added by policy_observation."""
        return set(self.observation_fields)

    def validate_action(self, value):
        if (not isinstance(value, dict) or not isinstance(value.get('action'), str) or
                value['action'] not in self.allowed_actions or not isinstance(value.get('reason'), str)):
            raise ValueError(f'Player must return action in {self.allowed_actions} and a string reason')
        result = {'action': value['action'], 'reason': value['reason'][:1000]}
        if value['action'] in self.pointer_actions:
            pointer = value.get('pointer')
            if not isinstance(pointer, dict) or set(pointer) != {'x', 'y', 'to_x', 'to_y'}:
                raise ValueError('Pointer action needs x,y,to_x,to_y')
            for k, v in pointer.items():
                if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1:
                    raise ValueError(f'Pointer {k} must be a finite normalized coordinate')
            result['pointer'] = dict(pointer)
        # Some structured-output backends populate every schema field even for
        # key actions. Ignore the unused pointer; execution is defined by action.
        return result

    def events(self, value):
        value = self.validate_action(value)
        if value['action'] in self.pointer_actions:
            pointer = value['pointer']
            w, h = self.viewport
            coords = {k: min(round(pointer[k] * (w if k.endswith('x') else h)),
                             w - 1 if k.endswith('x') else h - 1)
                      for k in pointer}
            return [{'type': 'mouse_' + value['action'],
                     'button': 'left', **coords}]
        if value['action'] in self.key_actions:
            kind, key = self.key_actions[value['action']]
            return [{'type': kind, 'keycode': key}]
        key = self.actions[value['action']]
        return [] if key is None else [{'type': 'key_press', 'keycode': key}]

    def scripted_action(self, step, reason='Declared playtest input'):
        """Accept legacy action IDs and pointer-capable action records."""
        if isinstance(step, str):
            step = {'action': step}
        return self.validate_action({**step, 'reason': reason}) if isinstance(step, dict) else self.validate_action(step)

    def validate_scenario(self, scenario):
        if not isinstance(scenario, str) or not scenario or len(scenario) > 128:
            raise ValueError('Expected a nonempty scenario name of at most 128 characters')

    def seed(self, project, scenario):
        return None  # Task adapters override when scenario seeds are meaningful.

    def measured(self, status):
        return {k: status[k] for k in self.replay_fields}

    def terminal(self, status):
        return status['terminal'] is True

    def improved(self, before, after, minimum_gain=.25):
        """Task-specific objective for the optional behavior skill training loop."""
        raise NotImplementedError('This adapter must define improved() before skill training')

    def equivalent(self, stepped, replay):
        for key in self.replay_fields:
            if key not in stepped or key not in replay:
                return False
            a, b = stepped[key], replay[key]
            if key in self.numeric_fields:
                if (type(a) not in (int, float) or type(b) not in (int, float) or
                        not math.isfinite(a) or not math.isfinite(b) or abs(a-b) >= 1e-6):
                    return False
            elif a != b:
                return False
        return True
