"""Prepare, generate, and score GameASG submissions without touching Godot flows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from codex_provider import add_arguments, child_environment, cli_args, from_args

DEFAULT_BENCH = Path(os.environ.get('GAMEASG_BENCH_ROOT', 'GameASG-Bench')).expanduser().resolve()
BENCH_COMMIT = '6427cb6517ce11ff350ec45fcae17b67c5aec245'
DOCS = ('target.md', 'game-spec.md', 'tdd.md')
TASK_RE = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def benchmark(path):
    path = Path(path).resolve()
    commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != BENCH_COMMIT:
        raise ValueError(f'GameASG checkout must be at {BENCH_COMMIT}; found {commit}')
    if subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain', '--untracked-files=no'], text=True).strip():
        raise ValueError('Tracked GameASG benchmark files have changed')
    return path


def public_documents(bench, task):
    if not TASK_RE.fullmatch(task):
        raise ValueError('Invalid GameASG task ID')
    root = bench / 'task' / task
    if not all((root / name).is_file() for name in DOCS):
        raise ValueError(f'Missing public documents for {task}')
    return {name: root / name for name in DOCS}


def workspace(path):
    path = Path(path).resolve()
    manifest = read_json(path / 'manifest.json')
    if manifest.get('format') != 'gameasg-code-world-v1':
        raise ValueError('Not a code_world GameASG workspace')
    for name, expected in manifest['documents'].items():
        if digest(path / name) != expected:
            raise ValueError(f'Frozen public document changed: {name}')
    return path, manifest


def prepare(args):
    bench = benchmark(args.bench)
    docs = public_documents(bench, args.task)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    for name, source in docs.items():
        shutil.copyfile(source, root / name)
    (root / 'game').mkdir()
    write_json(root / 'manifest.json', {
        'format': 'gameasg-code-world-v1', 'task': args.task, 'benchmark_commit': BENCH_COMMIT,
        'benchmark_path': str(bench), 'documents': {name: digest(root / name) for name in DOCS},
        'created_at': time.time(), 'generation': None, 'evaluation': None,
    })
    print(root)


def validate_html(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError('Submission must be a nonempty regular index.html')
    html = path.read_text(errors='replace')
    if not re.search(r'</html>\s*\Z', html, re.I):
        raise ValueError('index.html must end with </html>')
    if not re.search(r'(?:window|globalThis)\s*(?:\.\s*__gameTest|\[\s*[\'\"]__gameTest[\'\"]\s*\])\s*=', html):
        raise ValueError('Missing window.__gameTest public contract')
    for name in ('reset', 'input', 'getSnapshot', 'loadScenario'):
        if not re.search(r'\b' + name + r'\s*(?:\(|:)', html):
            raise ValueError(f'Missing public method {name}')
    for tag in re.findall(r'<(?:script|link|img|video|audio|source)\b[^>]*>', html, re.I):
        for attr in ('src', 'href', 'poster'):
            match = re.search(r'\b' + attr + r'\s*=\s*[\'\"]([^\'\"]+)', tag, re.I)
            if match and not (match.group(1).startswith(('data:', 'https://', 'http://', '#'))):
                raise ValueError(f'Local companion resource forbidden: {match.group(1)}')
    return digest(path)


def freeze(args):
    root, manifest = workspace(args.workspace)
    if manifest['generation'] is not None:
        raise ValueError('Workspace already frozen')
    source = Path(args.html).resolve()
    html_hash = validate_html(source)
    target = root / 'game/index.html'
    if target.exists():
        raise ValueError('Game output already exists')
    shutil.copyfile(source, target)
    manifest['generation'] = {'mode': 'import', 'html_sha256': html_hash, 'frozen_at': time.time()}
    write_json(root / 'manifest.json', manifest)
    print(json.dumps(manifest['generation'], indent=2))


def generate(args):
    root, manifest = workspace(args.workspace)
    if manifest['generation'] is not None or (root / 'game/index.html').exists():
        raise ValueError('Workspace already has a generated game')
    config = from_args(args)
    codex_home = Path(args.codex_home).resolve()
    if not codex_home.is_dir():
        raise ValueError('Explicit --codex-home directory is required')
    if not shutil.which(args.codex):
        raise ValueError('Codex CLI not found')
    game = root / 'game'
    for name in DOCS:
        shutil.copyfile(root / name, game / name)
    prompt = (
        'Build the complete browser game described by target.md, game-spec.md, and tdd.md. '
        'Read all three documents fully. Deliver one self-contained index.html '
        'in the current directory. Inline all first-party CSS, JS and assets. '
        'Implement window.__gameTest.reset, input, getSnapshot, and loadScenario using the '
        'same state as visible gameplay; real mouse and keyboard paths must work. '
        'Use only these public documents. Do not read benchmark tests, reports, or prior scores. '
        'Do not change the three documents. Verify that index.html is complete and ends with </html>.')
    env = child_environment(config)
    env['CODEX_HOME'] = str(codex_home)
    command = [args.codex, 'exec', '--model', args.model, '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'workspace-write', '-c', 'approval_policy="never"',
               *cli_args(config), '--cd', str(game), '--output-last-message', str(root / 'last-message.txt'), '-']
    log = root / 'generation.log'
    with log.open('w') as stream:
        result = subprocess.run(command, input=prompt, text=True, stdout=stream,
                                stderr=subprocess.STDOUT, env=env, timeout=args.timeout)
    if result.returncode:
        raise RuntimeError(f'Codex exited {result.returncode}; see {log}')
    if any(digest(root / name) != manifest['documents'][name] for name in DOCS):
        raise ValueError('Public documents changed during generation')
    html_hash = validate_html(game / 'index.html')
    manifest['generation'] = {'mode': 'codex', 'model': args.model, 'provider': config['codex_provider'],
                              'html_sha256': html_hash, 'frozen_at': time.time()}
    write_json(root / 'manifest.json', manifest)
    print(json.dumps(manifest['generation'], indent=2))


def score(args):
    root, manifest = workspace(args.workspace)
    generation = manifest['generation']
    if generation is None:
        raise ValueError('Generate or freeze a game first')
    html = root / 'game/index.html'
    if validate_html(html) != generation['html_sha256']:
        raise ValueError('Frozen HTML has changed')
    if manifest['evaluation'] is not None:
        raise ValueError('Workspace already evaluated; use a fresh workspace for another run')
    bench = benchmark(manifest['benchmark_path'])
    if not ID_RE.fullmatch(args.exp_id):
        raise ValueError('Invalid experiment ID')
    output = bench / 'output' / args.exp_id / manifest['task']
    if output.exists():
        raise ValueError(f'Official output already exists: {output}')
    if not shutil.which(args.docker):
        raise ValueError(f'Docker CLI is required for official evaluation: {args.docker}')
    from .environment import daemon_version, image_id
    runtime_args = argparse.Namespace(docker=args.docker, docker_host=args.docker_host,
                                      image=args.image or 'gamebench-env:v2.0')
    server = daemon_version(runtime_args)
    evaluator_image = image_id(runtime_args)
    if evaluator_image is None:
        raise ValueError(f'Missing evaluator image: {runtime_args.image}')
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(html, output / 'index.html')
    command = ['bash', str(bench / 'docker_test.sh'), manifest['task'], '--exp-id', args.exp_id]
    env = dict(os.environ)
    if args.docker_host:
        env['DOCKER_HOST'] = args.docker_host
    docker_dir = str(Path(shutil.which(args.docker)).resolve().parent)
    env['PATH'] = docker_dir + os.pathsep + env.get('PATH', '')
    if args.image:
        env['DOCKER_IMAGE'] = args.image
    log = root / 'evaluation.log'
    with log.open('w') as stream:
        result = subprocess.run(command, cwd=bench, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=args.timeout)
    reports = {}
    for name in ('summary-test.json', 'summary.json', 'report-l1.json', 'report-l2.json'):
        source = output / name
        if source.is_file():
            shutil.copyfile(source, root / name)
            reports[name] = digest(root / name)
    evaluation = {'exp_id': args.exp_id, 'returncode': result.returncode,
                  'html_sha256': digest(output / 'index.html'), 'reports': reports,
                  'docker_server': server, 'evaluator_image': runtime_args.image,
                  'evaluator_image_id': evaluator_image,
                  'finished_at': time.time()}
    if 'summary-test.json' in reports:
        summary = read_json(root / 'summary-test.json')
        evaluation['l1'] = summary.get('l1')
        evaluation['l2'] = summary.get('l2')
    if all(name in reports for name in ('summary-test.json', 'report-l1.json', 'report-l2.json')):
        l1 = read_json(root / 'report-l1.json')
        l2 = read_json(root / 'report-l2.json')
        first = [item for group in ('tool_checks', 'checks', 'anti_patterns', 'proximity_checks')
                 for item in l1.get(group, [])]
        second = [item for item in l2.get('checks', []) if item.get('status') != 'NOT_APPLICABLE']
        priorities = {}
        for level in ('P0', 'P1', 'P2'):
            rows = [item for item in second if item.get('level') == level]
            priorities[level] = {'passed': sum(item.get('status') == 'PASS' for item in rows),
                                 'total': len(rows)}
        evaluation['priorities'] = priorities
        evaluation['strict_task_success'] = bool(
            result.returncode == 0 and first and second
            and all(item.get('status') == 'PASS' for item in first)
            and all(item.get('status') == 'PASS' for item in second
                    if item.get('level') in ('P0', 'P1')))
    manifest['evaluation'] = evaluation
    write_json(root / 'manifest.json', manifest)
    print(json.dumps(evaluation, indent=2))
    if result.returncode:
        raise RuntimeError(f'Official evaluation failed; see {log}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='Copy only public task documents')
    p.add_argument('--bench', type=Path, default=DEFAULT_BENCH)
    p.add_argument('--task', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(func=prepare)
    p = sub.add_parser('generate', help='Generate a single HTML game with Codex')
    p.add_argument('workspace', type=Path)
    p.add_argument('--codex-home', type=Path, required=True)
    p.add_argument('--codex', default='codex')
    p.add_argument('--model', default='gpt-6-sol')
    p.add_argument('--timeout', type=int, default=3600)
    add_arguments(p)
    p.set_defaults(func=generate)
    p = sub.add_parser('freeze', help='Import and freeze an existing HTML game')
    p.add_argument('workspace', type=Path)
    p.add_argument('--html', type=Path, required=True)
    p.set_defaults(func=freeze)
    p = sub.add_parser('score', help='Run the official Docker L1/L2 evaluator')
    p.add_argument('workspace', type=Path)
    p.add_argument('--exp-id', required=True)
    p.add_argument('--image', help='Override official Docker image')
    p.add_argument('--docker', default='docker', help='Docker CLI path or command')
    p.add_argument('--docker-host', help='Docker daemon URI')
    p.add_argument('--timeout', type=int, default=1800)
    p.set_defaults(func=score)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
