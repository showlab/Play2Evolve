"""Fast post-RSI scoring collection from existing, hash-checked input traces."""
from __future__ import annotations

from pathlib import Path
import shutil

from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase
from .packaging import validate_trace
from .trained_demos import collect_official, file_hash


def select_traces(initial, rsi, limit=final_showcase.MAX_DEMOS):
    """Keep RSI evidence and a deterministic spread of initialization routes."""
    if len(rsi) > limit:
        raise ValueError('RSI playtest traces exceed the benchmark demo limit')
    slots = limit - len(rsi)
    if slots == 0:
        return rsi
    default = next((demo for demo in initial if demo['scenario'] is None), None)
    candidates = [demo for demo in initial if demo is not default]
    selected = [default] if default else []
    slots -= len(selected)
    if slots > 0 and candidates:
        if len(candidates) <= slots:
            selected.extend(candidates)
        elif slots == 1:
            selected.append(candidates[-1])
        else:
            # Preserve early and late gameplay without reading the hidden rubric.
            indices = {round(i * (len(candidates) - 1) / (slots - 1))
                       for i in range(slots)}
            selected.extend(candidates[i] for i in sorted(indices))
    return selected + rsi


def run_score_only(source, output, bench, godot):
    """Officially replay at most ten existing inputs on the selected RSI game."""
    from .rsi_showcase import reusable_demos, reusable_rsi_playtests

    output, bench, godot = Path(output), Path(bench), Path(godot)
    project = source['project']
    fingerprint = digest(project)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'status.json', {'state': 'collecting'})
    write_json(output / 'configuration.json', {
        'protocol': 'post-rsi-score-only-v1', 'showcase_mode': 'score_only',
        'run': str(source['run']), 'task_id': source['config']['task_id'],
        'project_source': str(project), 'project_hashes': fingerprint,
        'input_metadata_hashes': source['metadata_hashes'],
        'max_demos': final_showcase.MAX_DEMOS,
        'selection': 'RSI core and upgrade inputs, normal launch, and a spread of initialization routes',
        'coverage_judge_used': False,
    })
    initial = reusable_demos(source)
    rsi = reusable_rsi_playtests(source, output, fingerprint)
    chosen = select_traces(initial, rsi)
    if not chosen:
        raise ValueError('No reusable initialization or RSI playtest traces')
    submission = output / 'project'
    shutil.copytree(project, submission,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__', 'demo_outputs'))
    source_without_demos = {name: value for name, value in fingerprint.items()
                            if not name.startswith('demo_outputs/')}
    if digest(submission) != source_without_demos:
        raise RuntimeError('Selected RSI game changed while freezing the scoring project')
    dest_dir = submission / 'demo_outputs'
    dest_dir.mkdir()
    entries = []
    for index, demo in enumerate(chosen, 1):
        if file_hash(demo['path']) != demo['sha256']:
            raise RuntimeError('Reusable input trace changed before scoring')
        dest = dest_dir / f'{index:02d}_{Path(demo["file"]).stem}.json'
        shutil.copyfile(demo['path'], dest)
        validate_trace(dest)
        entries.append({'file': dest.name, 'source': demo.get('source', 'initialization_trace'),
                        'source_file': demo.get('source_file', str(demo['path'])),
                        'scenario': demo['scenario'], 'sha256': file_hash(dest)})
    write_json(output / 'demo_provenance.json', {
        'selection': 'deterministic_spread', 'selected': entries,
        'omitted_initialization_routes': [d['file'] for d in initial if d not in chosen],
        'coverage_assessed': False, 'final_game_replay_required': True,
    })
    # This is the same clean BUILD and OS-input replay used by the full pathway.
    evidence = collect_official(submission, output, bench, godot)
    if (digest(project) != fingerprint or digest(submission) != {
            **source_without_demos,
            **{f'demo_outputs/{e["file"]}': e['sha256'] for e in entries}}
            or any(file_hash(Path(path)) != sha
                   for path, sha in source['metadata_hashes'].items())
            or any(file_hash(d['path']) != d['sha256'] or
                   (d.get('source_file') and
                    file_hash(Path(d['source_file'])) != d['source_sha256']) or
                   (d.get('source_result') and
                    file_hash(Path(d['source_result'])) != d['source_result_sha256'])
                   for d in chosen)):
        raise RuntimeError('RSI source or exported traces changed during collection')
    result = {
        'state': 'ready_for_judging', 'run': str(source['run']),
        'project': str(submission), 'collection': str(output / 'collection'),
        'showcase_mode': 'score_only', 'replay_complete': True,
        'coverage_complete': None, 'coverage_assessed': False,
        'official_benchmark_metric': False, 'selected_demo_count': len(entries),
        'initialization_demo_count': sum(e['source'] == 'initialization_trace' for e in entries),
        'rsi_playtest_demo_count': sum(e['source'] == 'rsi_playtest_trace' for e in entries),
        'build_ok': evidence['build_ok'],
    }
    write_json(output / 'result.json', result)
    write_json(output / 'status.json', result)
    return submission
