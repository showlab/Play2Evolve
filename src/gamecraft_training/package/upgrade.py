"""Three-round RSI upgrade branch; initialization keeps the repair loop."""
import json
import os
from copy import copy
from pathlib import Path
from types import SimpleNamespace

from gamecraft_assets.contract import material_intent
from gamecraft_assets.plans import validate_generic
from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.program_policy import program_player
from . import models
from .candidates import material_state
from .objective import available_entries
from .version_player import play_version
from .continuation import planning_history


SCOPES = ('single_subject', 'coupled_action', 'scene')


def with_generic_pointer_actions(adapter):
    """Expose ordinary mouse input to both versions without editing the package."""
    from gamecraft_behavior.adapters.base import GameAdapter
    if not isinstance(adapter, GameAdapter) or type(adapter).events is not GameAdapter.events:
        return adapter
    reserved=set(adapter.actions)|set(adapter.key_actions or {})
    extra=tuple(name for name in ('click','drag')
                if name not in reserved and name not in adapter.pointer_actions)
    if not extra:
        return adapter
    augmented=copy(adapter)
    augmented.pointer_actions=(*adapter.pointer_actions,*extra)
    augmented.__post_init__()
    return augmented


def _source(project, *, limit=50000):
    rows = []
    paths = sorted(path for path in Path(project).rglob('*')
                   if path.is_file() and path.suffix in ('.gd', '.tscn', '.gdshader'))
    game_plan = Path(project)/'game_plan.json'
    if game_plan.is_file():
        paths.insert(0, game_plan)
    for path in paths:
        if limit <= 0:
            break
        excerpt = path.read_text(errors='replace')[:min(12000, limit)]
        rows.append({'file': path.relative_to(project).as_posix(), 'text': excerpt})
        limit -= len(excerpt)
    return rows


def _audit_baseline(package, project, adapter, cases, plan, baseline):
    """Describe the incumbent using only the public brief and game evidence."""
    public = Path(package)
    requirements = json.loads((public/'requirements.json').read_text())['requirements']
    brief = (public/'instruction.md').read_text(errors='replace')[:14000]
    return models.call(
        'Audit the CURRENT game before planning an upgrade. This is not a benchmark review. '
        'Use only the supplied public brief, implementation, assets, declared entries and '
        'recorded play. Separate code-supported features from features visibly exercised in '
        'the replay. Identify missing or repetitive player choices, progression, animation, '
        'atmosphere and interface treatment. A feature already implemented, such as an '
        'existing night economy, must appear under existing_capabilities, never as a new '
        'opportunity. Each claim must cite a concrete source file, asset, or replay event; '
        'mark anything unobserved as uncertain. Suggest several different substantial '
        'opportunities, including gameplay and visual possibilities where justified. '
        'Do not infer hidden scoring thresholds or edit files. Return only JSON.',
        {'task_id': adapter.task_id, 'public_brief': brief,
         'public_requirements': requirements, 'code': _source(project),
         'assets': plan['assets'], 'declared_entries': available_entries(adapter, cases),
         'baseline_outcomes': [row.get('replay', {}) for row in baseline],
         **({'previous_rsi_cycles': planning_history(package)} if planning_history(package) else {})},
        models.content(baseline, limit=12)+models.outcome_content(baseline),
        'rsi_upgrade_baseline_audit', {
            'existing_capabilities': {'type': 'array', 'items': {'type': 'string'}},
            'experience_gaps': {'type': 'array', 'items': {'type': 'string'}},
            'visual_gaps': {'type': 'array', 'items': {'type': 'string'}},
            'uncertain': {'type': 'array', 'items': {'type': 'string'}},
            'upgrade_opportunities': {'type': 'array', 'maxItems': 6,
                                      'items': {'type': 'string'}},
        }, tokens=2800, backend='codex',
        model=os.getenv('GAMECRAFT_REPAIR_PLANNER_MODEL', 'gpt-6-sol'))


def _review_novelty(goal, audit, project, baseline, *, execution_mode='incremental'):
    """Challenge both proposed tracks against the original game before editing."""
    return models.call(
        'Independently review BOTH tracks of this proposed upgrade against the ORIGINAL '
        'game. For gameplay, require a meaningful new player choice, encounter, or '
        'progression experience. For aesthetics, require a substantial visible change '
        'such as motion, atmosphere, or a coherent art system, with an explicit H3 material '
        'decision and Godot integration. Recording a playtest is NOT a material plan. '
        'Reject either track if its claimed change is already present or only relabels '
        'existing content. If the replay missed relevant content, inspect the code before '
        'assuming absence. Check that the shared milestones can deliver both tracks using '
        'the selected core and upgrade entries; do not rely on an undeclared third playtest. '
        + ('For build_repair, BOTH upgrades must be planned for the initial complete build; '
           'the following rounds are validation and repair, not deferred feature milestones. '
           if execution_mode == 'build_repair' else '') +
        'Do not demand hidden rubric thresholds. Explain the evidence. Return only JSON.',
        {'proposal': goal, 'baseline_audit': audit, 'original_code': _source(project),
         'baseline_outcomes': [row.get('replay', {}) for row in baseline]},
        models.content(baseline, limit=8), 'rsi_upgrade_novelty_review', {
            'gameplay_already_present': {'type': 'boolean'},
            'aesthetic_already_present': {'type': 'boolean'},
            'gameplay_visible_delta': {'type': 'boolean'},
            'aesthetic_visible_delta': {'type': 'boolean'},
            'material_role_clear': {'type': 'boolean'},
            'evidence_sufficient': {'type': 'boolean'},
            'reason': {'type': 'string'},
        }, tokens=1500, backend='codex',
        model=os.getenv('GAMECRAFT_REPAIR_PLANNER_MODEL', 'gpt-6-sol'))


def _plan_goal(package, project, adapter, cases, plan, baseline, rounds,
               selected_entry, run_objective, *, audit=None, previous_rejection='',
               execution_mode='incremental'):
    entries = available_entries(adapter, cases)
    requirements = json.loads((Path(package)/'requirements.json').read_text())['requirements']
    brief = (Path(package)/'instruction.md').read_text(errors='replace')[:14000]
    prior_cycles = planning_history(package)
    evidence_instruction = (
        'Propose TWO distinct but compatible upgrade tracks from the baseline audit: '
        '(1) a core gameplay plan with a new player-visible choice, encounter, or '
        'progression experience; (2) an aesthetic plan with a substantial visible change. '
        'For each, cite the original gap, concrete work and replay evidence of success. '
        'The aesthetic plan must decide what H3 should generate and what Godot must render '
        'or animate. Name candidate material targets; if H3 is inappropriate for a target, '
        'explain that choice in material_strategy. Recording gameplay is validation, NOT '
        'H3 generation or an aesthetic upgrade. '
        + ('Integrate BOTH plans into one coherent goal. '
           'Build the complete gameplay and aesthetic upgrade BEFORE any of the '+str(rounds)+
           ' repair rounds. The milestones describe playtest/diagnosis focus for those '
           'rounds, never deferred feature implementation. ' if execution_mode == 'build_repair'
           else 'Integrate BOTH plans into one coherent three-round goal. '
                'Milestones may sequence the tracks, but by the final round both '
                'must be visible through the selected core and upgrade playtest entries. ')+
        ('Both tracks must be reachable through the selected core and upgrade playtest entries. '
         if execution_mode == 'build_repair' else '')+
        'Do not '
        'require extra scenario entries that those two playtests do not cover. Set focus '
        'to combined. Do not repeat capabilities already in the original or use hidden '
        'benchmark scores or thresholds. Previous reviewer objection: '+previous_rejection+' '
        if audit is not None else '')
    track_schema = {
        'gameplay_plan': {'type': 'object', 'additionalProperties': False,
                          'required': ['baseline_gap', 'change', 'code_work', 'validation'],
                          'properties': {key: {'type': 'string'} for key in
                                         ('baseline_gap', 'change', 'code_work', 'validation')}},
        'aesthetic_plan': {'type': 'object', 'additionalProperties': False,
                           'required': ['baseline_gap', 'change', 'material_strategy',
                                        'h3_material_targets', 'code_integration', 'validation'],
                           'properties': {
                               **{key: {'type': 'string'} for key in
                                  ('baseline_gap', 'change', 'material_strategy',
                                   'code_integration', 'validation')},
                               'h3_material_targets': {'type': 'array', 'maxItems': 4,
                                                       'items': {'type': 'string'}},
                           }},
    } if audit is not None else {}
    answer = models.call(
        evidence_instruction+'Choose ONE ambitious, coherent game upgrade to pursue over all RSI rounds. It may improve '
        'gameplay, visual aesthetics, or both. New characters, tracks, modes, dynamic animation and '
        'stronger visual coherence are valid when justified by the brief and current game. Do not '
        'invent a feature merely to make a larger diff. Describe the PLAYER-VISIBLE difference, '
        'the separate Godot and video-generation responsibilities, and a runnable milestone for '
        'each round. A half-finished first milestone need not beat the original game. Keep all '
        'original required controls and benchmark scenarios. The adapter actions and declared '
        'entry IDs are frozen during this run; design the upgrade so it is reachable through them. '
        'Choose a declared entry that can exercise the upgrade after implementation. Keep the '
        'core and upgrade playtest goals focused and reachable within 600 trace frames each. '
        'The selected core entry and its objective provide the original-play regression target; '
        'the upgrade entry may differ but must be declared. '
        'Use observed video as evidence; source code can reveal possibilities but not prove quality. '+
        ('Previous RSI cycles are provenance, not proof of quality. Reassess the CURRENT '
         'game and plan a further upgrade; do not claim already retained changes as new. '
         'A rejected goal may be revisited only with a revised approach. ' if prior_cycles else '') +
        'Return only JSON; do not edit or run files.',
        {'task_id': adapter.task_id, 'brief': brief, 'requirements': requirements,
         'rules': adapter.instructions,
         'actions': getattr(adapter, 'allowed_actions', list(adapter.actions)),
         'entries': entries, 'selected_core_entry': selected_entry,
         'original_run_objective': run_objective,
         **({'previous_rsi_cycles': prior_cycles} if prior_cycles else {}),
         **({'baseline_audit': audit} if audit is not None else {}),
         'assets': plan['assets'], 'code': _source(project),
         'rounds': rounds},
        models.content(baseline, limit=12)+models.outcome_content(baseline),
        'rsi_upgrade_goal', {
            'goal': {'type': 'string'},
            'focus': {'type': 'string', 'enum': (['combined'] if audit is not None else
                                              ['gameplay', 'aesthetic', 'combined'])},
            'player_visible_change': {'type': 'string'},
            'reason': {'type': 'string'},
            'code_responsibility': {'type': 'string'},
            'video_responsibility': {'type': 'string'},
            'core_playtest_goal': {'type': 'string'},
            'upgrade_playtest_goal': {'type': 'string'},
            'upgrade_entry_mode': {'type': 'string', 'enum': ['default', 'scenario']},
            'upgrade_entry_id': {'type': 'string',
                                 'enum': sorted({entry['entry_id'] for entry in entries})},
            'preserve': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                         'items': {'type': 'string'}},
            'success_signals': {'type': 'array', 'minItems': 1, 'maxItems': 5,
                                'items': {'type': 'string'}},
            'milestones': {'type': 'array', 'minItems': rounds, 'maxItems': rounds,
                           'items': {'type': 'string'}},
            **track_schema,
        }, tokens=4200 if audit is not None else 3200, backend='codex',
        model=os.getenv('GAMECRAFT_REPAIR_PLANNER_MODEL', 'gpt-6-sol'))
    if not any(answer['upgrade_entry_mode'] == entry['entry_mode']
               and answer['upgrade_entry_id'] == entry['entry_id'] for entry in entries):
        raise ValueError('Upgrade playtest selected an unavailable entry')
    for key in ('goal', 'player_visible_change', 'reason', 'code_responsibility',
                'video_responsibility', 'core_playtest_goal', 'upgrade_playtest_goal'):
        if not answer[key].strip():
            raise ValueError('Upgrade goal needs a concrete '+key)
    if len(answer['milestones']) != rounds or any(not row.strip() for row in answer['milestones']):
        raise ValueError('Upgrade goal needs one nonempty milestone per round')
    if audit is not None:
        for track in ('gameplay_plan', 'aesthetic_plan'):
            for key, value in answer[track].items():
                if isinstance(value, str) and not value.strip():
                    raise ValueError(f'{track}.{key} needs a concrete plan')
    return answer


def _plan_goal_evidence(package, project, adapter, cases, plan, baseline, rounds,
                        selected_entry, run_objective, output, *, execution_mode='incremental'):
    audit = _audit_baseline(package, project, adapter, cases, plan, baseline)
    write_json(Path(output)/'rsi_baseline_audit.json', audit)
    if not audit['upgrade_opportunities']:
        raise ValueError('Baseline audit found no evidenced upgrade opportunity')
    attempts = []
    for _ in range(3):
        objection = attempts[-1]['review']['reason'] if attempts else ''
        proposal = _plan_goal(package, project, adapter, cases, plan, baseline, rounds,
                              selected_entry, run_objective, audit=audit,
                              previous_rejection=objection, execution_mode=execution_mode)
        review = _review_novelty(proposal, audit, project, baseline,
                                 execution_mode=execution_mode)
        attempts.append({'proposal': proposal, 'review': review})
        write_json(Path(output)/'rsi_goal_attempts.json', attempts)
        if (not review['gameplay_already_present'] and
                not review['aesthetic_already_present'] and
                review['gameplay_visible_delta'] and review['aesthetic_visible_delta'] and
                review['material_role_clear'] and review['evidence_sufficient']):
            return proposal
    raise ValueError('No novel, evidenced RSI upgrade goal after three proposals: '
                     + attempts[-1]['review']['reason'])


def _material_schema():
    from .boundary_planner import schema
    groups = schema(['upgrade'])['groups']['items']['properties']
    return groups['candidates']['items']['properties']['materials']


def _textualize_static_references(option, plan):
    """H3's static t2va path cannot consume an incumbent image reference."""
    known = {asset['id']: asset for asset in plan['assets']}
    for entry in option['materials']:
        asset = entry['asset']
        reference_id = asset.get('reference_asset_id')
        if material_intent(asset)[1] != 'static' or not reference_id:
            continue
        if reference_id not in known:
            continue  # Keep the normal validator's unknown-reference error.
        description = known[reference_id].get('description', '').strip()[:400]
        suffix = ' Visual continuity with existing art: ' + description if description else ''
        if suffix and len(asset['prompt']) + len(suffix) <= 4000:
            asset['prompt'] += suffix
        asset.pop('reference_asset_id', None)


def _normalize_build_materials(option, plan, config=None):
    """Make the new build path's material requests executable by the selected H3 backend."""
    _textualize_static_references(option, plan)
    known = {asset['id']: asset for asset in plan['assets']}
    bindings = (config or {}).get('assets', {}).get('bindings', {})
    for entry in option['materials']:
        asset = entry['asset']
        scope, motion = material_intent(asset)
        if entry['operation'] == 'add':
            # A new material has no incumbent interface to preserve. Its role
            # determines the kind; model responses sometimes confuse these.
            asset['kind'] = ('background' if scope == 'scene' else
                             'sprite' if motion == 'static' else 'animation')
        reference_id = asset.get('reference_asset_id')
        bound = bindings.get(reference_id, []) if reference_id in known else []
        if bound and Path(bound[0]).suffix.lower() == '.svg':
            # Godot can display SVG, but H3's image-reference path uses PIL.
            # Keep the visual identity as text rather than copying SVG as PNG.
            description = known[reference_id].get('description', '').strip()
            if not asset.get('reference_prompt'):
                asset['reference_prompt'] = description or asset['description']
            asset.pop('reference_asset_id', None)
    if os.environ.get('H3_BACKEND', 'local') != 'wan':
        for entry in option['materials']:
            asset = entry['asset']
            duration = asset.get('source_duration_seconds')
            if duration is not None and duration < 4:
                asset['source_duration_seconds'] = 4


def _plan_stage(goal, ordinal, project, plan, config, previous, assessment, episodes,
                *, attempt_feedback='', phase='incremental', repair_context=None):
    if phase not in ('incremental', 'full_build', 'repair'):
        raise ValueError('Unknown upgrade planning phase')
    material_schema = _material_schema()
    if phase == 'full_build':
        material_schema['maxItems'] = 8
    properties = {
        'reason': {'type': 'string'},
        'scope': {'type': 'string', 'enum': list(SCOPES)},
        'code_instructions': {'type': 'string'},
        'video_controls': {'type': 'array', 'items': {'type': 'string'}},
        'code_controls': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
        'materials': material_schema,
    }
    prior_error = ''
    for attempt in range(3):
        phase_instruction = (
            'Build the COMPLETE gameplay and aesthetic goal in ONE playable candidate now. '
            'There are no later implementation milestones: the next rounds only playtest '
            'and repair this full build. Plan all essential Godot changes and H3 materials '
            'together, choosing at most eight material requests. Keep the build focused '
            'enough to replay both selected entries. '
            if phase == 'full_build' else
            'Repair the existing complete candidate using the supplied observed issues '
            'and goal-track review. Fix both gameplay and aesthetic defects that the '
            'evidence supports, while preserving working parts. Do not add an unrelated '
            'new feature or postpone a known blocking repair to another round. '
            if phase == 'repair' else '')
        track_instruction = (
            'This goal has separate gameplay_plan and aesthetic_plan. Carry forward both. '
            'If this milestone includes the aesthetic plan, implement its stated visual '
            'change and Godot integration. A playtest recording is evidence, not a material. '
            'If materials is empty, explain in reason how the milestone still advances '
            'the aesthetic plan or why its material work is scheduled for another round. '
            if 'aesthetic_plan' in goal else '')
        lead = (
            'You are the single code-and-H3 execution planner for this RSI upgrade milestone. '
            'Plan one coherent candidate of meaningful scope. Continue the SAME overall upgrade '
            'goal, using previous milestone feedback to fix incomplete integration. You may add '
            if phase == 'incremental' else
            'You are the single code-and-H3 execution planner for this RSI upgrade. '
            'Plan one coherent candidate of meaningful scope. Use previous feedback to fix '
            'incomplete integration. You may add ')
        answer = models.call(
        phase_instruction+track_instruction+lead+
        'characters, tracks, dynamic animation, feedback or content if the goal requires it. '
        'The code agent will own all planned H3 calls, inspect actual frames, and integrate '
        'them with Godot. Write COMPLETE prompts for each material; do not append to old prompts. '
        'Choose single_subject, coupled_action or scene video ownership based on interactive '
        'control requirements. Code retains input timing, physics, collision, scoring, HUD and '
        'outcomes. Do not duplicate video-owned subjects in Godot. Use at most '+
        ('eight' if phase == 'full_build' else 'four')+' material '
        'requests this round; choose the most important ones. Existing asset replacements must '
        'keep kind, size, frame count, fps and generation intent; add a new asset ID for a new '
        'interface or static-to-animation change. A scene material must use kind=background; '
        'a foreground static material must use kind=sprite; a foreground moving material '
        'must use kind=animation. Static materials must leave '
        'reference_asset_id empty; describe the visual style fully in the prompt. '
        + ('The selected local H3 backend requires a four-second source clip even if '
           'the exported game asset contains fewer frames. '
           if phase != 'incremental' and os.environ.get('H3_BACKEND', 'local') != 'wan'
           else '') +
        'New material must be reachable by the frozen '
        'adapter actions. '+
        ('The initial build must include the planned H3 material targets. '
         if phase == 'full_build' and goal.get('aesthetic_plan', {}).get('h3_material_targets')
         else 'A code-only candidate may have no materials. ')+
        'Explain the runnable '
        'milestone, not just a list of small defects. Return only JSON; do not edit files.',
        {'upgrade_goal': goal, 'round': ordinal,
         'milestone': ('complete both tracks' if phase == 'full_build' else
                       goal['milestones'][ordinal-1]),
         'previous_rounds': previous[-2:], 'current_assessment': assessment,
         **({'previous_attempt_feedback': attempt_feedback} if attempt_feedback else {}),
         **({'repair_context': repair_context} if repair_context is not None else {}),
         'assets': plan['assets'], 'bindings': config['assets']['bindings'],
         'code': _source(project), 'previous_validation_error': prior_error},
        models.content(episodes, limit=12)+models.outcome_content(episodes)
            if attempt == 0 else [],
        'rsi_upgrade_stage_plan', properties,
        tokens=6500 if phase == 'full_build' else 4300, backend='codex',
        model=os.getenv('GAMECRAFT_REPAIR_PLANNER_MODEL', 'gpt-6-sol'))
        try:
            if phase != 'incremental':
                _normalize_build_materials(answer, plan, config)
            _validate_stage(answer, plan, config)
            if (phase == 'full_build' and goal.get('aesthetic_plan',{}).get('h3_material_targets')
                    and not answer['materials']):
                raise ValueError('Complete build omitted planned H3 material targets')
            return answer
        except ValueError as exc:
            prior_error = str(exc)
    raise ValueError('Upgrade planner returned three invalid stage plans: '+prior_error)


def _validate_stage(option, plan, config):
    if not option['code_instructions'].strip() or not option['reason'].strip():
        raise ValueError('Upgrade stage needs an executable code brief and reason')
    if set(option['video_controls']) & set(option['code_controls']):
        raise ValueError('Video and Godot cannot own the same control')
    known = {asset['id']: asset for asset in plan['assets']}
    changed = {}
    for entry in option['materials']:
        asset = entry['asset']
        aid = asset['id']
        if asset.get('source_duration_seconds') is None:
            asset.pop('source_duration_seconds', None)
        if not asset.get('reference_asset_id'):
            asset.pop('reference_asset_id', None)
        if asset['kind'] == 'animation':
            asset['loop'] = asset['motion'] == 'loop'
        if aid in changed or not asset['prompt'].strip() or len(asset['prompt']) > 4000:
            raise ValueError('Duplicate or invalid planned material')
        if entry['operation'] == 'replace':
            if aid not in known:
                raise ValueError('Unknown replacement material')
            old = known[aid]
            if any(asset[key] != old[key] for key in ('kind', 'size', 'frames', 'fps')) or (
                    material_intent(asset) != material_intent(old)):
                raise ValueError('Material replacement changed its interface; add a new ID')
        elif entry['operation'] == 'add':
            if aid in known:
                raise ValueError('New material ID already exists')
        else:
            raise ValueError('Unknown material operation')
        reference = asset.get('reference_asset_id')
        if reference and reference not in known:
            raise ValueError('Material reference must name an incumbent asset')
        if reference == aid and entry['operation'] == 'replace':
            asset['incumbent_reference'] = True
        if material_intent(asset)[1] == 'static' and reference:
            raise ValueError('Static material cannot use an image reference')
        changed[aid] = asset
    validate_generic({**plan, 'assets': [changed.get(aid, asset) for aid, asset in known.items()]
                      + [asset for aid, asset in changed.items() if aid not in known]})
    branch_plan, branch_config = material_state(plan, config, option, 'upgrade_validation')
    if set(branch_config['assets']['bindings']) != {a['id'] for a in branch_plan['assets']}:
        raise ValueError('Upgrade bindings do not match the asset plan')


def _tasks(goal, selected_entry, max_decisions):
    if selected_entry is None:
        raise ValueError('Upgrade RSI needs a planned core-gameplay entry')
    return [
        {'id': selected_entry['entry_id'], 'launch': selected_entry['entry_mode'],
         'profile': 'core', 'goal': goal['core_playtest_goal'],
         'max_decisions': max_decisions},
        {'id': goal['upgrade_entry_id'], 'launch': goal['upgrade_entry_mode'],
         'profile': 'upgrade', 'goal': goal['upgrade_playtest_goal'],
         'max_decisions': max_decisions},
    ]


def _play_version(project, tasks, path, *, skill, prior, goal, adapter, rollout_fn,
                  godot, model, backend, step_frames, max_decisions,
                  policy_factory=program_player):
    profiles = [{'profile': row['profile'], 'scenario': row['id'],
                 'normal_launch': row['launch'] == 'default', 'goal': row['goal']}
                for row in tasks]
    return play_version(project, tasks, path, profiles=profiles, prior=prior, skill=skill,
                        context={'phase': 'rsi_upgrade', 'upgrade_goal': goal,
                                 'behavior_skill_enabled': bool(skill)},
                        adapter=adapter, rollout=rollout_fn, godot=godot,
                        model=model, backend=backend, step_frames=step_frames,
                        max_decisions=max_decisions, factory=policy_factory)


def _stage_review(goal, ordinal, episodes):
    return models.judge_call(
        'Review this PROVISIONAL game version after one RSI upgrade milestone. '
        'Determine whether the milestone is visibly achieved, partial, failed or unobserved. '
        'A partial version is allowed to continue to the next round; do not demand that it '
        'already beat the original game. Mark critical_regression only for broken controls, '
        'unplayable required content, crashes, or a serious rule violation. Missing evidence '
        'is unobserved, not success. Use actual replayed frames and actions, not code claims.',
        {'goal': goal, 'round': ordinal, 'milestone': goal['milestones'][ordinal-1],
         'episode_outcomes': [{'scenario': row['scenario'], 'completion': row.get('completion'),
                               'final_state': row.get('replay', {})} for row in episodes]},
        models.content(episodes, limit=16)+models.outcome_content(episodes),
        'rsi_upgrade_stage_review', {
            'milestone_status': {'type': 'string',
                                 'enum': ['achieved', 'partial', 'failed', 'unobserved']},
            'critical_regression': {'type': 'boolean'},
            'evidence_valid': {'type': 'boolean'},
            'reason': {'type': 'string'},
            'next_round_focus': {'type': 'string'},
        }, tokens=1500)


def _build_candidate(working, rd, option, plan, config, generator, *, goal, ordinal, editor,
                     edit_policy, adapter, assessment, episodes, history,
                     asset_python, video_skill):
    branch_plan, branch_config = material_state(plan, config, option, f'upgrade_{rd.name}')
    write_json(rd/'candidate_asset_plan.json', branch_plan)
    write_json(rd/'candidate_bindings.json', branch_config['assets']['bindings'])
    material_execution = None
    if option['materials']:
        material_execution = {
            'plan': branch_plan, 'config': branch_config, 'option': option,
            'python': asset_python, 'video_skill': video_skill,
            'skill_source': str(generator.root/'video_skill'),
        }
    diagnosis = {
        'id': f'upgrade-{rd.name}', 'problem': option['code_instructions'],
        'upgrade_goal': goal, 'upgrade_round': ordinal,
        'issues': [], 'route': 'both' if option['materials'] else 'composition',
        'asset_ids': [item['asset']['id'] for item in option['materials']],
        'code_instructions': option['code_instructions'],
        'boundary_plan': {'scope': option['scope'],
                          'video_controls': option['video_controls'],
                          'code_controls': option['code_controls'],
                          'repair_units': [{'id': 'upgrade', 'issue_ids': [],
                                            'code_instructions': option['code_instructions']}]},
        'planned_materials': option['materials'],
    }
    candidate = rd/'candidate_project'
    report = editor(working, candidate, rd/'code', diagnosis=diagnosis,
                    evaluation=assessment, episodes=episodes, history=history,
                    adapter=adapter, policy=edit_policy,
                    material_execution=material_execution)
    if option['materials']:
        branch_plan = report['material_plan']
        branch_generator = SimpleNamespace(root=Path(report['generation_workspace']))
    else:
        branch_generator = generator
    return candidate, branch_plan, branch_config, branch_generator, report


def run_upgrade(*, package, output, project, adapter, cases, config, plan, composition,
                generator, baseline, skill, rounds, selected_entry, run_objective,
                player_backend, player_model, godot, rollout_fn, code_editor,
                edit_policy, asset_python, video_skill, step_frames, max_decisions,
                services=models, planning_mode='legacy', execution_mode='incremental'):
    if planning_mode not in ('legacy', 'evidence'):
        raise ValueError('Unknown RSI upgrade planning mode')
    if execution_mode not in ('incremental', 'build_repair'):
        raise ValueError('Unknown RSI upgrade execution mode')
    if execution_mode == 'build_repair' and planning_mode != 'evidence':
        raise ValueError('Build-and-repair requires evidence planning')
    out = Path(output)
    original = Path(project)
    original_digest = digest(original)
    if planning_mode == 'evidence':
        goal = _plan_goal_evidence(package, original, adapter, cases, plan, baseline,
                                   rounds, selected_entry, run_objective, out,
                                   execution_mode=execution_mode)
    else:
        goal = _plan_goal(package, original, adapter, cases, plan, baseline, rounds,
                          selected_entry, run_objective)
    initial_skill = skill
    write_json(out/'rsi_upgrade_plan.json', goal)
    tasks = _tasks(goal, selected_entry, min(600//step_frames, max(100, max_decisions)))
    write_json(out/'rsi_upgrade_playtests.json', tasks)
    if execution_mode == 'build_repair':
        from .build_repair import run as run_build_repair
        return run_build_repair(output=out, original=original, adapter=adapter,
                config=config, plan=plan, composition=composition,
                generator=generator, baseline=baseline, skill=skill, rounds=rounds,
                goal=goal, tasks=tasks, player_backend=player_backend,
                player_model=player_model, godot=godot, rollout_fn=rollout_fn,
                code_editor=code_editor, edit_policy=edit_policy,
                asset_python=asset_python, video_skill=video_skill,
                step_frames=step_frames, services=services)
    working, current_runs = original, baseline
    current_plan, current_config, current_generator = plan, config, generator
    history = []
    attempts_total = 0
    for ordinal in range(1, rounds+1):
        rd = out/f'round-{ordinal:03}'
        rd.mkdir()
        write_json(out/'progress.json', {'stage': 'rsi_upgrade', 'round': ordinal,
                    'max_rounds': rounds, 'project': str(working)})
        attempts=[]
        max_attempts=2 if planning_mode=='evidence' else 1
        for attempt in range(1,max_attempts+1):
            work=rd/f'attempt-{attempt:03}' if planning_mode=='evidence' else rd
            if work!=rd:work.mkdir()
            record = {'round': ordinal, 'attempt':attempt,
                      'project_before': str(working),
                      'milestone': goal['milestones'][ordinal-1], 'status': 'pending'}
            try:
                assessment = services.judge(current_config, adapter, working, current_runs)
                write_json(work/'quality_before.json', assessment)
                feedback=(attempts[-1].get('error') or
                          attempts[-1].get('review',{}).get('reason','')) if attempts else ''
                stage_kwargs={'attempt_feedback':feedback} if planning_mode=='evidence' else {}
                option = _plan_stage(goal, ordinal, working, current_plan, current_config,
                                     history, assessment, current_runs, **stage_kwargs)
                write_json(work/'upgrade_stage_plan.json', option)
                candidate, candidate_plan, candidate_config, candidate_generator, edit = (
                    _build_candidate(working, work, option, current_plan, current_config,
                                     current_generator, goal=goal, ordinal=ordinal,
                                     editor=code_editor,
                                     edit_policy=edit_policy, adapter=adapter,
                                     assessment=assessment, episodes=current_runs,
                                     history=history, asset_python=asset_python,
                                     video_skill=video_skill))
                record['code_edit'] = edit
                evidence = _play_version(candidate, tasks, work/'playtest', skill=skill,
                            prior=current_runs if all(r.get('player_policy') for r in current_runs) else (),
                            goal=goal, adapter=adapter, rollout_fn=rollout_fn, godot=godot,
                            model=player_model, backend=player_backend,
                            step_frames=step_frames, max_decisions=tasks[0]['max_decisions'])
                review = _stage_review(goal, ordinal, evidence)
                record['review'] = review
                record['candidate'] = str(candidate)
                unobserved=(planning_mode=='evidence' and
                            (not review.get('evidence_valid') or
                             review.get('milestone_status') not in ('achieved','partial')))
                record['status'] = ('rejected_critical_regression' if review['critical_regression']
                                    else 'rejected_no_progress' if unobserved else 'provisional')
                if record['status']=='provisional':
                    working, current_runs = candidate, evidence
                    current_plan, current_config = candidate_plan, candidate_config
                    current_generator = candidate_generator
                    write_json(out/'current_asset_plan.json', current_plan)
                    write_json(out/'current_training.json',
                               {k:v for k,v in current_config.items() if not k.startswith('_')})
                    try:
                        proposal = services.skill(adapter, skill, current_runs, history)
                        write_json(work/'skill_proposal.json', proposal)
                        if proposal['skill'].strip() and proposal['skill'].strip() != skill:
                            skill = proposal['skill'].strip()
                            record['behavior'] = 'adopt_unverified'
                        else:
                            record['behavior'] = 'unchanged'
                    except Exception as exc:
                        record['behavior'] = 'error'
                        record['behavior_error'] = f'{type(exc).__name__}: {exc}'
                else:
                    record['behavior'] = 'unchanged_after_rollback'
            except Exception as exc:
                record.update(status='error', error=f'{type(exc).__name__}: {exc}')
            write_json(work/'result.json', record)
            attempts.append(record)
            attempts_total += 1
            if record['status']=='provisional' or planning_mode=='legacy':break
        if planning_mode=='evidence':
            write_json(rd/'attempts.json', attempts)
        record['project_after'] = str(working)
        history.append(record)
        write_json(rd/'result.json', record)
        write_json(out/'history.json', history)
        print(f'[RSI upgrade] round {ordinal}/{rounds}: {record["status"]}', flush=True)
        if planning_mode=='evidence' and record['status']!='provisional':
            print('[RSI upgrade] milestone could not be evidenced; stopping before the next milestone',flush=True)
            break
    # The working project contains only candidates admitted by round review.
    # Do not reject it through a separate, sparsely sampled final comparison.
    upgraded = working != original
    final = {'status': 'accepted' if upgraded else 'not_run',
             'selected': 'upgrade' if upgraded else 'original',
             'selection_method': 'automatic_after_round_review',
             'final_judge_run': False}
    write_json(out/'final_upgrade_review.json', final)
    selected = working if final['selected'] == 'upgrade' else original
    selected_plan = current_plan if final['selected'] == 'upgrade' else plan
    selected_config = current_config if final['selected'] == 'upgrade' else config
    write_json(out/'selected_asset_plan.json', selected_plan)
    write_json(out/'selected_training.json',
               {k:v for k,v in selected_config.items() if not k.startswith('_')})
    write_json(out/'selected_bindings.json', selected_config['assets']['bindings'])
    if digest(original) != original_digest:
        raise RuntimeError('RSI upgrade changed the original game')
    (out/'working_skill.txt').write_text(skill)
    (out/'best_skill.txt').write_text(skill if final['selected'] == 'upgrade' else initial_skill)
    result = {'profile': 'rsi', 'rsi_mode': 'upgrade', 'state': 'completed',
              'upgrade_status': final['status'],
              'project': str(selected), 'original_project': str(original),
              'working_project': str(working), 'skill': str(out/'best_skill.txt'),
              'working_skill': str(out/'working_skill.txt'),
              'upgrade_plan': str(out/'rsi_upgrade_plan.json'),
              'final_review': str(out/'final_upgrade_review.json'),
              'selected_asset_plan': str(out/'selected_asset_plan.json'),
              'selected_training': str(out/'selected_training.json'),
              'rounds_completed': (sum(row['status']=='provisional' for row in history)
                                   if planning_mode=='evidence' else len(history)),
              'rounds_attempted':len(history),
              'attempts_total': attempts_total,
              'planning_mode':planning_mode,
              'errors': [{'round': row['round'], 'reason': row['error']} for row in history
                         if row['status'] == 'error'],
              'behavior_skill_verified': False,
              'final_project_evaluated': False,
              'official_benchmark_metric': False}
    if result['rounds_completed']<rounds or result['errors'] or final['status'] == 'error':
        result['state'] = 'completed_with_errors'
    if video_skill:
        from .continuation import save_selected_video_skill
        selected_generator = current_generator if selected != original else generator
        result['selected_video_skill'] = save_selected_video_skill(out, selected_generator)
    write_json(out/'result.json', result)
    write_json(out/'progress.json', {'stage': result['state'],
                                     'result': str(out/'result.json')})
    print(json.dumps(result), flush=True)
    return result
