"""HTML-specific agents and browser execution for grouped initialization."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from codex_provider import child_environment, from_args
from . import contracts as c
from .__main__ import DOCS, digest, read_json, write_json
from .html_assets import bundle_html, source_text, stage_materials
from .method import codex

PUBLIC_ONLY = ('Use only the supplied public task documents, game source and independent playtest evidence. '
               'Do not inspect official tests, runners, reports, reference games or previous benchmark scores. ')


def sample_indices(frames, limit):
    """Prefer brief state/outcome transitions before filling uniformly in time."""
    if len(frames) <= limit:
        return list(range(len(frames)))
    keys = {'phase', 'state', 'status', 'result', 'gameover', 'game_over',
            'won', 'victory', 'scene', 'screen', 'mode', 'level', 'stage', 'round', 'wave'}
    def signature(value, prefix=''):
        result = {}
        if isinstance(value, dict):
            for key, item in value.items():
                name = prefix + '.' + key
                if isinstance(item, dict):
                    result.update(signature(item, name))
                elif key.lower() in keys and not isinstance(item, list):
                    result[name] = item
        return result
    selected, transitions = {0, len(frames) - 1}, []
    previous = signature(frames[0].get('snapshot'))
    for i, frame in enumerate(frames[1:], 1):
        current = signature(frame.get('snapshot'))
        if current != previous:
            transitions.extend([i - 1, i])
        previous = current
    transitions = sorted(set(transitions) - selected)
    room = limit - len(selected)
    if len(transitions) > room:
        transitions = [transitions[round(i * (len(transitions) - 1) / max(1, room - 1))]
                       for i in range(room)]
    selected.update(transitions)
    uniform = [round(i * (len(frames) - 1) / (limit - 1)) for i in range(limit)]
    for i in uniform:
        if len(selected) >= limit:
            break
        selected.add(i)
    return sorted(selected)


def images(evidence, limit=10):
    frames = read_json(Path(evidence) / 'trace.json')['frames']
    return [Path(evidence) / frames[i]['image'] for i in sample_indices(frames, limit)]


def compact_trace(trace, limit=32):
    """Keep indices from the full trace, including transitions and asset evidence."""
    frames = trace['frames']
    indices = sample_indices(frames, limit)
    return {k: v for k, v in trace.items() if k not in ('frames', 'actions')} | {
        'frames': [dict(index=i, **frames[i]) for i in indices],
        'action_count': len(trace.get('actions', [])), 'total_frames': len(frames)}


class Services:
    def __init__(self, args, documents):
        self.args, self.documents = args, documents

    def ask(self, directory, name, prompt, context, schema, attached=()):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / (name + '.context.json'), context)
        return codex(self.args, directory, PUBLIC_ONLY + prompt + '\nRead ' + name + '.context.json.',
                     directory / (name + '.log'), schema=schema, images=attached)

    def plan(self, output):
        prompt = (
            'Plan a complete bootstrap from ALL public requirements. Extract atomic observable requirements '
            'with exact document excerpts. Do not silently omit required mechanics or UI. Partition their '
            'playtest coverage into exactly group_count complementary groups, including normal game entry. '
            'A group is an observable player goal, not an arbitrary division of assets. launch=null means normal '
            'entry; otherwise use only an existing public TDD scenario as a legal precondition, never a solved '
            'outcome. Plan every needed material role and its code binding. Generated assets may be static '
            'sprites/backgrounds, articulated animation, synchronized coupled actions, or animated scenes. '
            'Choose based on the required visible effect and controllability. Keep collision, progress, HUD and '
            'outcomes in code. Dynamic assets need reference_prompt or reference_asset_id and multiple frames. '
            'Use underscore asset IDs. Shared style contains art direction only. Explain which visuals remain '
            'procedural. A meaningful required action must not be replaced with a moving static picture merely '
            'to avoid generation. background requires scene scope, sprite requires single_subject/static, '
            'animation requires single_subject or coupled_action and loop/event motion. Coupled actions name '
            'at least two included subjects/props. A static sprite or background cannot use '
            'reference_asset_id; only moving animations may reference another asset. '
            'Empty reference fields mean unused. '
            'Budget at most 12 material roles. Return the plan as JSON.')
        context = {'documents': self.documents, 'group_count': self.args.groups}
        for attempt in range(3):
            name = ('plan', 'plan-retry', 'plan-retry-2')[attempt]
            answer = self.ask(output, name, prompt, context, c.PLAN)
            try:
                return c.validate_plan(answer, self.documents, self.args.groups, self.args.task)
            except ValueError as exc:
                if attempt == 2:
                    raise
                context.update(previous_plan=answer, validation_error=str(exc))

    def materials(self, version, plan, only=None, feedback=''):
        material = version / 'material'
        material.mkdir(exist_ok=True)
        write_json(material / 'asset_plan.json', c.material_plan(plan, self.args.task))
        if not (material / 'asset_manifest.json').exists():
            write_json(material / 'asset_manifest.json', dict(schema_version=1, task_id=self.args.task, assets={}))
        if not plan['assets'] or only == []:
            return
        if only and feedback:
            # This is a private candidate copy. An explicit asset repair must not
            # silently reuse the old accepted result merely because its prompt matches.
            manifest = read_json(material / 'asset_manifest.json')
            for aid in only:
                if aid in manifest['assets']:
                    manifest['assets'][aid]['plan_sha256'] = None
            write_json(material / 'asset_manifest.json', manifest)
        env = child_environment(from_args(self.args))
        env.update(CODEX_HOME=str(Path(self.args.codex_home).resolve()), CODEX_BIN=self.args.codex,
                   GAMECRAFT_EXPECTED_CODEX_ACCOUNT_SHA256_12=self.args.expected_account_hash or '',
                   GAMECRAFT_CODEX_PROVIDER=from_args(self.args)['codex_provider'],
                   LLM_BACKEND='codex', OPTIMIZER_BACKEND='codex', GAMECRAFT_JUDGE_BACKEND='codex',
                   GAMECRAFT_JUDGE_MODEL=self.args.model, GAMECRAFT_VIDEO_SKILL_MODEL=self.args.model,
                   GAMECRAFT_CODEX_TIMEOUT=str(self.args.codex_timeout),
                   GAMECRAFT_H3_JOB_TIMEOUT_SECONDS=str(self.args.h3_timeout),
                   H3_BACKEND='local', H3_URL=self.args.h3_url,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', ''))
        command = [self.args.asset_python, '-m', 'gameasg.material', str(material),
                   '--h3-steps', str(self.args.h3_steps)]
        if only:
            command += ['--only', *only]
        if feedback:
            command += ['--feedback', feedback]
        count = len(only or plan['assets'])
        with (version / 'material.log').open('a') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True,
                           timeout=self.args.h3_timeout * count + self.args.codex_timeout)

    def build(self, version, plan, instructions, feedback=None):
        game = version / 'game'
        game.mkdir(exist_ok=True)
        for name, text in self.documents.items():
            (game / name).write_text(text)
        catalog = stage_materials(version, plan)
        write_json(game / 'bootstrap-plan.json', plan)
        write_json(game / 'change-request.json', {'instructions': instructions, 'feedback': feedback})
        frozen = {name: digest(game / name) for name in DOCS}
        prompt = PUBLIC_ONLY + (
            'Read all three public documents, bootstrap-plan.json, assets-catalog.json, and change-request.json. '
            'Implement the complete game in index.html with all listed requirement groups, not only the active '
            'group. Preserve functioning controls and progression. Implement window.__gameTest against the SAME '
            'state that is drawn and controlled by real inputs. Inline all first-party code and avoid external '
            'network dependencies. Include exactly <script src="gameasg-assets.js"></script> before game code; '
            'the host will inline this immutable material runtime. When editing, replace the existing '
            'GAMEASG MATERIALS BEGIN/END block with that marker without reading its huge base64 data. '
            'Use GameASGAssets.draw(ctx,id,x,y,w,h,elapsedSeconds) for EVERY planned binding; advance local '
            'animation time according to gameplay, use ready(id), and retain code-owned collision/state/HUD. '
            'Do not use generated video for outcomes. Never edit gameasg-assets.js, assets-catalog.json, '
            'bootstrap-plan.json or the public documents. All changes belong in index.html. '
            'Check syntax, real control paths, complete/retry flow, and closing </html>.')
        codex(self.args, game, prompt, version / 'builder.log', writable=True)
        if any(digest(game / n) != sha for n, sha in frozen.items()):
            raise ValueError('Builder changed public documents')
        # Recreate immutable data in case an editor touched it, before the final inlining.
        stage_materials(version, plan)
        return bundle_html(game)

    def probe(self, game, output, policy, group):
        output.mkdir(parents=True, exist_ok=False)
        config = dict(policy=policy, launch=group['launch'], max_seconds=self.args.play_seconds,
                      max_actions=self.args.max_actions)
        write_json(output / 'player.json', config)
        script = Path(__file__).with_name('browser_player.js')
        command = [self.args.docker, 'run', '--rm', '--network', 'none']
        for src, dst, readonly in ((game, '/game', True), (script, '/player.js', True), (output, '/out', False)):
            command += ['--mount', f'type=bind,src={src.resolve()},dst={dst}' + (',readonly' if readonly else '')]
        command += [self.args.image, 'bash', '-lc', 'NODE_PATH="$(npm root -g)" node /player.js /game/index.html /out /out/player.json']
        env = dict(os.environ)
        if self.args.docker_host:
            env['DOCKER_HOST'] = self.args.docker_host
        with (output / 'container.log').open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=self.args.play_seconds + 90, check=False)
        if not (output / 'trace.json').exists():
            raise RuntimeError(f'Browser produced no trace: {output}')
        trace = read_json(output / 'trace.json')
        if trace['frames']:
            frames = trace['frames']
            concat = []
            for i, frame in enumerate(frames):
                concat += [f"file '{frame['image']}'", 'duration ' + str(max(.001,
                    ((frames[i+1]['time_ms'] - frame['time_ms']) / 1000) if i+1 < len(frames) else .25))]
            concat += [f"file '{frames[-1]['image']}'"]
            (output / 'video-frames.txt').write_text('\n'.join(concat) + '\n')
            with (output / 'encode.log').open('w') as log:
                subprocess.run([self.args.ffmpeg, '-v', 'error', '-f', 'concat', '-safe', '0', '-i',
                                str(output / 'video-frames.txt'), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', str(output / 'video.mp4')],
                               stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
        return trace

    def player(self, game, plan, group, output, initial, prior=None, changes=None):
        answer = self.ask(output, 'player',
            'Write a reactive browser Player for this exact game version and group goal. Return skill and '
            'JavaScript defining function step(observation,memory). observation contains snapshot from the '
            'public getSnapshot, visible controls with normalized x/y, viewport width/height, elapsed_ms and '
            'decision index. memory persists. Return ONE action {type,value,duration_ms} per call. '
            'Types: click/move value="x,y", drag/touch value="x1,y1,x2,y2", key value=KeyboardEvent.key, '
            'wait value="", done value=reason. Coordinates 0..1. duration_ms 0..10000, used to hold key/drag '
            'or wait. Policy runs outside the page without DOM, timers, require, or game APIs. '
            'Use observations to react to progress and UI transitions. Do not assume a fixed timing or auto-start. '
            'Reach every group target, including outcomes and later content when required. You may stop only '
            'after targets or a documented inability to progress. Use prior skills as hints, adapt to changes '
            'and retry feedback. Never change game rules or invoke loadScenario inside the policy.',
            {'documents': self.documents, 'plan': plan, 'group': group, 'game_source': source_text(game),
             'initial': compact_trace(initial), 'prior': prior, 'changes': changes,
             'max_seconds': self.args.play_seconds, 'max_actions': self.args.max_actions},
            c.POLICY, images(output / 'initial'))
        if not answer['skill'].strip() or not answer['policy'].strip():
            raise ValueError('Player returned an empty policy or skill')
        return answer

    def assess(self, group, plan, output, trace):
        answer = self.ask(output, 'coverage',
            'Assess EACH group requirement from actual independent browser evidence. observed means the '
            'interaction was exercised and its outcome visible, even when it failed. passed additionally '
            'means the requirement was met. Missing evidence is not a game defect. Cite actual frame indices. '
            'Request a single policy retry only if improved actions could expose missing goals. Never infer '
            'gameplay from source alone or audible sound from silent video. Return all target requirement IDs.',
            {'group': group, 'requirements': plan['requirements'], 'trace': compact_trace(trace)},
            c.ASSESSMENT, images(output / 'replay'))
        return c.validate_assessment(answer, group, trace)

    def summarize_skill(self, plan, group, output, attempts, *, prior=None, changes=None):
        replays, attached = [], []
        for attempt in attempts:
            trace = attempt['trace']
            actions = trace.get('actions', [])
            indices = sorted({round(i * (len(actions) - 1) / max(1, min(32, len(actions)) - 1))
                              for i in range(min(32, len(actions)))})
            replays.append({**attempt, 'trace': compact_trace(trace),
                            'actions': [{'array_index': i, **actions[i]} for i in indices]})
            attached += images(Path(attempt['path']) / 'replay', 5)
        answer = self.ask(output, 'reflector',
            'Act as an independent Behavior Skill reflection agent AFTER real play. Improve only the '
            'player guidance; do not edit game code, materials, policy or benchmark rules. Read the previous '
            'skill, the executed policies, actions, observed states, screenshots and coverage assessments. '
            'Compare failed and successful attempts. Distinguish policy mistakes from observed game bugs '
            'and infrastructure errors. An error or uncovered goal does not prove a strategy failed. '
            'Return a complete replacement skill of at most 8000 characters: concise conditional decision '
            'rules covering controls, observable cues, progress checks, recovery and goal completion. '
            'Preserve useful prior knowledge only when applicable to this version; qualify untested guidance '
            'and remove contradicted rules. Do not hardcode frame counts, seeds or fixed action sequences. '
            'Cite actual attempt IDs and frame/action/error array indices for the new conclusions. '
            'The Player\'s pre-play skill is a hypothesis, not evidence of success. Explain changes and '
            'list uncertainties. The new skill has not been independently tested; never claim it improves '
            'performance or benchmark scores. Screenshots are silent and follow the attempts order. '
            'Full traces can be read at each attempt path under replay/trace.json.',
            {'documents': self.documents, 'group': group, 'requirements': plan['requirements'],
             'previous_skill': (prior or {}).get('skill', ''), 'changes': changes, 'attempts': replays},
            c.BEHAVIOR_SKILL, attached)
        return c.validate_behavior_skill(answer, attempts)

    def diagnose(self, version, plan, group, evidence, output):
        source_images = []
        manifest = read_json(version / 'material/asset_manifest.json')
        for record in manifest['assets'].values():
            names = record['files']
            for i in sorted({0, len(names)//2, len(names)-1}):
                if len(source_images) < 9:
                    source_images.append(version / 'material' / names[i])
        return self.ask(output, 'judge',
            'Diagnose initialization completeness for the active group. Return all evidenced issues together, '
            'with code/asset/both/trajectory routes and acceptance goals. Separately describe visual and gameplay '
            'richness, including whether required actions/content are missing or repeatedly static. Treat unseen '
            'goals as missing evidence, not automatically broken mechanics. Compare material source frames with '
            'rendered frames: good material used badly is a code issue. Consider articulated motion and scope '
            'upgrades only with observed need and preserve required controllability. All issues feed ONE round '
            'repair plan. The first attached images show play, remaining images are source material samples.',
            {'documents': self.documents, 'plan': plan, 'group': group, 'evidence': evidence,
             'source_images': [str(x) for x in source_images]}, c.DIAGNOSIS,
            images(Path(evidence['path']) / 'replay', 8) + source_images)

    def repair(self, version, plan, diagnosis, output):
        prompt = (
            'Make ONE integrated repair plan. Candidate a may target a useful subset of diagnosed issues; '
            'defer issues that cannot be fixed reliably in this round and explain the choice. '
            'Optionally candidate b replaces relevant fixes with a justified material ownership upgrade, '
            'while keeping all other fixes. Both start from the same incumbent and target the same selected issues. '
            'Return no candidates if only missing evidence/no actionable issue remains. materials lists FULL '
            'definitions for new/replaced IDs only; other materials stay intact. Support adding IDs and bindings. '
            'Static-to-animation upgrades must reference the prior sprite via reference_asset_id; spatial '
            'upgrades expand single_subject to coupled_action or scene. Explain motion synchronization and '
            'which controls/collisions/states remain in code. Do not regenerate sound sources for code-use defects.')
        context = {'plan': plan, 'diagnosis': diagnosis, 'game_source': source_text(version / 'game')}
        diagnosed = c.diagnosis_issue_ids(diagnosis['issues'])
        for attempt in range(2):
            answer = self.ask(output, 'repair' if not attempt else 'repair-retry', prompt, context, c.REPAIR)
            try:
                c.validate_repair_targets(answer, diagnosed)
                return answer
            except ValueError as exc:
                if attempt:
                    raise
                context.update(previous_repair=answer, validation_error=str(exc))

    def select(self, group, diagnosis, options, output):
        attached = []
        for evidence in options.values():
            attached += images(Path(evidence['path']) / 'replay', 4)
        return self.ask(output, 'selection',
            'Compare the incumbent and each candidate using their own freshly authored playtests. Select a '
            'candidate for an evidenced improvement to any selected issue without critical regression. '
            'Unresolved issues outside that candidate are not a reason to reject a real partial improvement. '
            'Do not penalize a game just because its policy missed a goal; mark uncertainty and retain current '
            'when improvement is not demonstrated. Consider source correctness, runtime animation, controls '
            'and required outcomes together. Post-play Behavior Skill reflections have not been replayed; '
            'never count a suggested skill or its claimed benefits as demonstrated game improvement. '
            'Images follow the options order, up to four per option.',
            {'group': group, 'diagnosis': diagnosis, 'options': options}, c.SELECTION, attached)
