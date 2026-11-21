"""Generate reactive policies for training and candidate acceptance.

The authored function only receives the same episode's structured observations
and action history. It cannot import modules, access files, or edit the game.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from .adapters import resolve


_SAFE_CALLS = {'abs': abs, 'bool': bool, 'float': float, 'int': int,
               'len': len, 'max': max, 'min': min, 'round': round}
_ALLOWED = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign,
            ast.If, ast.Return, ast.Pass, ast.Name, ast.Load, ast.Store,
            ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Subscript,
            ast.Call, ast.Attribute, ast.Compare, ast.BoolOp, ast.BinOp,
            ast.UnaryOp, ast.IfExp, ast.Slice, ast.And, ast.Or, ast.Not,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is,
            ast.IsNot, ast.In, ast.NotIn, ast.Add, ast.Sub, ast.Mult,
            ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd)


def compile_policy(source: str):
    """Accept a pure Python policy, rejecting I/O and unbounded control flow."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError('Policy source must be a nonempty string')
    tree = ast.parse(source, filename='policy.py')
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError('Policy must contain exactly one function: act')
    if sum(isinstance(node, ast.FunctionDef) for node in ast.walk(tree)) != 1:
        raise ValueError('Nested policy functions are not allowed')
    function = tree.body[0]
    args = function.args
    if (function.name != 'act' or function.decorator_list or function.returns is not None or
            [a.arg for a in args.args] != ['state', 'recent'] or args.posonlyargs or
            args.kwonlyargs or args.vararg or args.kwarg or args.defaults or
            args.kw_defaults or any(a.annotation is not None for a in args.args)):
        raise ValueError('Expected def act(state, recent) without annotations or decorators')
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f'Unsupported policy syntax: {type(node).__name__}')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('Dunder names are unavailable in policies')
        if isinstance(node, ast.Assign):
            if any(not isinstance(target, ast.Name) or target.id in ('state', 'recent', 'act')
                   for target in node.targets):
                raise ValueError('Policies may only assign local variables')
        if isinstance(node, ast.Attribute) and node.attr != 'get':
            raise ValueError('Only dictionary .get() access is allowed')
        if isinstance(node, ast.Call):
            callee = node.func
            if not ((isinstance(callee, ast.Name) and callee.id in _SAFE_CALLS) or
                    (isinstance(callee, ast.Attribute) and callee.attr == 'get')):
                raise ValueError('Policy call is not allowed')
            if node.keywords:
                raise ValueError('Policy calls must use positional arguments')
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 1000:
            raise ValueError('Policy string is too long')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float) and abs(node.value) > 1e9:
            raise ValueError('Policy numeric constant is too large')
    namespace = {'__builtins__': {}, **_SAFE_CALLS}
    exec(compile(tree, 'policy.py', 'exec'), namespace)
    return namespace['act']


def validate_public_state_keys(source: str, fields):
    """Reject policy reads of private engine fields absent from Player observations."""
    allowed = set(fields)
    for node in ast.walk(ast.parse(source, filename='policy.py')):
        key = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'state'
                and node.func.attr == 'get' and node.args):
            key = node.args[0]
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
              and node.value.id == 'state'):
            key = node.slice
        if key is not None:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise ValueError('Player state keys must be literal public field names')
            if key.value not in allowed:
                raise ValueError(f'Player state field {key.value!r} is not observable; '
                                 f'use only {sorted(allowed)}')


def _source_context(project: Path) -> dict:
    source = {}
    remaining = 18000
    paths = list(project.glob('*.gd')) + list((project / 'scripts').rglob('*.gd'))
    for path in sorted(paths):
        if remaining <= 0:
            break
        text = path.read_text(errors='replace')[:min(9000, remaining)]
        source[path.relative_to(project).as_posix()] = text
        remaining -= len(text)
    return source


def program_player(project, output, *, model='local', backend='local', adapter=None,
                   playtest_context=None, run_objective=None, previous_playtest=None):
    """Build a callable that authors its policy at the first real observation."""
    project, output = Path(project), Path(output)
    adapter = resolve(project, adapter)
    # Initialization shares one program across profiles.
    profile_count = 1
    initialization = bool(playtest_context and playtest_context.get('phase') in
                          ('initialization', 'final_showcase', 'rsi_upgrade'))
    if initialization:
        profile_count = max(1, min(10, len(playtest_context.get('profiles') or [])))
    public_fields = adapter.policy_state_fields() | ({'_profile'} if initialization else set())
    from .objective import COMPLETION_PROMPT, decision
    run_objective = json.loads(json.dumps(run_objective))
    compiled = None
    active_profile = None
    inherited_error = ''
    if playtest_context and playtest_context.get("reuse_source"):
        inherited = playtest_context["reuse_source"].strip()
        try:
            if initialization or run_objective is not None:
                validate_public_state_keys(inherited, public_fields)
            compiled = compile_policy(inherited)
        except (SyntaxError, ValueError) as exc:
            if not initialization:
                raise
            inherited_error = f'Inherited policy must be rewritten: {type(exc).__name__}: {exc}'
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(playtest_context["reuse_source"])

    def set_profile(profile):
        nonlocal active_profile
        active_profile = profile

    def decide(image, skill, recent, step_frames, *, current_state=None):
        nonlocal compiled
        if current_state is None:
            raise ValueError('Program Player requires structured engine observations')
        current_state = dict(current_state)
        if active_profile is not None:
            current_state['_profile'] = active_profile
        if compiled is None:
            import llm
            action_contract = ('fields action (string or null on completion) and reason (string). '
                               if run_objective is not None else 'string fields action and reason. ')
            system = (
                'Write a reactive game-playing policy as Python source. Return JSON with one '
                'string field named source. The source must contain exactly '
                f'def act(state, recent): and return a dictionary with {action_contract}'
                'state is the current structured observation; recent is a list '
                'of previous {frame, action, reason, optional pointer} records from this episode. Use only '
                'if/else, local assignments, returns, arithmetic, comparisons, dictionary '
                'lookups and safe calls abs, bool, float, int, len, max, min, round. '
                'The generated policy must not use imports, loops, classes, file access, network access, '
                'random numbers, hard-coded seeds, or game modifications. The policy is called every '
                f'{step_frames} trace frames at 30 frames per game second '
                f'({step_frames / 30:g} seconds, {step_frames * 2} physics ticks at 60 Hz). '
                'Video recording FPS does not change this action interval. '
                f'Allowed action IDs: {adapter.allowed_actions}. '
                + ('For pointer actions return pointer={"x":x,"y":y,"to_x":to_x,"to_y":to_y}; '
                   'down/move/up may keep a drag held across decisions. '
                   'using normalized screen coordinates in [0,1]; click has equal start/end. '
                   'For keyboard and wait actions omit pointer. '
                   if adapter.pointer_actions else '') +
                'Use only keys in public_state_fields. Game source may contain internal state '
                'that the policy never receives; do not read those keys, even with defaults. '
                'Return short reasons.'
            )
            context = {'task_id': adapter.task_id, 'game_rules': adapter.instructions,
                       'state_description': adapter.state_description,
                       'public_state_fields': sorted(public_fields),
                       'sample_state': current_state, 'behavior_skill': skill,
                       'recent_actions': recent[-4:]}
            source_access = {}
            if backend == 'codex':
                source_access['read_only_project'] = str(project.resolve())
                context['game_source_workspace'] = str(project.resolve())
                system += (
                    ' While AUTHORING, use read-only file and search tools in game_source_workspace '
                    'to inspect the full current candidate project as needed. Source is not embedded '
                    'or truncated. Start with project.godot and follow the relevant scripts, scenes, '
                    'input handlers, observation interface, timing windows and completion transitions. '
                    'Read changed interactions and their dependencies before writing the policy; '
                    'do not infer timing from the prior policy or change summaries alone. '
                    'Inspect only this candidate and the supplied prior context, not sibling projects. '
                    'Do not modify files or run the game. Return the policy in the final JSON; '
                    'the host saves and executes it. The generated policy itself cannot read files.'
                )
            else:
                context['game_source'] = _source_context(project)
            if playtest_context is not None:
                system += (
                    ' This is a candidate acceptance playtest, not Behavior Skill training. '
                    'Write a NEW policy for the current candidate code and observed state. '
                    'Exercise the supplied goals and changed interactions through legal controls. '
                    'Do not avoid a defective interaction just to survive longer or maximize score. '
                    'Changes and desired outcomes are hypotheses to test, not proof of success.'
                )
                if not playtest_context.get('behavior_skill_enabled'):
                    context.pop('behavior_skill')
                else:
                    system += (' The supplied Behavior Skill is fixed for this playtest. '
                               'Use it to choose legal actions, but do not change it or claim '
                               'that improved Player behavior proves a game repair.')
                context['playtest'] = playtest_context
                if playtest_context.get('profiles'):
                    system += (
                        ' Author ONE complete program covering ALL supplied profiles. '
                        'state["_profile"] selects the episode strategy; it is a goal label, not a game-state field. '
                        'Each profile has its own launch and goal. Different profiles may share the normal title entry. '
                        'Branch on this label and current observations; do not assume the first sample state '
                        'describes other profiles. Use safe defaults for state keys absent in other screens. '
                        'Adapt valid logic from prior_policy. Only a selected baseline supplies that prior; '
                        'do not copy fixed action sequences or fabricate successful coverage.'
                    )
                if playtest_context.get('phase') == 'initialization':
                    system += (
                        ' This is initialization playtesting with a stable group goal. '
                        'Reuse valid logic from prior_policy, adapting it to the CURRENT version and '
                        'prior observations. The prior program belongs to the selected baseline; '
                        'save your own complete program for this version. React to observed state, '
                        'rather than copying the old fixed action list. Stay within this episode\'s '
                        'declared launch and contribute to the group goal. Missing goal coverage '
                        'must remain visible; do not fabricate a successful outcome.'
                    )
                elif playtest_context.get('phase') == 'final_showcase':
                    system += (
                        ' This is the final game showcase, after all game edits. The supplied '
                        'profiles were planned from public requirements for this final version. '
                        'Show the specified interactions and their visible effects within each '
                        'episode, including important result screens long enough to be observed. '
                        'Use existing launch scenarios only. Do not change game code, fabricate '
                        'outcomes, or copy old fixed action sequences. An unmet goal must remain '
                        'visible in the evidence rather than being claimed as achieved.'
                    )
                    if any(row.get('original_policy') for row in
                           playtest_context.get('showcase_plan', {}).get('profiles', [])):
                        system += (
                            ' A profile may include an archived original_policy, original_trace, '
                            'and original_scene_facts. Use them to recover the same visible '
                            'route on the CURRENT game. Inspect current state and adapt the '
                            'program; the old timing and state names may have changed.'
                        )
                elif playtest_context.get('phase') == 'rsi_upgrade':
                    system += (
                        ' This is a version-owned RSI upgrade playtest. Use the supplied profile goal '
                        'to explore either the original core game or the new upgrade. Follow only '
                        'player-visible observations and legal semantic actions. Show whether the '
                        'new content really works; do not claim coverage from source code alone. '
                        'A profile may be unable to reach its goal in the current version; make '
                        'that failure visible instead of inventing an outcome.'
                    )
                elif playtest_context.get('phase') == 'sustained_playability':
                    system += (
                        ' This is repeated exploration of one FROZEN game, not a repair '
                        'acceptance test. The prior_episodes field is your memory of what '
                        'you actually did and observed. Compare their action sequences, '
                        'outcomes and discoveries before authoring this episode. Pursue '
                        'a different legal route, strategy or deeper progression when the '
                        'game permits it. Do not repeat a previous script merely because '
                        'it is safe. If no alternate route exists, play honestly and let '
                        'the later evaluator mark repetition. Do not modify the game.'
                    )
            if run_objective is not None:
                system += COMPLETION_PROMPT
                context['run_objective'] = run_objective
                context['previous_playtest'] = previous_playtest or []
            schema = {'type': 'object', 'additionalProperties': False,
                      'required': ['source'], 'properties': {'source': {'type': 'string'}}}
            if initialization:
                system += ' Keep shared logic compact, avoid long comments, and return a complete function.'
            failure = inherited_error
            for attempt in range(3):
                content = [{'type': 'input_text', 'text': json.dumps({**context, 'previous_error': failure})}]
                if attempt == 0:
                    content.append(llm.img(str(image), width=512))
                answer = llm.call(model, system, content, 'game_program_policy', schema,
                                  backend=backend, max_tokens=2200 * profile_count,
                                  effort=os.getenv('GAMECRAFT_CODEX_REASONING_EFFORT') if backend=='codex' else None,
                                  **source_access)
                source = answer.get('source', '').strip()
                if source.startswith('```'):
                    source = source.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
                if initialization:
                    attempts = output.parent / (output.stem + '-attempts')
                    attempts.mkdir(parents=True, exist_ok=True)
                    (attempts / f'{attempt + 1:03}.py').write_text(source + '\n')
                try:
                    candidate = compile_policy(source)
                    if initialization or run_objective is not None:
                        validate_public_state_keys(source, public_fields)
                    probe = candidate(json.loads(json.dumps(current_state)), [])
                    decision(probe, adapter, run_objective)
                except (SyntaxError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError,
                        NameError, AttributeError, OverflowError) as exc:
                    failure = f'{type(exc).__name__}: {exc}'
                    if initialization:
                        (attempts / f'{attempt + 1:03}.json').write_text(json.dumps(
                            {'accepted': False, 'error': failure, 'source_chars': len(source)}, indent=2))
                    continue
                if initialization:
                    (attempts / f'{attempt + 1:03}.json').write_text(json.dumps(
                        {'accepted': True, 'source_chars': len(source)}, indent=2))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(source + '\n')
                output.with_suffix('.json').write_text(json.dumps(
                    {'backend': backend, 'model': model, 'sample_state': current_state,
                     **({'playtest': playtest_context,
                         'purpose': 'initialization' if playtest_context.get('phase') == 'initialization' else 'candidate_acceptance',
                         **({'skill': skill} if playtest_context.get('behavior_skill_enabled') else {})}
                        if playtest_context is not None else {'skill': skill}),
                     'run_objective': run_objective, 'previous_playtest': previous_playtest or [],
                     'source': str(output)}, indent=2))
                compiled = candidate
                break
            if compiled is None:
                raise RuntimeError(f'Agent did not produce a valid Player policy in 3 attempts: {failure}')
        state = json.loads(json.dumps(current_state))
        history = [{k: row[k] for k in ('frame', 'action', 'reason', 'pointer') if k in row}
                   for row in recent]
        value = compiled(state, history)
        return decision(value, adapter, run_objective)

    decide.run_objective = run_objective
    decide.set_profile = set_profile
    decide.adapter_id = adapter.task_id
    decide.program_mode = True
    decide.policy_path = str(output)
    return decide
