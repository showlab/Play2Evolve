import re
from pathlib import Path
from gamecraft_behavior.adapters import GameAdapter


class BoxingOpponentAdapter(GameAdapter):
    def __init__(self):
        fields=('player_hp','opponent_hp','player_stamina','opponent_stamina',
                'player_action','opponent_action','player_phase','opponent_phase','opponent_target','difficulty')
        super().__init__('boxing-video-lab',
            'You are the left boxer. Jab (J), hook (H), uppercut (U), block (K), dodge_high (D/duck), '
            'dodge_low (S/slip back), or wait. Jab/hook target high, uppercut targets low. '
            'Read glove pull-back, shoulder turn, or shoulder dip/foot shift tells. '
            'dodge_high avoids high strikes; dodge_low avoids low strikes. Block stops both when stamina is adequate. '
            'A jab winds up 0.4s, hook 0.6s, uppercut 0.7s before contact; they deal 10/16/20 damage and cost 12/20/26 stamina. '
            'Block lasts 0.6s and costs 6 plus 12 per blocked hit; dodges last 0.5s and cost 10. '
            'Low stamina slows attacks and weakens guard. Idle recovers stamina faster. Actions while busy are ignored. '
            'Opponents observe committed tells, guard and counter, and use different seeded attack patterns. '
            'Rookie/contender/champion have progressively faster tells. Do not assume a fixed attack timetable. '
            'Counter during recovery, manage stamina, and end the 12s bout with more HP or knock out the opponent.',
            {'jab':'J','hook':'H','uppercut':'U','block':'K','dodge_high':'D','dodge_low':'S','wait':None},
            Path(__file__).with_name('bridge.gd'), {k:k for k in fields},
            'Left is player, right is opponent. HP/stamina range 0–100. tell is a visible windup, recovery is post-contact. '
            'opponent_target labels the CURRENT visible high/low tell only; none otherwise. Future patterns are not exposed.',
            fields+('player_age','opponent_age','ticks','terminal','outcome','events'),
            numeric_fields=('player_stamina','opponent_stamina'),default_scenario='bout_contender_seed_100')

    def validate_scenario(self, scenario):
        if not isinstance(scenario,str) or not re.fullmatch(r'bout_(rookie|contender|champion)_seed_\d+',scenario):
            raise ValueError('Expected bout_<rookie|contender|champion>_seed_<integer>')

    def seed(self,project,scenario):
        self.validate_scenario(scenario)
        return int(scenario.rsplit('_',1)[1])

    def behavior_metrics(self,result):
        r=result['replay']
        if not r['terminal']:
            raise ValueError('Boxing metrics require a complete bout (60 decisions at step_frames=6)')
        return {'win_rate':float(r['outcome']=='win'),'hp_balance':r['player_hp']-r['opponent_hp']}


def create_adapter():
    return BoxingOpponentAdapter()
