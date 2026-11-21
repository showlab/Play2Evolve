"""Ten-episode, evidence-backed sustained playability pilot.

Run separately on each frozen version with the same flags. This is an agent
proxy measure, not a validated human enjoyment score.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from gamecraft_behavior.adapters import load_factory, resolve
from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.runtime import player, rollout


DIMENSIONS = ('logic', 'visual', 'gameplay')


def _frames(video: Path, output: Path, interval_seconds: float = 5) -> list[Path]:
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'json', str(video)],
                           capture_output=True, text=True, check=True, timeout=30)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'Invalid replay duration: {video}')
    output.mkdir()
    paths = []
    count = max(1, math.ceil(duration / interval_seconds))
    for index in range(count):
        at = min(duration - .05, (index + .5) * duration / count)
        path = output / f'{index + 1:02}.jpg'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-ss', str(at), '-i', str(video), '-frames:v', '1',
                        '-vf', 'scale=854:-2', str(path)], check=True, timeout=30)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f'Missing sampled frame: {path}')
        paths.append(path)
    return paths


def _validate(answer: dict) -> dict:
    if not isinstance(answer, dict):
        raise ValueError('Judge response must be an object')
    for key in DIMENSIONS:
        item = answer.get(key)
        if not isinstance(item, dict):
            raise ValueError(f'Missing {key} assessment')
        value = item.get('score')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 5:
            raise ValueError(f'Invalid {key} score')
        if not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError(f'Missing {key} reason')
    discoveries = answer.get('new_discoveries')
    if not isinstance(discoveries, list) or len(discoveries) > 5 or any(
        not isinstance(x, str) or not x.strip() for x in discoveries
    ):
        raise ValueError('Invalid discovery list')
    return answer


def _cap_repeated_gameplay(answer: dict, *, repeated: bool) -> float:
    """Return raw gameplay score and lower only an exact, unproductive repeat."""
    raw = answer['gameplay']['score']
    if repeated and not answer['new_discoveries']:
        answer['gameplay']['score'] = min(raw, 2)
        answer['gameplay']['reason'] += (
            ' Repeated a prior action sequence without a new observed discovery; capped at 2.')
    return raw


def _judge(model: str, video: Path, frames: list[Path], prior: list[dict],
           replay: dict, episode: int, evidence_dir: Path, actions: list[str]) -> dict:
    import llm
    schema = {'type': 'object', 'additionalProperties': False,
              'required': [*DIMENSIONS, 'new_discoveries'],
              'properties': {
                  **{key: {'type': 'object', 'additionalProperties': False,
                           'required': ['score', 'reason'],
                           'properties': {'score': {'type': 'number', 'minimum': 0, 'maximum': 5},
                                          'reason': {'type': 'string'}}}
                     for key in DIMENSIONS},
                  'new_discoveries': {'type': 'array', 'maxItems': 5,
                                      'items': {'type': 'string'}}}}
    context = {'episode': episode, 'previous_episodes': prior,
               'replay_verified': replay['replay_verified'],
               'completion': replay['completion'],
               'current_actions': actions, 'video': str(video)}
    windows = []
    window_schema = {'type': 'object', 'additionalProperties': False,
                     'required': ['visible_events', 'visual_changes', 'possible_bugs'],
                     'properties': {key: {'type': 'array', 'maxItems': 8,
                                         'items': {'type': 'string'}}
                                    for key in ('visible_events', 'visual_changes', 'possible_bugs')}}
    for start in range(0, len(frames), 12):
        group = frames[start:start + 12]
        content = [{'type': 'input_text', 'text':
                    f'Chronological frames {start + 1}–{start + len(group)} of {len(frames)}.'}]
        for index, frame in enumerate(group, start + 1):
            content.extend([{'type': 'input_text', 'text': f'Frame {index}'},
                            llm.img(str(frame), width=854, jpeg_quality=80)])
        observed = llm.call(model,
                            'Describe only events visible in these chronological frames. '
                            'Do not score or infer hidden game rules. Return strict JSON.',
                            content, 'playability_video_window', window_schema,
                            backend='codex', max_tokens=900)
        windows.append({'first_frame': start + 1, 'last_frame': start + len(group),
                        'observation': observed})
        write_json(evidence_dir / 'video_windows.json', windows)
    content = [{'type': 'input_text', 'text': json.dumps({**context, 'video_windows': windows})}]
    system = (
        'Evaluate one episode of an agent playing a game. The video was inspected '
        'from start to end in chronological windows. Use only those observations, '
        'current actions and verified replay status as current evidence. Previous '
        'episodes are context for detecting repetition, not proof of current events. '
        'Score 0 to 5: '
        'logic = coherent controls, feedback, progression and absence of blocking bugs; '
        'visual = meaningful visual variety, animation and readable feedback that remain '
        'engaging in THIS episode; gameplay = distinct choices, strategies or situations '
        'experienced in THIS episode and reason to continue playing. Compare the current '
        'action sequence and events with previous episodes. An identical route with no '
        'new outcome or strategic choice cannot receive gameplay above 2. '
        'A repeated mechanic may still be engaging, but do not count it as a new '
        'discovery. Death is not a bug '
        'unless the game behaves incoherently. Loading screens, black frames and unobserved '
        'features earn no credit. List at most five concrete NEW discoveries, excluding '
        'prior discoveries. Reasons must cite window numbers or their absence. Do not infer '
        'quality from source code, game title or claimed intentions. Return strict JSON.'
    )
    return _validate(llm.call(model, system, content, 'sustained_playability', schema,
                              backend='codex', max_tokens=900))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--rsi-run', type=Path, help='Completed RSI run, with its selected game and Skill')
    source.add_argument('--game-package', type=Path, help='Frozen initialization package')
    source.add_argument('--project', type=Path, help='Standalone project; supply --adapter for non-bird games')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--adapter', help='Trusted adapter factory module:factory')
    p.add_argument('--godot', default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot'))
    p.add_argument('--model', default='gpt-6-sol')
    p.add_argument('--episodes', type=int, default=10)
    p.add_argument('--step-frames', type=int, default=30)
    p.add_argument('--max-decisions', type=int, default=60,
                   help='Direct Codex decisions per episode; 60 at 30 frames is one game minute')
    p.add_argument('--frame-sample-seconds', type=float, default=5)
    p.add_argument('--scenario')
    p.add_argument('--skill', type=Path)
    args = p.parse_args()
    if not args.godot or not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        p.error('Godot, ffmpeg and ffprobe are required')
    if (not 1 <= args.episodes <= 100 or not 1 <= args.step_frames <= 30 or
            args.max_decisions < 1 or not math.isfinite(args.frame_sample_seconds) or
            args.frame_sample_seconds <= 0):
        p.error('Invalid episode or frame budget')
    if args.rsi_run:
        from gamecraft_evaluation.rsi_showcase import load_source
        from game_initialization.package import load_adapter
        source_info = load_source(args.rsi_run)
        project = source_info['project']
        adapter = load_adapter(args.rsi_run.resolve() / 'initial')
        base_skill = source_info['skill']
    elif args.game_package:
        from game_initialization.package import inspect_package, load_adapter
        package = args.game_package.resolve()
        inspect_package(package)
        project = package / 'game'
        adapter = load_adapter(package)
        base_skill = ''
    else:
        project = args.project.resolve()
        adapter = resolve(project, load_factory(args.adapter) if args.adapter else None)
        base_skill = ''
    from gamecraft_training.package.upgrade import with_generic_pointer_actions
    adapter = with_generic_pointer_actions(adapter)
    project, output = project.resolve(), args.output.resolve()
    if not (project / 'project.godot').is_file() or output.exists() or project == output or project in output.parents:
        p.error('Choose an existing project and a new, separate output directory')
    frozen = digest(project)
    if args.skill:
        base_skill = args.skill.read_text()
    output.mkdir(parents=True)
    (output / 'memory').mkdir()
    write_json(output / 'config.json', {'project': str(project), 'source_hashes': frozen,
               'task_id': adapter.task_id, 'model': args.model, 'episodes': args.episodes,
               'step_frames': args.step_frames, 'max_decisions': args.max_decisions,
               'frame_sample_seconds': args.frame_sample_seconds,
               'scenario': args.scenario, 'skill': str(args.skill) if args.skill else None,
               'rsi_run': str(args.rsi_run) if args.rsi_run else None,
               'game_package': str(args.game_package) if args.game_package else None,
               'player_mode': 'direct_codex_per_decision',
               'metric_status': 'agent_proxy_unvalidated'})
    history = []
    for index in range(1, args.episodes + 1):
        episode = output / f'episode-{index:02d}'
        # A fresh user:// store prevents saves from a previous episode or version
        # from changing the launch state. Exploration memory stays in the prompt.
        user_data = output / f'user-data-{index:02d}'
        user_data.mkdir(parents=True)
        os.environ['XDG_DATA_HOME'] = str(user_data)
        # Keep memory bounded. The same observations inform play and judging.
        prior = [{'episode': x['episode'], 'new_discoveries': x['new_discoveries'],
                  'actions': x['actions'], 'final_public_state': x['final_public_state']}
                 for x in history]
        play_memory = [
            {'episode': x['episode'], 'new_discoveries': x['new_discoveries'],
             'action_counts': dict(Counter(x['actions'])),
             'opening_actions': x['actions'][:20], 'ending_actions': x['actions'][-20:],
             'final_public_state': x['final_public_state']}
            for x in history]
        write_json(output / 'memory' / f'episode-{index:02d}.json', play_memory)
        memory = ('\nMEMORY OF YOUR PREVIOUS PLAYS OF THIS GAME: '
                  + json.dumps(play_memory, ensure_ascii=False)
                  + '\nUse the current screenshot and feedback after EVERY action. '
                  'Try a different legal route or deeper progression when possible. '
                  'If an action has no effect, change your plan. Never invent outcomes.') if play_memory else ''
        decide = player(args.model, 'codex', adapter=adapter)
        result = rollout(project, episode, base_skill + memory, decide, godot=args.godot,
                         step_frames=args.step_frames, max_decisions=args.max_decisions,
                         scenario=args.scenario, player_state='engine', adapter=adapter,
                         max_episode_frames=args.step_frames * args.max_decisions)
        if not result['replay_verified']:
            raise RuntimeError(f'Episode {index} lacks verified replay')
        actions = [x['action'] for x in result['decisions']]
        frames = _frames(Path(result['video']), episode / 'judge_frames',
                         args.frame_sample_seconds)
        answer = _judge(args.model, Path(result['video']), frames, prior, result,
                        index, episode, actions)
        signature = hashlib.sha256(json.dumps(actions).encode()).hexdigest()
        repeated = any(x['action_signature'] == signature for x in history)
        gameplay_raw = _cap_repeated_gameplay(answer, repeated=repeated)
        row = {'episode': index, **{k: answer[k]['score'] for k in DIMENSIONS},
               'score': sum(answer[k]['score'] for k in DIMENSIONS) / len(DIMENSIONS),
               'gameplay_raw': gameplay_raw, 'repeated_actions': repeated,
               'action_signature': signature, 'actions': actions,
               'final_public_state': result['final_player_state'],
               'sampled_frames': len(frames),
               'new_discoveries': answer['new_discoveries'], 'rationales':
               {k: answer[k]['reason'] for k in DIMENSIONS},
               'replay_verified': result['replay_verified'], 'video': result['video'],
               'trace': result['trace'], 'player_mode': 'direct_codex_per_decision',
               'memory_file': str(output / 'memory' / f'episode-{index:02d}.json'),
               'result': str(episode / 'result.json')}
        history.append(row)
        write_json(episode / 'assessment.json', row)
        write_json(output / 'curve.json', {'episodes': history,
                   'mean': sum(x['score'] for x in history) / len(history),
                   'late_mean': sum(x['score'] for x in history[-3:]) / min(3, len(history))})
        with (output / 'curve.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['episode', *DIMENSIONS, 'score', 'replay_verified', 'video'])
            writer.writeheader()
            writer.writerows({key: row[key] for key in writer.fieldnames} for row in history)
        if digest(project) != frozen:
            raise RuntimeError('Source project changed during experiment')
        print(f'[playability] episode={index} score={row["score"]:.2f} video={row["video"]}', flush=True)


if __name__ == '__main__':
    main()
