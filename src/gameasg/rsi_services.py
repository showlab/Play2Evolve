"""ASG upgrade planning, independent of official benchmark tests and scores."""
from pathlib import Path

from . import contracts as c
from .html_assets import source_text
from .services import Services, images

GOAL = c.obj({
    'existing_features': c.STRINGS,
    'gameplay_upgrade': c.TEXT,
    'aesthetic_upgrade': c.TEXT,
    'novelty_reason': c.TEXT,
    'observable_outcomes': c.STRINGS,
    'requirement_ids': c.STRINGS,
    'instructions': c.TEXT,
    'materials': {'type': 'array', 'maxItems': 12, 'items': c.ASSET},
})
REVIEW = c.obj({'core_playable': {'type': 'boolean'},
                'critical_regression': {'type': 'boolean'},
                'evidence_valid': {'type': 'boolean'}, 'reason': c.TEXT,
                'missing_upgrade_evidence': c.STRINGS})


def build_candidate(goal):
    return dict(id='a', instructions=goal['instructions'], issue_ids=[],
                upgrade_reason=goal['novelty_reason'], materials=goal['materials'])


def validate_goal(goal, plan, documents, task):
    for key in ('gameplay_upgrade', 'aesthetic_upgrade', 'novelty_reason', 'instructions'):
        if not goal[key].strip():
            raise ValueError('RSI needs both gameplay and aesthetic goals: ' + key)
    known = {r['id'] for r in plan['requirements']}
    if not goal['requirement_ids'] or not set(goal['requirement_ids']) <= known:
        raise ValueError('RSI preservation targets must reference existing requirements')
    if not goal['observable_outcomes'] or not all(x.strip() for x in goal['observable_outcomes']):
        raise ValueError('RSI upgrade needs observable outcomes')
    c.apply_candidate(plan, build_candidate(goal), documents, task)
    return goal


class RSIServices(Services):
    def upgrade_goal(self, version, plan, baseline, output, history):
        context = {'documents': self.documents, 'plan': plan,
                   'game_source': source_text(version / 'game'), 'baseline': baseline,
                   'previous_upgrade_goals': history[-5:]}
        for attempt in range(2):
            goal = self.ask(output, 'upgrade-goal' if not attempt else 'upgrade-goal-retry',
                'Plan ONE coherent game upgrade with TWO explicit tracks: core gameplay and aesthetics. '
                'Audit what already exists first; renaming an existing level is not new gameplay. '
                'Consider meaningful new mechanics, level variants, progression or strategic choices, '
                'and visible animation/art direction improvements where appropriate. Choose feasible '
                'changes supported by the supplied source and play evidence. Explain novelty relative '
                'to existing features and earlier cycles. Implement BOTH tracks fully now; the next '
                'three rounds repair the result, not postpone building it. Preserve original public '
                'requirements, controls, test API, legal scenarios and core loops. Specify reachable '
                'observable outcomes from normal game entry and existing requirement_ids to preserve. '
                'Do not invent source quotes or benchmark requirements for new content. materials '
                'contains full definitions of only new/replaced assets; leave the rest intact. '
                'Choose static/single-subject motion/coupled motion/scene scope deliberately. '
                'Update integration code together with assets: state, input and collision stay in code. '
                'Moving assets need valid reference_asset_id or reference_prompt. A new animation can '
                'reference an existing static sprite under a distinct ID. Do not reference itself. '
                'Static assets have empty reference fields. instructions must specify the complete build.',
                context, GOAL, images(Path(baseline['path']) / 'replay', 8))
            try:
                return validate_goal(goal, plan, self.documents, self.args.task)
            except ValueError as exc:
                if attempt:
                    raise
                context.update(previous_goal=goal, validation_error=str(exc))

    def review_build(self, goal, before, after, output):
        return self.ask(output, 'build-review',
            'Admit a runnable full upgrade for subsequent repair rounds. Reject only invalid evidence, '
            'an unplayable core loop or an evidenced critical regression. Incomplete upgrade coverage '
            'or remaining cosmetic defects belong in missing_upgrade_evidence, not automatic rejection. '
            'Distinguish Player mistakes from game defects. Images show before then after.',
            {'goal': goal, 'before': before, 'after': after}, REVIEW,
            images(Path(before['path']) / 'replay', 4) + images(Path(after['path']) / 'replay', 8))

    def diagnose(self, version, plan, group, evidence, output):
        return self.ask(output, 'judge',
            'Diagnose the current RSI game after its full upgrade. Inspect both core gameplay and '
            'aesthetic goals, including new content, meaningful strategy, input/feedback, actual '
            'animation playback and code/material synchronization. Report evidenced defects and '
            'concrete unmet upgrade outcomes, not only original initialization requirements. Preserve '
            'existing functionality. Distinguish unseen content from broken content; Player coverage '
            'problems route to trajectory. Use original requirement IDs where relevant, otherwise []. '
            'All issues feed one integrated repair plan. Do not require all issues solved at once.',
            {'documents': self.documents, 'plan': plan, 'group': group,
             'upgrade_goal': self.goal, 'evidence': evidence,
             'game_source': source_text(version / 'game')}, c.DIAGNOSIS,
            images(Path(evidence['path']) / 'replay', 10))

    def repair(self, version, plan, diagnosis, output):
        # Existing repair contracts/material validation stay shared with initialization.
        return super().repair(version, plan,
                              dict(diagnosis, upgrade_goal=self.goal), output)
