"""Training-free evaluation; selected benchmark runs in a dedicated subprocess."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def tree_hash(root):
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*'))
            if p.is_file() and not any(x in {'.godot', '.git', '__pycache__'} for x in p.parts)
            and p.suffix not in {'.import', '.uid'}}


def categories(rubric, values):
    return {c['name']: statistics.mean(values[k] for k in c['items'])
            for c in rubric['categories']}


def checked_response(response, ids):
    if set(response.scores) != set(ids):
        raise ValueError('Judge did not return every rubric item')
    for value in response.scores.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('Invalid judge score; refusing partial result')
    # Some upstream parsers fill missing scores with zero. Check the raw payload too.
    if response.raw:
        from gamecraft_bench.verifier.judges._common import _extract_json_object
        payload = json.loads(_extract_json_object(response.raw) or '{}')
        scores = payload.get('scores', {})
        if set(scores) != set(ids):
            raise ValueError('Raw judge answer omitted rubric items')
        for value in scores.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('Invalid raw judge score')
    return response


def run(args):
    bench, out = args.bench.resolve(), args.output.resolve()
    if args.command == 'score-project':
        from .showcase_coverage import require_ready_for_scoring
        require_ready_for_scoring(args.project)
    if Path(args.task).name != args.task or args.task in {'.', '..'}:
        raise ValueError('Task must be a benchmark task ID')
    rubric = bench / 'tasks' / args.task / 'tests/rubric.json'
    if not rubric.is_file() or not (bench / 'gamecraft_bench').is_dir():
        raise ValueError('Benchmark checkout/task not found')
    if not 1 <= args.passes <= 10 or args.timeout <= 0:
        raise ValueError('Use 1–10 passes and a positive timeout')
    if not args.judge_model.strip() or args.judge == 'stub':
        raise ValueError('An explicit real judge model is required')
    sources = [p.resolve() for p in args.videos] if args.command == 'score-videos' else [args.project.resolve()]
    for p in sources:
        if not p.exists() or p == out or out in p.parents or p in out.parents:
            raise ValueError('Inputs must exist and output must be a separate new directory')
    spec = json.loads(rubric.read_text())
    if args.command == 'score-videos':
        if len(sources) > spec.get('max_demos', 10) or len({p.stem for p in sources}) != len(sources):
            raise ValueError('Too many videos or duplicate demo names')
        if any(not p.is_file() for p in sources):
            raise ValueError('Pass explicit video files')
    else:
        if not (sources[0] / 'project.godot').is_file():
            raise ValueError('--project must point to the clean Godot game directory')
        if 'BehaviorBridge=' in (sources[0] / 'project.godot').read_text() or (sources[0] / 'behavior_bridge.gd').exists():
            raise ValueError('Use a clean game, not an instrumented runtime project')
        traces = sorted((sources[0] / 'demo_outputs').glob('*.json'))
        if not traces or len(traces) > spec.get('max_demos', 10):
            raise ValueError('Project needs 1–max_demos traces in demo_outputs; no implicit dropping')
        from .packaging import validate_trace
        for p in traces:
            validate_trace(p)
        godot = shutil.which(args.godot or os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or 'godot')
        if not godot:
            raise ValueError('Godot executable not found')
    out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(rubric, out / 'rubric.json')
    config = {'mode': args.command, 'task': args.task, 'bench': str(bench),
              'judge': args.judge, 'judge_model': args.judge_model,
              'judge_label': args.judge_label or args.judge_model, 'passes': args.passes,
              'timeout': args.timeout, 'input_label': args.input_label,
              'rubric_sha256': sha(rubric), 'benchmark_hashes': tree_hash(bench / 'gamecraft_bench'),
              'frame_interval_seconds': 0.5, 'window_seconds': spec.get('max_demo_seconds', 20),
              'padding': False, 'sources': [str(p) for p in sources]}
    if args.command == 'score-videos':
        dest = out / 'videos'; dest.mkdir()
        for p in sources:
            shutil.copyfile(p, dest / p.name)
        config['videos'] = [str(dest / p.name) for p in sources]
        config['input_hashes'] = {p.name: sha(dest / p.name) for p in sources}
        config['interpretation'] = 'Video rubric score conditional on BUILD=1; build not tested. Not a full benchmark score.'
    else:
        project = out / 'project'
        shutil.copytree(sources[0], project, ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
        config.update(project=str(project), godot=str(Path(godot).resolve()), input_hashes=tree_hash(project))
        config['interpretation'] = 'Frozen project, original demos, official rubric; no training, no padding.'
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).resolve().parents[1]), str(bench)))
    env['XDG_DATA_HOME'] = str(out / 'userdata')
    env['GAMECRAFT_BENCH_GODOT_BIN'] = config.get('godot', '')
    env.setdefault('GAMECRAFT_BENCH_JUDGE_TEMPERATURE', '0')
    env.setdefault('GAMECRAFT_BENCH_JUDGE_REPEATS', '1')
    config['judge_settings'] = {k: v for k, v in env.items() if k in {
        'GAMECRAFT_BENCH_JUDGE_TEMPERATURE', 'GAMECRAFT_BENCH_JUDGE_REPEATS',
        'GAMECRAFT_BENCH_JUDGE_NO_THINK', 'GAMECRAFT_BENCH_JUDGE_MAX_FRAMES',
        'GAMECRAFT_BENCH_JUDGE_MAX_BODY_MB', 'GAMECRAFT_BENCH_JUDGE_PROVIDER'}}
    config['evaluation_source_sha256'] = sha(Path(__file__))
    dump(out / 'configuration.json', config)
    try:
        with (out / 'evaluation.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(out)],
                                    env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                proc.wait(timeout=args.timeout)
            except BaseException:
                # Only this worker's newly-created process group, never shared services.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
    except subprocess.TimeoutExpired:
        dump(out / 'failure.json', {'type': 'TimeoutExpired', 'message': 'Evaluation exceeded the wall-clock budget'})
        raise
    if proc.returncode:
        raise RuntimeError(f'Evaluation failed; inspect {out / "evaluation.log"}')
    result = json.loads((out / 'summary.json').read_text())
    key = 'video_rubric_mean' if args.command == 'score-videos' else 'overall_mean'
    print(f'{key}: {result[key] * 100:.2f}/100\n{out / "summary.json"}')
    return result


def worker(out):
    config = json.loads((out / 'configuration.json').read_text())
    rubric = json.loads((out / 'rubric.json').read_text())
    from gamecraft_bench.verifier import score as upstream
    from gamecraft_bench.verifier.judges import get_judge
    from gamecraft_bench.verifier.judges.base import JudgeRequest, RequirementSpec
    if config['judge'] == 'codex':
        from gamecraft_evaluation.codex_judge import CodexJudge
        judge = CodexJudge(model=config['judge_model'])
    else:
        judge = get_judge(config['judge'], config['judge_model'])
    ids = [r['id'] for r in rubric['requirements']]
    original_score = judge.score
    judge.score = lambda request: checked_response(original_score(request), ids)
    video_mode = config['mode'] == 'score-videos'
    frames = []
    if video_mode:
        for name in config['videos']:
            path = Path(name)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                    '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
            duration = float(json.loads(probe.stdout)['format']['duration'])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('Invalid video duration')
            sampled = upstream._sample_frames(path, out / 'frames' / path.stem,
                        duration_seconds=duration, interval_seconds=0.5,
                        max_window_seconds=config['window_seconds'], seed=path.stem)
            if not sampled:
                raise ValueError(f'No frames from {path}')
            frames.append((path, sampled, duration))
        dump(out / 'frames.json', [{'video':str(p), 'duration':d, 'frames':[str(f) for f in fs]} for p,fs,d in frames])
    else:
        project = Path(config['project'])
        with (out / 'import.log').open('w') as log:
            cp = subprocess.run([config['godot'], '--headless', '--path', str(project), '--import', '--quit'],
                                stdout=log, stderr=subprocess.STDOUT, timeout=600)
        if cp.returncode or 'SCRIPT ERROR' in (out / 'import.log').read_text():
            raise RuntimeError('Godot import failed; this is not a valid zero score')
        import shlex
        cmd = shlex.split(rubric['build_check']['cmd'])
        if cmd != ['godot','--headless','--path','/workspace/game','--quit-after','5']:
            raise ValueError('Unsupported build command; refusing silent rubric changes')
        cmd[0],cmd[3] = config['godot'],str(project)
        rubric['build_check']['cmd'] = shlex.join(cmd)
        dump(out / 'runtime-rubric.json', rubric)
    passes = []
    for n in range(1,config['passes']+1):
        directory = out / f'pass-{n:02d}'; directory.mkdir()
        if video_mode:
            scores = {k:{} for k in ids}; logs=[]
            for path, sampled, duration in frames:
                response = judge.score(JudgeRequest(path.stem,path,sampled,
                       [RequirementSpec(r['id'],r['description']) for r in rubric['requirements']]))
                logs.append({'demo_id':path.stem,'scores':response.scores,'rationales':response.rationales,'raw':response.raw})
                dump(directory / 'judge_log.json', logs)
                for k in ids: scores[k][path.stem]=response.scores[k]
            values = {r['id']:upstream._aggregate(r.get('agg','max'),scores[r['id']]) for r in rubric['requirements']}
            value = upstream._safe_eval_formula(rubric['score_formula'], {rubric['build_check'].get('id','BUILD'):1.0,**values})
            data = {'build_ok':None,'video_rubric_score':value,'requirements':scores,'aggregated':values}
            dump(directory / 'breakdown.json',data)
        else:
            result = upstream.score_project(project_dir=project, rubric_path=out / 'runtime-rubric.json',
                         output_dir=directory, judge=judge)
            if result.errors:
                raise RuntimeError(f'Invalid evaluation pass: {result.errors}')
            if result.build_ok and len(result.demos) != len(list((project/'demo_outputs').glob('*.json'))):
                raise RuntimeError('Not all demos evaluated')
            value=result.reward;values={r.requirement_id:r.aggregated for r in result.requirements}
        passes.append({'pass':n,'score':value,'categories':categories(rubric,values)})
        dump(out/'passes.json',passes)
        print('PASS',n,value,flush=True)
    if video_mode:
        assert all(sha(Path(p))==config['input_hashes'][Path(p).name] for p in config['videos']), 'Frozen videos changed'
    else:
        assert tree_hash(project)==config['input_hashes'], 'Frozen game changed'
    dump(out/'summary.json',{'mode':config['mode'],'task':config['task'],'scale':'0–1',
        'interpretation':config['interpretation'],'build_checked':not video_mode,
        'repeat_scope':'same sampled frames, repeated judge calls' if video_mode else 'independent replay and judging',
        'judge':config['judge'],'judge_model':config['judge_model'],'judge_label':config['judge_label'],
        'passes':passes,('video_rubric_mean' if video_mode else 'overall_mean'):statistics.mean(p['score'] for p in passes),
        'std_population':statistics.pstdev(p['score'] for p in passes),
        'categories_mean':{k:statistics.mean(p['categories'][k] for p in passes) for k in passes[0]['categories']}})


if __name__ == '__main__':
    output = Path(sys.argv[1])
    try:
        worker(output)
    except Exception as exc:
        dump(output/'failure.json',{'type':type(exc).__name__,'message':str(exc)})
        raise
