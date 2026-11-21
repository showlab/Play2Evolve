"""Continue RSI upgrades from selected versions, without rerunning initialization."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from game_initialization.package import SKIP, inspect_package, manifest_hash, seal_without_replay
from gamecraft_behavior.core import write_json
from .config import load


def read(path):
    return json.loads(Path(path).read_text())


def owned_path(root, value, recorded_root):
    """Rebase run artifacts across mount points; never guess by basename."""
    path = Path(value)
    if path.is_absolute():
        path = path.relative_to(recorded_root)
    result = (root / path).resolve()
    if root not in result.parents:
        raise ValueError(f'Artifact outside RSI run: {value}')
    return result


def resolve_run(source):
    root = Path(source).resolve()
    info = read(root / 'result.json')
    if info.get('kind') == 'rsi_cycles':
        if not info.get('last_run'):
            raise ValueError('Cycle queue has no completed RSI run')
        root = owned_path(root, info['last_run'], Path(info['output']))
        info = read(root / 'result.json')
    if (info.get('profile') != 'rsi' or info.get('rsi_mode') != 'upgrade'
            or info.get('state') not in ('completed', 'completed_with_errors')):
        raise ValueError('Continuation requires a finished RSI upgrade run')
    return root, info


def planning_history(package):
    path = Path(package) / 'rsi_lineage.json'
    return read(path).get('cycles', [])[-5:] if path.is_file() else []


def save_selected_video_skill(output, generator):
    """Keep a run-owned snapshot even when the editor uses an external job queue."""
    from gamecraft_video_skill import copy_skill
    workspace = Path(output) / 'selected_skills'
    return str(copy_skill(generator.root / 'video_skill', workspace))


def _video_source(root, info, recorded_root):
    if info.get('selected_video_skill'):
        source = owned_path(root, info['selected_video_skill'], recorded_root)
        if not source.is_dir():
            raise ValueError('Selected Video Skill snapshot is missing')
        return source
    enabled = read(root / 'run.json').get('video_skill')
    if not enabled:
        inherited = root / 'initial/video_skill'
        return inherited if inherited.is_dir() else None
    # Compatibility with older runs: follow only the selected candidate ancestry.
    records = []
    for path in (root / 'full_build/attempts.json', root / 'history.json'):
        if path.is_file():
            records.extend(read(path))
    current = info['project']
    for row in reversed(records):
        if row.get('candidate') != current:
            continue
        if (row.get('status') not in ('playable', 'provisional')
                and row.get('action') != 'accept_repair'):
            continue
        workspace = row.get('code_edit', {}).get('generation_workspace')
        if workspace:
            source = owned_path(root, workspace, recorded_root) / 'video_skill'
            if source.is_dir():
                return source
            raise ValueError('Selected material workspace lacks its Video Skill snapshot')
        current = row.get('project_before', info['original_project'])
    for source in (root / 'generation/video_skill', root / 'initial/video_skill'):
        if source.is_dir():
            return source
    raise ValueError('Cannot recover the enabled Video Skill; refusing to reset it silently')


def export_checkpoint(source, output):
    """Freeze the selected game and skills; this is NOT another quality acceptance."""
    root, info = resolve_run(source)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    if output == root or output in root.parents or root in output.parents:
        raise ValueError('Checkpoint cannot replace its source run')
    recorded_root = Path(info['final_review']).parent
    path = lambda value: owned_path(root, value, recorded_root)
    review = read(path(info['final_review']))
    selected = review.get('selected')
    expected = info.get('working_project') if selected == 'upgrade' else info.get('original_project')
    if (selected not in ('upgrade', 'original') or not expected
            or review.get('status') != info.get('upgrade_status')
            or path(info['project']) != path(expected)
            or (selected == 'upgrade' and info.get('upgrade_status') != 'accepted')
            or (selected == 'original' and info.get('upgrade_status') not in ('not_run', 'retained_original'))):
        raise ValueError('RSI result disagrees with final selection')
    if selected == 'upgrade' and info.get('full_build_status', 'playable') != 'playable':
        raise ValueError('Selected build was not playable')
    initial = root / 'initial'
    metadata, _, _ = inspect_package(initial)
    plan = read(path(info['selected_asset_plan']))
    config = read(path(info['selected_training']))
    skill = path(info['skill'])
    video = _video_source(root, info, recorded_root)
    project = path(info['project'])
    if ((project / 'behavior_bridge.gd').exists()
            or 'BehaviorBridge=' in (project / 'project.godot').read_text()):
        raise ValueError('Selected game still contains the development bridge')
    for tree in (project, video):
        if tree and any(p.is_symlink() for p in tree.rglob('*')):
            raise ValueError('Selected artifacts must not contain symlinks')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.rsi-checkpoint-', dir=output.parent) as tmp:
        draft = Path(tmp) / 'draft'
        shutil.copytree(initial, draft, ignore=shutil.ignore_patterns(*SKIP))
        for name in ('game', 'checks', 'video_skill'):
            shutil.rmtree(draft / name, ignore_errors=True)
        shutil.copytree(project, draft / 'game', ignore=shutil.ignore_patterns(*SKIP))
        write_json(draft / 'asset_plan.json', plan)
        write_json(draft / 'training.json', config)
        write_json(draft / 'asset_bindings.json', config['assets']['bindings'])
        shutil.copyfile(skill, draft / 'behavior_skill.txt')
        if video:
            shutil.copytree(video, draft / 'video_skill')
        lineage_path = initial / 'rsi_lineage.json'
        lineage = read(lineage_path).get('cycles', []) if lineage_path.exists() else []
        lineage.append({'cycle': len(lineage) + 1, 'source_run': str(root),
                        'input_manifest_sha256': manifest_hash(metadata),
                        'upgrade_status': info['upgrade_status'], 'state': info['state'],
                        'goal': read(path(info['upgrade_plan'])),
                        'rounds_attempted': info.get('rounds_attempted'),
                        'errors': info.get('errors', []),
                        'behavior_skill_verified': info.get('behavior_skill_verified', False)})
        write_json(draft / 'rsi_lineage.json', {'schema_version': 1, 'cycles': lineage})
        metadata = {k: v for k, v in metadata.items() if k not in ('files', 'verification')}
        metadata.update(state='draft', version=f'rsi-cycle-{len(lineage):03}',
                        source_rsi_run=str(root))
        write_json(draft / 'initialization.json', metadata)
        _, adapter, cases = inspect_package(draft, sealed=False)
        load(draft, adapter, cases, profile='rsi')
        sealed = seal_without_replay(draft, Path(tmp) / 'frozen')
        sealed.rename(output)
    return output


def run_cycles(package, output, *, cycles=1, continue_from=None, runner=None, **options):
    """Each cycle replans an upgrade; existing per-cycle rounds remain unchanged."""
    if type(cycles) is not int or cycles < 1:
        raise ValueError('RSI cycles must be a positive integer')
    if options.get('profile', 'rsi') != 'rsi' or options.get('rsi_mode', 'upgrade') != 'upgrade':
        raise ValueError('RSI cycles require profile=rsi and rsi_mode=upgrade')
    if bool(package) == bool(continue_from):
        raise ValueError('Specify exactly one package or completed RSI run')
    if continue_from and any(options.get(k) is not None for k in
                             ('initial_behavior_skill', 'global_video_skill', 'local_video_skill')):
        raise ValueError('Continuation inherits skills; do not override its starting skills')
    if runner is None:
        from .loop import run
        runner = run
    out = Path(output).resolve()
    if out.exists():
        raise FileExistsError(out)
    if package:
        source = Path(package).resolve()
        inspect_package(source)
    else:
        source, _ = resolve_run(continue_from)
        # Resume the previous run's enabled learning module, not just its files.
        if read(source / 'run.json').get('video_skill'):
            options['video_skill'] = True
    if source == out or source in out.parents or out in source.parents:
        raise ValueError('Cycle output and input must be separate directories')
    out.mkdir(parents=True)
    summary = {'kind': 'rsi_cycles', 'profile': 'rsi', 'output': str(out),
               'state': 'running', 'cycles_requested': cycles, 'cycles': [],
               'last_run': None, 'package': None, 'errors': []}
    write_json(out / 'result.json', summary)
    try:
        if continue_from:
            source = export_checkpoint(source, out / 'starting-package')
        for number in range(1, cycles + 1):
            kwargs = dict(options, profile='rsi', rsi_mode='upgrade')
            if continue_from or number > 1:
                kwargs['initial_behavior_skill'] = str(source / 'behavior_skill.txt')
                kwargs['global_video_skill'] = kwargs['local_video_skill'] = None
            elif kwargs.get('initial_behavior_skill') is None and (source / 'behavior_skill.txt').is_file():
                kwargs['initial_behavior_skill'] = str(source / 'behavior_skill.txt')
            directory = out / f'cycle-{number:03}'
            write_json(out / 'progress.json', {'state': 'running', 'cycle': number,
                                              'run': str(directory), 'input_package': str(source)})
            result = runner(str(source), str(directory), **kwargs)
            checkpoint = export_checkpoint(directory, out / f'package-{number:03}')
            summary['cycles'].append({'cycle': number, 'run': str(directory),
                                      'input_package': str(source), 'package': str(checkpoint),
                                      'state': result['state'], 'upgrade_status': result['upgrade_status']})
            summary['errors'].extend({'cycle': number, 'detail': e} for e in result.get('errors', []))
            summary.update(last_run=str(directory), package=str(checkpoint))
            write_json(out / 'result.json', summary)
            source = checkpoint
        summary['state'] = ('completed_with_errors' if summary['errors'] or any(
            row['state'] != 'completed' for row in summary['cycles']) else 'completed')
    except Exception as exc:
        summary['state'] = 'failed'
        summary['errors'].append({'error': f'{type(exc).__name__}: {exc}'})
        raise
    finally:
        write_json(out / 'result.json', summary)
        write_json(out / 'progress.json', {'state': summary['state'], 'last_run': summary['last_run'],
                                          'package': summary['package']})
    return summary
