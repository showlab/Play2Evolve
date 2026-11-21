"""Execution profiles for the shared game-evolution loop."""

PROFILES = {
    'rsi': {
        'behavior_enabled': True,
        'player_mode': 'agent',
        'scenario_groups': ('train',),
        'acceptance': 'relative',
        'judge_goal': 'quality_improvement',
        'submit_global_video_skill': False,
    },
    'initialization': {
        'behavior_enabled': False,
        'player_mode': 'scripted',
        'scenario_groups': ('initialization',),
        'acceptance': 'relative',
        'judge_goal': 'functional_and_material_completeness',
        'submit_global_video_skill': True,
    },
}


def resolve(name):
    if name not in PROFILES:
        raise ValueError('Unknown evolution profile: '+str(name))
    return {'name': name, **PROFILES[name]}
