"""Experimental HTML adaptation of material, behavior and composition training.

Only public task documents and independently captured browser evidence enter the
training loop. The pinned official evaluator is called once after final freeze.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from codex_provider import add_arguments, child_environment, cli_args, from_args
from .__main__ import (BENCH_COMMIT, DOCS, benchmark, digest, prepare, freeze,
                       public_documents, score, validate_html, write_json)
from .environment import IMAGE, preflight

PLAN_SCHEMA = {'type': 'object', 'additionalProperties': False,
               'required': ['style', 'description', 'prompt', 'requirements'],
               'properties': {key: {'type': 'string'} for key in
                              ('style', 'description', 'prompt', 'requirements')}}
ACTION_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'required': ['type', 'value'], 'properties': {
                     'type': {'type': 'string', 'enum': ['wait', 'key', 'click', 'move', 'drag']},
                     'value': {'type': 'string'}}}
PLAYER_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'required': ['skill', 'actions'], 'properties': {
                     'skill': {'type': 'string'},
                     'actions': {'type': 'array', 'maxItems': 8, 'items': ACTION_SCHEMA}}}
ISSUE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                'required': ['repair_side', 'issue', 'reason'], 'properties': {
                    'repair_side': {'type': 'string', 'enum': ['asset', 'behavior', 'code', 'none']},
                    'issue': {'type': 'string'}, 'reason': {'type': 'string'}}}
ACCEPT_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'required': ['accept', 'reason'], 'properties': {
                     'accept': {'type': 'boolean'}, 'reason': {'type': 'string'}}}


def codex(args, workdir, prompt, log, *, schema=None, images=(), writable=False):
    config = from_args(args)
    home = Path(args.codex_home).resolve(strict=True)
    if not (home / 'auth.json').is_file():
        raise ValueError('Explicit Codex home needs auth.json')
    if args.expected_account_hash:
        auth = json.loads((home / 'auth.json').read_text())
        account_id = (auth.get('tokens') or {}).get('account_id') or auth.get('account_id')
        actual = hashlib.sha256(str(account_id).encode()).hexdigest()[:12] if account_id else ''
        if actual != args.expected_account_hash:
            raise ValueError('Codex account identity differs from the required hash')
    env = child_environment(config)
    env['CODEX_HOME'] = str(home)
    if args.expected_account_hash:
        env['GAMECRAFT_EXPECTED_CODEX_ACCOUNT_SHA256_12'] = args.expected_account_hash
        if Path(args.codex).parent.name == 'codex-runtime':
            subprocess.run([args.codex, '--check-account'], env=env,
                           check=True, capture_output=True, text=True, timeout=30)
    out = Path(log).with_suffix('.last.txt')
    command = [str(Path(args.codex).resolve()), 'exec', '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'workspace-write' if writable else 'read-only',
               '-c', 'approval_policy="never"', *cli_args(config),
               '--cd', str(Path(workdir).resolve()), '--model', args.model,
               '--output-last-message', str(out)]
    if schema:
        schema_path = Path(log).with_suffix('.schema.json')
        write_json(schema_path, schema)
        command += ['--output-schema', str(schema_path)]
    for path in images:
        command += ['--image', str(Path(path).resolve())]
    log = Path(log)
    for attempt in range(2):
        with log.open('w') as stream:
            result = subprocess.run(command + ['-'], input=prompt, text=True,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    env=env, timeout=args.codex_timeout)
        if not result.returncode:
            break
        with log.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read().decode('utf-8', errors='replace').lower()
        if attempt or not ('at capacity' in tail and 'model' in tail):
            break
        log.replace(log.with_name(log.stem + '.capacity-attempt-1' + log.suffix))
        time.sleep(30)
    if result.returncode:
        raise RuntimeError(f'Codex exited {result.returncode}; see {log}')
    return json.loads(out.read_text()) if schema else out.read_text()


def material_plan(args, root, docs):
    planner = root / 'public'
    prompt = (
        'Read target.md, game-spec.md and tdd.md fully. Plan ONE static H3 background '
        'material for the playable scene of this browser game. It must support, not '
        'replace, game-controlled objects, HUD or interaction. Style is pure art '
        'direction; put any camera or delivery instructions in prompt. The prompt '
        'must describe a stable, top-down or scene-appropriate locked-off plate '
        'with no text, buttons, counters or characters that should move. The '
        'requirements describe how to judge whether the material is usable. '
        'Use only the three public documents. Return JSON only.')
    choice = codex(args, planner, prompt, root / 'material-plan.log', schema=PLAN_SCHEMA)
    if any(not choice[key].strip() or len(choice[key]) > 2000 for key in PLAN_SCHEMA['required']):
        raise ValueError('H3 plan fields must be nonempty and bounded')
    plan = {'schema_version': 1, 'task_id': args.task, 'style': choice['style'],
            'assets': [{'id': 'scene_plate', 'kind': 'background', 'frames': 1, 'fps': 1,
                        'size': [1024, 576], 'description': choice['description'],
                        'prompt': choice['prompt'], 'requirements': choice['requirements']}]}
    from gamecraft_assets.plans import validate_generic
    from gamecraft_assets.contract import validate_shared_style
    validate_shared_style(plan['style'])
    validate_generic(plan)
    material = root / 'material'
    material.mkdir()
    write_json(material / 'asset_plan.json', plan)
    write_json(material / 'asset_manifest.json',
               {'schema_version': 1, 'task_id': args.task, 'assets': {}})
    return material


def generate_material(args, material, log, feedback=None):
    env = child_environment(from_args(args))
    env.update({'CODEX_HOME': str(Path(args.codex_home).resolve()),
                'CODEX_BIN': str(Path(args.codex).resolve()),
                'GAMECRAFT_CODEX_PROVIDER': from_args(args)['codex_provider'],
                'LLM_BACKEND': 'codex', 'OPTIMIZER_BACKEND': 'codex',
                'GAMECRAFT_JUDGE_BACKEND': 'codex',
                'GAMECRAFT_JUDGE_MODEL': args.model,
                'GAMECRAFT_VIDEO_SKILL_MODEL': args.model,
                'GAMECRAFT_CODEX_TIMEOUT': str(args.codex_timeout),
                'GAMECRAFT_H3_JOB_TIMEOUT_SECONDS': str(args.h3_timeout),
                'H3_BACKEND': 'local', 'H3_URL': args.h3_url,
                'PYTHONPATH': str(Path(__file__).resolve().parents[1]) +
                              os.pathsep + env.get('PYTHONPATH', '')})
    # Keep the venv entrypoint path: resolving its python symlink loses pyvenv.cfg.
    command = [str(Path(args.asset_python).expanduser()), '-m', 'gameasg.material', str(material)]
    if feedback:
        command += ['--feedback', feedback]
    with Path(log).open('w') as stream:
        result = subprocess.run(command, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=args.h3_timeout * 3)
    if result.returncode:
        raise RuntimeError(f'H3 material generation exited {result.returncode}; see {log}')
    manifest = json.loads((material / 'asset_manifest.json').read_text())
    record = manifest['assets']['scene_plate']
    frame = material / record['files'][0]
    if not frame.is_file():
        raise RuntimeError('Accepted H3 frame is missing')
    return frame, record


def probe(args, game, output, actions=None):
    output.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_name('browser_probe.js').resolve()
    mounts = [f'type=bind,src={game.resolve()},dst=/envarena/workspace,readonly',
              f'type=bind,src={script},dst=/envarena/probe.js,readonly',
              f'type=bind,src={output.resolve()},dst=/envarena/probe-out']
    if actions is not None:
        write_json(output / 'actions.json', {'actions': actions})
        mounts.append(f'type=bind,src={output / "actions.json"},dst=/envarena/actions.json,readonly')
    command = [str(Path(args.docker).resolve()), 'run', '--rm', '--network', 'none']
    for mount in mounts:
        command += ['--mount', mount]
    node = 'NODE_PATH="$(npm root -g)" node /envarena/probe.js '
    node += '/envarena/workspace/index.html /envarena/probe-out'
    if actions is not None:
        node += ' /envarena/actions.json'
    command += [args.image, 'bash', '-lc', node]
    env = dict(os.environ)
    if args.docker_host:
        env['DOCKER_HOST'] = args.docker_host
    with (output / 'container.log').open('w') as stream:
        result = subprocess.run(command, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=180)
    if result.returncode or not (output / 'trace.json').is_file():
        raise RuntimeError(f'Browser probe failed; see {output / "container.log"}')
    trace = json.loads((output / 'trace.json').read_text())
    if trace['errors']:
        raise RuntimeError(f'Browser runtime errors; see {output / "trace.json"}')
    return trace


def evidence_images(output):
    trace = json.loads((output / 'trace.json').read_text())
    names = [trace['frames'][0]['image'], trace['frames'][-1]['image']]
    return [output / name for name in dict.fromkeys(names)]


def play(args, root, round_dir, game, initial):
    docs = {name: (root / 'public' / name).read_text() for name in DOCS}
    prompt = (
        'Act as a browser game Player. The attached screenshots and trace are '
        'from a new, independent browser run; no benchmark tests are available. '
        'Use the public task documents below and the observed snapshots to write '
        'a concise general Behavior Skill for controlling this game. Then choose '
        'up to eight real browser actions that exercise the core loop. Coordinates '
        'for click/move/drag are normalized to 0..1 (drag uses x1,y1,x2,y2); '
        'key uses a KeyboardEvent key; wait uses milliseconds. Avoid hidden APIs '
        'and hardcoded evaluator states. Return JSON only.\n'
        + json.dumps({'documents': docs, 'trace': initial}, ensure_ascii=False))
    answer = codex(args, round_dir, prompt, round_dir / 'player.log',
                   schema=PLAYER_SCHEMA, images=evidence_images(round_dir / 'initial'))
    if not answer['skill'].strip():
        raise ValueError('Player returned an empty Behavior Skill')
    (round_dir / 'behavior_skill.md').write_text(answer['skill'] + '\n')
    write_json(round_dir / 'actions.json', {'actions': answer['actions']})
    return answer


def inspect(args, root, round_dir, played):
    docs = {name: (root / 'public' / name).read_text() for name in DOCS}
    prompt = (
        'Judge the current browser game only from these public documents and '
        'independent browser playtest evidence. Inspect the screenshots. Name '
        'one concrete, observable issue worth repairing in this round, choosing '
        'asset, behavior, or code. Asset means a defect in the H3 background '
        'plate itself; rendered game objects, icons, fonts, controls and state '
        'belong to code. If none is evidenced, choose none. Do not '
        'infer benchmark score or read benchmark tests. Return JSON only.\n'
        + json.dumps({'documents': docs, 'trace': played}, ensure_ascii=False))
    return codex(args, round_dir, prompt, round_dir / 'judge.log',
                 schema=ISSUE_SCHEMA, images=evidence_images(round_dir / 'played'))


def edit(args, game, round_dir, issue, skill):
    write_json(game / 'method_feedback.json', issue)
    (game / 'behavior_skill.md').write_text(skill + '\n')
    prompt = (
        'Read the three public documents, the current index.html, '
        'method_feedback.json and behavior_skill.md. Make ONE coherent improvement '
        'to index.html addressing the evidenced issue. Preserve the core game, '
        'real mouse/keyboard controls, and window.__gameTest consistency. '
        'Retain the inlined H3 scene material and keep all first-party resources '
        'inside index.html. Do not read benchmark tests, reports or prior scores. '
        'Do not change the public documents. Check JavaScript syntax and ensure '
        'index.html ends with </html>.')
    codex(args, game, prompt, round_dir / 'editor.log', writable=True)
    validate_html(game / 'index.html')


def accept(args, round_dir, issue, before, after):
    prompt = (
        'Compare the before and after independent browser traces and screenshots '
        'against the stated public-doc issue. Accept only if the issue visibly '
        'improves without a critical gameplay or runtime regression. The first '
        'two attached images are before; the last two are after. This is not '
        'official benchmark feedback. Return JSON only.\n'
        + json.dumps({'issue': issue, 'before': before, 'after': after}, ensure_ascii=False))
    images = evidence_images(round_dir / 'played') + evidence_images(round_dir / 'candidate')
    return codex(args, round_dir, prompt, round_dir / 'accept.log',
                 schema=ACCEPT_SCHEMA, images=images)


def run(args):
    bench = benchmark(args.bench)
    public_documents(bench, args.task)
    preflight(argparse.Namespace(bench=bench, docker=args.docker,
                                 docker_host=args.docker_host, image=args.image, cache=None))
    if not 1 <= args.rounds <= 3:
        raise ValueError('Method pilot supports one to three rounds')
    if (bench / 'output' / args.exp_id).exists():
        raise ValueError('Official experiment ID already exists')
    root = Path(args.output).resolve()
    if args.resume or args.resume_game:
        result = json.loads((root / 'method.json').read_text())
        if (result.get('format') != 'gameasg-method-pilot-v1'
                or result.get('task') != args.task
                or result.get('benchmark_commit') != BENCH_COMMIT):
            raise ValueError('Existing method workspace does not match this run')
        if args.resume and (result.get('rounds') or (root / 'game').exists()):
            raise ValueError('Use --resume-game for a workspace with composed HTML')
        if args.resume_game and not (root / 'game' / 'index.html').is_file():
            raise ValueError('Composed HTML is missing for --resume-game')
        public = root / 'public'
        material = root / 'material'
        for name in DOCS:
            if digest(public / name) != digest(public_documents(bench, args.task)[name]):
                raise ValueError('Public document changed before resume')
        if not (material / 'asset_plan.json').is_file():
            raise ValueError('Material plan is missing')
    else:
        root.mkdir(parents=True, exist_ok=False)
        result = {'format': 'gameasg-method-pilot-v1', 'task': args.task,
                  'benchmark_commit': BENCH_COMMIT, 'official_tests_during_training': False,
                  'rounds': [], 'started_at': time.time()}
        write_json(root / 'method.json', result)
        public = root / 'public'
        public.mkdir()
        for name, source in public_documents(bench, args.task).items():
            shutil.copyfile(source, public / name)
        print('stage=plan', flush=True)
        material = material_plan(args, root, public)
    game = root / 'game'
    if args.resume_game:
        manifest = json.loads((material / 'asset_manifest.json').read_text())
        record = manifest['assets']['scene_plate']
        frame = material / record['files'][0]
        if not frame.is_file() or digest(frame) != result['material']['sha256']:
            raise ValueError('Accepted H3 material differs from the composed game')
        for name in DOCS:
            if digest(game / name) != digest(public / name):
                raise ValueError('Composed game public document changed')
        print('stage=resume_game', flush=True)
    else:
        print('stage=h3_material', flush=True)
        frame, record = generate_material(args, material, root / 'material.log')
        result['material'] = {'frame': str(frame), 'sha256': digest(frame),
                              'verdict': record['verdict'], 'video_skill': True}
        write_json(root / 'method.json', result)
        game.mkdir()
        for name in DOCS:
            shutil.copyfile(public / name, game / name)
        shutil.copyfile(frame, game / 'h3-scene.png')
        prompt = (
            'Read target.md, game-spec.md and tdd.md fully. Build this complete '
            'browser game as a single self-contained index.html. The H3-generated '
            'h3-scene.png is an optional scene material; inspect it, then embed it '
            'as a data URI in index.html if usable. Do not reference it as a local '
            'runtime file. Use it behind game-controlled objects and preserve '
            'readability. Inline all first-party CSS/JS/assets. Implement '
            'window.__gameTest.reset, input, getSnapshot and loadScenario using '
            'the same state as visible gameplay. Real controls must work. Use only '
            'the public documents and H3 material. Do not read benchmark tests or '
            'prior scores. Do not edit the public documents. Verify JavaScript '
            'syntax and closing </html>.')
        print('stage=compose', flush=True)
        codex(args, game, prompt, root / 'compose.log', writable=True)
    validate_html(game / 'index.html')
    if 'data:image/' not in (game / 'index.html').read_text(errors='replace'):
        raise ValueError('Composed HTML did not inline the H3 material')
    for index in range(len(result['rounds']), args.rounds):
        round_dir = root / f'round-{index + 1:02d}'
        if round_dir.exists():
            round_dir.rename(root / f'round-{index + 1:02d}-interrupted-{int(time.time())}')
        round_dir.mkdir()
        print(f'stage=round-{index + 1}-probe', flush=True)
        initial = probe(args, game, round_dir / 'initial')
        player = play(args, root, round_dir, game, initial)
        played = probe(args, game, round_dir / 'played', player['actions'])
        issue = inspect(args, root, round_dir, played)
        if issue['repair_side'] == 'asset' and not any(
                word in issue['issue'].lower() for word in ('background', 'plate', 'h3')):
            issue = {**issue, 'repair_side': 'code',
                     'reason': issue['reason'] +
                     ' Game-rendered visuals are repaired in HTML, not in the H3 background.'}
        row = {'round': index + 1, 'behavior_skill': player['skill'],
               'actions': player['actions'], 'issue': issue,
               'before_sha256': digest(game / 'index.html')}
        if issue['repair_side'] == 'none':
            row.update(accepted=False, reason='No evidenced issue')
        elif issue['repair_side'] == 'behavior':
            row.update(accepted=True, reason='Behavior Skill updated; game unchanged')
        else:
            backup = round_dir / 'incumbent.html'
            shutil.copyfile(game / 'index.html', backup)
            if issue['repair_side'] == 'asset' and index == 0:
                plan_path = material / 'asset_plan.json'
                plan = json.loads(plan_path.read_text())
                plan['assets'][0]['prompt'] += ' Address observed issue: ' + issue['issue'][:500]
                write_json(plan_path, plan)
                new_frame, new_record = generate_material(args, material,
                                                          round_dir / 'material-repair.log',
                                                          feedback=issue['issue'][:500])
                shutil.copyfile(new_frame, game / 'h3-scene.png')
                row['material_repair'] = {'sha256': digest(new_frame),
                                          'verdict': new_record['verdict']}
            print(f'stage=round-{index + 1}-edit', flush=True)
            edit(args, game, round_dir, issue, player['skill'])
            candidate = probe(args, game, round_dir / 'candidate', player['actions'])
            verdict = accept(args, round_dir, issue, played, candidate)
            row.update(accepted=verdict['accept'], reason=verdict['reason'])
            if not verdict['accept']:
                shutil.copyfile(backup, game / 'index.html')
        row['after_sha256'] = digest(game / 'index.html')
        result['rounds'].append(row)
        write_json(root / 'method.json', result)
    print('stage=freeze', flush=True)
    final = root / 'final-workspace'
    prepare(argparse.Namespace(bench=bench, task=args.task, output=final))
    freeze(argparse.Namespace(workspace=final, html=game / 'index.html'))
    print('stage=official_score', flush=True)
    score_error = None
    try:
        score(argparse.Namespace(workspace=final, exp_id=args.exp_id,
                                 docker=args.docker, docker_host=args.docker_host,
                                 image=args.image, timeout=1800))
    except RuntimeError as exc:
        score_error = str(exc)
    result['final_html_sha256'] = digest(game / 'index.html')
    result['evaluation'] = json.loads((final / 'manifest.json').read_text())['evaluation']
    result['score_error'] = score_error
    result['finished_at'] = time.time()
    write_json(root / 'method.json', result)
    print(json.dumps({'strict_task_success': result['evaluation'].get('strict_task_success'),
                      'l1': result['evaluation'].get('l1'),
                      'l2': result['evaluation'].get('l2'),
                      'method': str(root / 'method.json')}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bench', type=Path, required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--exp-id', required=True)
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--expected-account-hash')
    parser.add_argument('--codex', required=True)
    parser.add_argument('--model', default='gpt-6-sol')
    parser.add_argument('--codex-timeout', type=int, default=3600)
    parser.add_argument('--asset-python', required=True)
    parser.add_argument('--h3-url', default='http://127.0.0.1:30010')
    parser.add_argument('--h3-timeout', type=int, default=1200)
    parser.add_argument('--docker', required=True)
    parser.add_argument('--docker-host', required=True)
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--resume', action='store_true',
                        help='Resume an intact plan after a pre-composition environment failure')
    parser.add_argument('--resume-game', action='store_true',
                        help='Continue an existing composed HTML from its next unfinished round')
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        run(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
