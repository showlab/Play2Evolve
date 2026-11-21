"""Godot build/replay evaluator and optional existing-model proposal adapter."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from .core import sources, write_json

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['reason', 'edits'],
          'properties': {'reason': {'type': 'string'}, 'edits': {'type': 'array', 'items': {
              'type': 'object', 'additionalProperties': False, 'required': ['path', 'old', 'new'],
              'properties': {k: {'type': 'string'} for k in ('path', 'old', 'new')}}}}}
SYSTEM = '''You repair the COMPOSITION of an existing Godot 4 game after a gameplay rollout.
Use the task, diagnosed problem, runtime evidence and existing code. Fix scene layout,
animation timing, asset integration, input/UI feedback, or the code connecting them.
Do not alter objectives, difficulty, damage, scoring, win conditions, or invent success.
Do not change assets, player skill, input traces, test code, or the evaluation criteria.
Return at most 8 small exact text replacements in existing project.godot/.gd/.tscn/.tres files.
Each old string must match exactly once. No shell commands, new files or deletions.
Preserve all unrelated behavior. Explain how the edit addresses the supplied problem.
'''


def proposer(model, backend):
    def propose(project, feedback, evaluation, history):
        import llm  # reuse model transport lazily; no change to existing module
        request = {'feedback': feedback, 'evaluation': evaluation, 'previous_trials': history,
                   'sources': sources(project)}
        content = [{'type': 'input_text', 'text': json.dumps(request, ensure_ascii=False)}]
        for frame in evaluation.get('frames', [])[:6]:
            content.append(llm.img(frame))
        return llm.call(model, SYSTEM, content, 'godot_composition_patch', SCHEMA,
                        backend=backend, max_tokens=6000)
    return propose


def run_command(command, log, timeout, cwd=None):
    with Path(log).open('w') as stream:
        proc = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                cwd=cwd, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise RuntimeError(f'Command timed out; see {log}')


class Evaluator:
    def __init__(self, godot, judge='openai', judge_model=None, timeout=600):
        self.godot, self.judge, self.judge_model, self.timeout = godot, judge, judge_model, timeout

    def __call__(self, project, output, feedback):
        project, output = Path(project).resolve(), Path(output).resolve()
        output.mkdir(parents=True, exist_ok=False)
        traces = sorted((project / 'demo_outputs').glob('*.json'))
        if not 1 <= len(traces) <= 10:
            raise ValueError('Provide 1–10 fixed demo_outputs/*.json traces')
        for trace in traces:
            data = json.loads(trace.read_text())
            duration = data.get('duration_frames', 0)
            if not isinstance(duration, int) or not 1 <= duration <= 600:
                raise ValueError('Each trace must last 1–600 frames')
            frames = [e['frame'] for e in data.get('events', [])]
            if any(not isinstance(f, int) or not 0 <= f <= duration for f in frames) or frames != sorted(frames):
                raise ValueError('Trace events must be ordered and within duration')
        rc = run_command([self.godot, '--headless', '--editor', '--path', str(project), '--import'],
                         output / 'import.log', self.timeout, project)
        if rc or 'SCRIPT ERROR' in (output / 'import.log').read_text():
            return {'build_ok': False, 'errors': ['Godot import failed'], 'num_demos': 0, 'scores': {}}
        # This rubric is constructed ONLY from caller-supplied internal feedback.
        # Never load a benchmark task's tests/rubric.json or solution directory.
        criteria = [{'id': c['id'], 'description': c['description'], 'agg': 'mean'} for c in feedback['criteria']]
        rubric = {'build_check': {'id': 'BUILD', 'cmd': shlex.join([
            self.godot, '--headless', '--path', str(project), '--quit-after', '5'])},
            'requirements': criteria, 'score_formula': 'BUILD * (' + '+'.join(c['id'] for c in criteria) + f')/{len(criteria)}',
            'max_demos': 10, 'max_demo_seconds': 20}
        write_json(output / 'internal-rubric.json', rubric)
        command = [sys.executable, '-m', 'gamecraft_bench.verifier', '--project', str(project),
                   '--rubric', str(output / 'internal-rubric.json'), '--output', str(output / 'replay'),
                   '--judge', self.judge, '--frame-interval-seconds', '2', '--pass-threshold', '0']
        if self.judge_model:
            command += ['--judge-model', self.judge_model]
        rc = run_command(command, output / 'verifier.log', self.timeout, project)
        path = output / 'replay/breakdown.json'
        if rc or not path.exists():
            return {'build_ok': False, 'errors': ['Verifier process failed'], 'num_demos': 0, 'scores': {}}
        data = json.loads(path.read_text())
        errors = list(data['errors'])
        if len(data['demos']) != len(traces):
            errors.append('Not all fixed traces were replayed')
        # Godot can print script errors while returning exit code 0.
        for log in [output / 'replay/build.log', *(output / 'replay/demos').glob('*/logs/godot.log')]:
            if log.exists() and 'SCRIPT ERROR' in log.read_text():
                errors.append(f'Script error in {log}')
        result = {'build_ok': data['build_ok'], 'errors': errors, 'num_demos': len(data['demos']),
                  'scores': {r['id']: r['aggregated'] for r in data['requirements']},
                  'frames': [f for demo in data['demos'] for f in demo['frames']],
                  'judge': data['judge'], 'artifacts': str(output)}
        write_json(output / 'evaluation.json', result)
        return result
