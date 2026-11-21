"""Shared objective and completion contract; no game-specific terminal rules."""
import copy

COMPLETION_PROMPT = (
    ' The supplied run_objective is fixed for the entire RSI run. Do not redefine it. '
    'Return action, reason, done (boolean), and outcome (ongoing/success/failure). '
    'Set done=true only when public observations establish the goal or definitive failure. '
    'For done=true, action must be null and outcome success or failure; no further input is executed. '
    'Otherwise done=false, outcome=ongoing, and action must be legal. '
    'A stage or bout is success only if it satisfies the selected run_objective; otherwise continue. '
    'Do not restart after definitive failure, alter game state, or invent hidden-state triggers. '
    'The host enforces the frame budget and reports truncation separately. '
)


def decision(value, adapter, objective=None):
    if objective is None:
        return adapter.validate_action(value)
    if not isinstance(value, dict) or type(value.get('done')) is not bool:
        raise ValueError('Objective policy requires boolean done')
    reason = value.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Objective policy requires an observation-based reason')
    if value['done']:
        if value.get('action') is not None or value.get('outcome') not in ('success', 'failure'):
            raise ValueError('Completed policy requires null action and success/failure outcome')
        return {'action': None, 'reason': reason[:1000], 'done': True, 'outcome': value['outcome']}
    if value.get('outcome') != 'ongoing':
        raise ValueError('Continuing policy requires ongoing outcome')
    return {**adapter.validate_action(value), 'done': False, 'outcome': 'ongoing'}


def action_schema(adapter):
    schema = copy.deepcopy(adapter.action_schema)
    schema['properties']['action'] = {'anyOf': [schema['properties']['action'], {'type': 'null'}]}
    schema['properties'].update(done={'type': 'boolean'},
                                outcome={'type': 'string', 'enum': ['ongoing', 'success', 'failure']})
    schema['required'] += ['done', 'outcome']
    return schema


def previous_context(episodes):
    """Player-visible history only; never feed private replay telemetry back to Player."""
    rows = []
    for episode in episodes:
        decisions = episode.get('decisions', [])
        indexes = sorted({round(i * (len(decisions)-1) / 15) for i in range(16)}) if decisions else []
        row = {'scenario': episode['scenario'], 'completion': episode.get('completion'),
               'final_public_state': episode.get('final_player_state', {}),
               'decisions': [{k: decisions[i].get(k) for k in ('frame', 'action', 'reason', 'player_state')}
                             for i in indexes]}
        policy = episode.get('player_policy')
        if policy:
            from pathlib import Path
            row['prior_policy'] = Path(policy).read_text()
        rows.append(row)
    return rows
