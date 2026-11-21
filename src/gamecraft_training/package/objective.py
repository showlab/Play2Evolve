"""Plan one focused RSI playtest and entry before any optimization rounds.

The package runner defaults to --rsi-goal-mode planned, using the Player's
backend/model once. rsi_objective.json records the shared goal; each episode's
completion records success/failure claims or host-budget truncation. --rsi-goal-mode
legacy retains train fixtures and per-bout terminal stopping for old experiments.
Initialization is unaffected. The existing step_frames * max_decisions budget
remains capped at 600 trace frames (20 game seconds), independent of 60 FPS capture.
"""
import json
import os
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.program_policy import _source_context

FIELDS = ('goal', 'success_condition', 'failure_condition', 'continuation_rule')
ENTRY_FIELDS = ('entry_mode', 'entry_id', 'selection_reason')


def available_entries(adapter, cases):
    entries = [{'entry_mode': 'default', 'entry_id': adapter.default_scenario,
                'purpose': 'normal launch', 'declared_actions': []}]
    entries.extend({'entry_mode': 'scenario', 'entry_id': case['id'],
                    'purpose': case['purpose'], 'declared_actions': case.get('actions', [])}
                   for case in cases
                   if case['purpose'] in ('train', 'initialization')
                   and case.get('launch', 'scenario') == 'scenario')
    return entries


def validate(value, entries):
    if not isinstance(value, dict) or any(
            not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 4000
            for key in FIELDS + ENTRY_FIELDS):
        raise ValueError('RSI plan needs a selected entry, reason, and nonempty objective descriptions')
    entry = {key: value[key].strip() for key in ENTRY_FIELDS}
    if not any(entry['entry_mode'] == option['entry_mode'] and
               entry['entry_id'] == option['entry_id'] for option in entries):
        raise ValueError('RSI plan selected an unavailable playtest entry')
    return {key: value[key].strip() for key in FIELDS}, entry


def plan(adapter, project, package, *, model, backend, entries, frame_budget):
    import llm
    project, package = Path(project), Path(package)
    requirements = json.loads((package/'requirements.json').read_text())['requirements']
    briefs = {name: (package/name).read_text() for name in ('instruction.md', 'prompt.txt')
              if (package/name).is_file()}
    context = {'task_id': adapter.task_id, 'game_rules': adapter.instructions,
               'requirements': requirements, 'briefs': briefs,
               'public_state_fields': adapter.observation_fields,
               'state_description': adapter.state_description, 'actions': getattr(adapter,'allowed_actions',list(adapter.actions)),
               'available_entries': entries, 'frame_budget': frame_budget,
               'game_seconds_budget': frame_budget / 30}
    from .continuation import planning_history
    prior_cycles = planning_history(package)
    if prior_cycles:
        context['previous_rsi_cycles'] = prior_cycles
    system = (
        'Plan ONE focused core-gameplay playtest shared by ALL RSI optimization rounds. Do not write a policy. '
        'Read the game requirements and current implementation. Select ONE entry from available_entries: '
        'normal launch or a declared training/initialization scenario. Never use validation, confirmation, '
        'or test scenarios. Choose the entry that lets the Player interact with the most important playable '
        'mechanic within frame_budget; inspect what each scenario actually initializes before selecting it. '
        'Prefer normal launch if core play is quickly reachable; otherwise use a relevant scenario rather '
        'than spending the whole episode on menus or prerequisites. Reject entries that start in an already '
        'finished or unrepresentative state. Use declared_actions only as hints, not a fixed trajectory. '
        'Explain why this entry and mechanic are central in selection_reason. '
        'Keep the goal bounded to this core playtest: do not combine independent modes, optional minigames, '
        'all levels, or an entire career into one objective. Prerequisites are navigation, not goals. '
        'Choose a meaningful, player-visible success milestone feasible within the supplied frame budget; '
        'it need not be a win if completing the game would take longer. For endless games choose an '
        'observable gameplay milestone rather than inventing a final level. '
        'Return entry_mode, entry_id, selection_reason, goal, success_condition, failure_condition, '
        'continuation_rule. The same entry and goal will be used to compare all three rounds. '
        'Failure means definitive player death/defeat or an observable unrecoverable state; '
        'do not treat waiting, a menu, a recoverable mistake or frame budget exhaustion as failure. '
        'All conditions must be checkable from player-visible observations; mention missing signals '
        'rather than inventing public fields. The host reports budget exhaustion separately. '
        'Do not modify files or run the game. Return only the requested JSON.'
    )
    access = {}
    if backend == 'codex':
        context['game_source_workspace'] = str(project.resolve())
        access['read_only_project'] = str(project.resolve())
        system += ' Inspect the full project read-only as needed, including progression and end-state transitions.'
    else:
        context['game_source'] = _source_context(project)
    schema = {'type': 'object', 'additionalProperties': False,
              'required': list(ENTRY_FIELDS + FIELDS),
              'properties': {**{key: {'type': 'string'} for key in ENTRY_FIELDS + FIELDS},
                             'entry_mode': {'type': 'string', 'enum': ['default', 'scenario']},
                             'entry_id': {'type': 'string',
                                          'enum': sorted({entry['entry_id'] for entry in entries})}}}
    return llm.call(model, system, [{'type': 'input_text', 'text': json.dumps(context)}],
                    'rsi_run_objective', schema, backend=backend, max_tokens=1800,
                    effort=os.getenv('GAMECRAFT_CODEX_REASONING_EFFORT') if backend == 'codex' else None,
                    **access)


def create(adapter, project, package, output, *, model, backend, cases=(), frame_budget=600,
           planner=None):
    fingerprint = digest(project)
    entries = available_entries(adapter, cases)
    goal, entry = validate((planner or plan)(adapter, project, package, model=model,
                                            backend=backend, entries=entries,
                                            frame_budget=frame_budget), entries)
    if digest(project) != fingerprint:
        raise RuntimeError('Objective planning changed the game')
    write_json(Path(output)/'rsi_objective.json', {
        'objective': goal, 'entry': entry, 'scope': 'all_rounds',
        'backend': backend, 'model': model, 'project_sha256': fingerprint})
    return goal, entry
