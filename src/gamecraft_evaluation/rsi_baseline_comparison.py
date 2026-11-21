"""RSI-only comparison of initialization scene evidence against final replays."""

import json
import math
from pathlib import Path
import subprocess

from gamecraft_behavior.core import write_json
from gamecraft_training.package import models


def compare_baseline(group, official, *, backend, model, output, facts=None):
    """Compare visible Bootstrap milestones with one final-game replay."""
    import llm
    from gamecraft_bench.verifier.score import _sample_frames
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bootstrap = Path(group['bootstrap_video'])
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'json', str(bootstrap)],
                           capture_output=True, text=True, check=True, timeout=30)
    seconds = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Invalid Bootstrap baseline video')
    before = _sample_frames(bootstrap, output / 'bootstrap_frames',
                            duration_seconds=seconds, interval_seconds=0.5,
                            max_window_seconds=20, seed=group['id'])
    after = [Path(path) for path in official['frames'][:40]]
    if not before or not after:
        raise ValueError('Bootstrap coverage comparison needs sampled frames')
    images = []
    for prefix, frames in (('B', before), ('F', after)):
        for index, path in enumerate(frames, 1):
            label = f'{prefix}{index:02d}'
            images.append({'type': 'input_text', 'text': label})
            images.append(llm.img(str(path), width=854, jpeg_quality=80))
    schema = {'facts': {'type': 'array', 'minItems': 0, 'maxItems': 12,
               'items': {'type': 'object', 'additionalProperties': False,
                         'required': ['id', 'description', 'bootstrap_frame',
                                      'final_frame', 'covered'],
                         'properties': {'id': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'bootstrap_frame': {'type': 'string'},
                                        'final_frame': {'type': ['string', 'null']},
                                        'covered': {'type': 'boolean'}}}}}
    answer = None
    def evaluate(previous_error):
        nonlocal answer
        answer = models.call(
            'Compare actual visible scene coverage, not game code or claimed goals. '
            'B frames are the original Bootstrap recording; F frames are the final-game '
            'recording. On the first comparison, enumerate every distinct goal-relevant '
            'gameplay state, interaction and transition ACTUALLY visible in B, up to 12. '
            'A visible title or menu is a valid game mode or prerequisite even if the '
            'recording has no input events. If B contains only engine splash, loading '
            'or blank frames, return an empty facts array. '
            'A fact must directly demonstrate the supplied group goal or a visible '
            'prerequisite/result needed to prove that goal. Do not add incidental events '
            'from the recording, such as death, retry, or retained inventory, unless the '
            'group goal explicitly asks to demonstrate them. '
            'Use ids B1, B2, ... and cite a B frame for each. On a retry, evaluate exactly '
            'the supplied fixed facts and ids; do not redefine or omit them. Mark covered '
            'only when an F frame visibly shows the same interaction or resulting state. '
            'A matching scenario name, input action, code path or stated intention is not '
            'visible proof. If uncertain, mark uncovered. Do not assign benchmark scores.',
            {'group': group['id'], 'goal': group['goal'],
             'fixed_baseline_facts': facts, 'previous_validation_error': previous_error,
             'bootstrap_frame_count': len(before), 'final_frame_count': len(after)},
            images, 'bootstrap_scene_coverage', schema, tokens=2800,
            backend=backend, model=model)
        rows = answer.get('facts') if isinstance(answer, dict) else None
        if not isinstance(rows, list) or len(rows) > 12:
            raise ValueError('Bootstrap comparison returned invalid facts')
        expected = {item['id']: item for item in facts} if facts is not None else None
        if not rows and expected is not None:
            raise ValueError('Bootstrap retry omitted the required visible facts')
        if (expected is not None and (len(rows) != len(expected)
                or {item.get('id') for item in rows if isinstance(item, dict)} != set(expected))):
            raise ValueError('Bootstrap retry changed the required visible facts')
        before_labels = {f'B{i:02d}' for i in range(1, len(before) + 1)}
        after_labels = {f'F{i:02d}' for i in range(1, len(after) + 1)}
        normalized = []
        for index, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                raise ValueError('Invalid Bootstrap comparison fact')
            identity = item.get('id') if expected is not None else f'B{index}'
            description = (expected[identity]['description'] if expected is not None
                           else item.get('description'))
            baseline_frame = (expected[identity]['bootstrap_frame'] if expected is not None
                              else item.get('bootstrap_frame'))
            if isinstance(baseline_frame, str) and baseline_frame.startswith('B'):
                number = baseline_frame[1:]
                if number.isdecimal():
                    baseline_frame = f'B{int(number):02d}'
            if (not isinstance(description, str) or not description.strip()
                    or baseline_frame not in before_labels):
                raise ValueError('Bootstrap comparison has no valid baseline evidence')
            final_frame = item.get('final_frame')
            covered = item.get('covered') is True and final_frame in after_labels
            normalized.append({'id': identity, 'description': description,
                               'bootstrap_frame': baseline_frame,
                               'final_frame': final_frame if covered else None,
                               'covered': covered})
        report = {'group': group['id'], 'goal': group['goal'],
                  'bootstrap_video': str(bootstrap), 'final_video': official['video'],
                  'facts': normalized, 'passed': bool(normalized) and
                  all(item['covered'] for item in normalized),
                  'status': 'verified' if normalized else 'no_baseline_facts'}
        write_json(output / 'comparison.json', report)
        return report
    previous_error = None
    for attempt in range(1, 3):
        answer = None
        try:
            return evaluate(previous_error)
        except ValueError as exc:
            previous_error = str(exc)
            write_json(output / f'invalid_attempt_{attempt:02d}.json',
                       {'error': previous_error, 'answer': answer})
    report = {'group': group['id'], 'goal': group['goal'],
              'bootstrap_video': str(bootstrap), 'final_video': official['video'],
              'facts': [{**fact, 'final_frame': None, 'covered': False}
                        for fact in facts] if facts is not None else [],
              'passed': False, 'status': 'unverified',
              'error': 'Bootstrap comparison invalid after one retry: ' + previous_error}
    write_json(output / 'comparison.json', report)
    return report

