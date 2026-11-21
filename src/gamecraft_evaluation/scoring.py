"""Official rubric scoring of a sealed submission, with explicit judge selection."""
import math
import os
import shlex
import shutil
import statistics
import sys
import time
from pathlib import Path

from gamecraft_behavior.core import write_json
from gamecraft_composition.runtime import run_command
from .packaging import TASK, check_frozen, content_digest, file_hash, read_json, validate_trace


def adapted_rubric(source, project, godot):
    rubric = read_json(source)
    # Fail closed if the official build contract changes. No scoring edits.
    tokens = shlex.split(rubric['build_check']['cmd'])
    if tokens != ['godot', '--headless', '--path', '/workspace/game', '--quit-after', '5']:
        raise ValueError('Unexpected official build command; review path adaptation first')
    tokens[0], tokens[3] = godot, str(project)
    rubric['build_check']['cmd'] = shlex.join(tokens)
    return rubric


def summarize(data, rubric, expected_demos):
    errors = list(data.get('errors', []))
    requirements = {item['id']: item['aggregated'] for item in data['requirements']}
    expected = {r['id'] for r in rubric['requirements']}
    if set(requirements) != expected:
        errors.append('Missing or unexpected requirement scores')
    values = [data['reward'], *requirements.values()]
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError('Invalid official score values')
    if data['build_ok'] and {d['demo_id'] for d in data['demos']} != expected_demos:
        errors.append('Not every predeclared demo was scored')
    categories = {}
    for category in rubric['categories']:
        ids = category['items']
        categories[category['name']] = (statistics.mean(requirements[k] for k in ids)
                                        if all(k in requirements for k in ids) else None)
    return {'valid': not errors, 'build_ok': data['build_ok'], 'overall': data['reward'],
            'categories': categories, 'errors': errors, 'judge': data['judge']}


def score(run, bench, godot, judge, judge_model, *, passes=1, timeout=1800):
    run, bench = Path(run).resolve(), Path(bench).resolve()
    check_frozen(run)
    if judge.strip().lower() == 'stub' or not judge_model.strip():
        raise ValueError('Official evaluation requires an explicit real judge model')
    if not 1 <= passes <= 5 or timeout < 1:
        raise ValueError('Use 1–5 scoring passes and a positive timeout')
    manifest = read_json(run / 'submission.json')
    project = run / 'submission'
    if content_digest(project) != manifest['digest']:
        raise ValueError('Sealed submission changed')
    official = bench / 'tasks' / TASK / 'tests/rubric.json'
    rubric = adapted_rubric(official, project, godot)
    if len(manifest['demos']) > rubric.get('max_demos', 10):
        raise ValueError('Submission exceeds the official demo cap')
    for entry in manifest['demos']:
        trace = validate_trace(project / 'demo_outputs' / entry['name'])
        if trace['duration_frames'] / 30 > rubric.get('max_demo_seconds', 20):
            raise ValueError('Submission exceeds official demo length')
    output = run / 'scoring'
    output.mkdir(exist_ok=False)
    shutil.copyfile(official, output / 'official-rubric.json')
    write_json(output / 'runtime-rubric.json', rubric)
    env = os.environ
    env['GAMECRAFT_BENCH_GODOT_BIN'] = godot
    # Also select the same benchmark checkout in the verifier subprocess.
    env['PYTHONPATH'] = str(bench) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    bench_hashes = {str(p.relative_to(bench)): file_hash(p)
                    for p in sorted((bench / 'gamecraft_bench').rglob('*.py'))}
    write_json(output / 'configuration.json', {
        'judge': judge, 'judge_model': judge_model, 'passes': passes,
        'godot': godot, 'godot_sha256': file_hash(godot), 'python': sys.executable,
        'benchmark': str(bench), 'benchmark_source_sha256': bench_hashes,
        'official_rubric_sha256': file_hash(official),
        'build_command_before': read_json(official)['build_check']['cmd'],
        'build_command_after': rubric['build_check']['cmd'],
        'fps': 30, 'viewport': [1280, 720], 'record_size': [854, 480],
        'frame_interval_seconds': 0.5, 'submission_digest': manifest['digest'],
        # No credentials, URLs or arbitrary environment dump.
        'judge_environment': {k: v for k, v in env.items() if k in {
            'GAMECRAFT_BENCH_JUDGE_TEMPERATURE', 'GAMECRAFT_BENCH_JUDGE_REPEATS',
            'GAMECRAFT_BENCH_JUDGE_NO_THINK', 'GAMECRAFT_BENCH_JUDGE_MAX_FRAMES',
            'GAMECRAFT_BENCH_JUDGE_PROVIDER'}},
        'usage_scope': 'verifier does not expose complete token accounting; see logs',
    })
    summaries = []
    for index in range(1, passes + 1):
        start = time.monotonic()
        directory = output / f'pass-{index:02d}'
        directory.mkdir()
        # Imports affect generated caches only, never trained game code.
        imported = run_command([godot, '--headless', '--editor', '--path', str(project), '--import'],
                               directory / 'import.log', timeout, project)
        if imported or 'SCRIPT ERROR' in (directory / 'import.log').read_text():
            raise RuntimeError(f'Godot import failed: {directory / "import.log"}')
        rc = run_command([sys.executable, '-m', 'gamecraft_bench.verifier',
                          '--project', str(project), '--rubric', str(output / 'runtime-rubric.json'),
                          '--output', str(directory / 'verifier'), '--judge', judge,
                          '--judge-model', judge_model, '--pass-threshold', '0',
                          '--fps', '30', '--width', '1280', '--height', '720',
                          '--record-width', '854', '--record-height', '480',
                          '--frame-interval-seconds', '0.5'], directory / 'verifier.log', timeout, project)
        if content_digest(project) != manifest['digest']:
            raise RuntimeError('Evaluation changed sealed game content')
        if rc:
            raise RuntimeError(f'Verifier process failed: {directory / "verifier.log"}')
        data = read_json(directory / 'verifier/breakdown.json')
        summary = summarize(data, rubric, {Path(x['name']).stem for x in manifest['demos']})
        summary.update(pass_index=index, wall_seconds=time.monotonic() - start)
        summaries.append(summary)
        write_json(output / 'passes.json', summaries)
        # Retain the failure, never silently average only successful passes.
        if not summary['valid']:
            raise RuntimeError(f'Invalid scoring pass; inspect {directory}')
    result = {'protocol': 'official rubric on zero-optimization pipeline demos',
              'scope': 'Dread Wings only; not the full benchmark mean', 'passes': summaries,
              'overall_mean': statistics.mean(p['overall'] for p in summaries),
              'overall_std_population': statistics.pstdev(p['overall'] for p in summaries),
              'categories_mean': {k: statistics.mean(p['categories'][k] for p in summaries)
                                  for k in summaries[0]['categories']},
              'scale': '0–1; multiply by 100 for percentage points'}
    write_json(output / 'summary.json', result)
    return result
