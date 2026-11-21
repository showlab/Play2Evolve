"""Continue an ASG game with one full upgrade followed by three repair rounds.

This entrypoint owns its checkpoint and never rewrites the source initialization
or RSI workspace. Shared HTML/material/Player services retain their old defaults.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from codex_provider import child_environment, from_args
from . import contracts as c
from . import initialization as init
from .__main__ import BENCH_COMMIT, benchmark, public_documents, read_json, write_json, score
from .environment import preflight
from .rsi_services import RSIServices, build_candidate, validate_goal

FORMAT = 'gameasg-rsi-build-repair-v1'
ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


def source_checkpoint(root, task, documents):
    root = Path(root).resolve()
    checkpoint = root / ('rsi.json' if (root / 'rsi.json').exists() else 'initialization.json')
    state = read_json(checkpoint)
    if state.get('format') not in (init.FORMAT, FORMAT):
        raise ValueError('Source must be a grouped ASG initialization or RSI workspace')
    if state.get('status') not in ('bootstrapped', 'completed', 'completed_with_coverage_gaps',
                                    'completed_with_errors'):
        raise ValueError('Source run has not finished; do not train from an active checkpoint')
    if state['protocol']['task'] != task or state['protocol']['documents'] != documents:
        raise ValueError('Source task/public documents do not match the requested RSI task')
    if state['protocol']['benchmark_commit'] != BENCH_COMMIT:
        raise ValueError('Source benchmark revision does not match')
    version = (root / state['current']).resolve()
    if not version.is_relative_to(root):
        raise ValueError('Source version escapes its workspace')
    if init.version_hash(version) != state['current_hash']:
        raise ValueError('Source selected version has changed since checkpoint')
    return state, version


def inherited_policy(state):
    """Carry useful experience even when initialization skipped final-playtests."""
    evidence = dict(state.get('bootstrap_evidence', {}))
    for row in state.get('rounds', []):
        if row.get('selected_evidence'):
            evidence[row.get('group', 'rsi')] = row['selected_evidence']
    evidence.update(state.get('final_evidence', {}))
    if state.get('selected_evidence'):
        evidence['rsi'] = state['selected_evidence']
    policies = [ev['policy'] for ev in evidence.values() if ev.get('policy')]
    return {'policy': policies[-1]['policy'] if policies else '',
            'skill': '\n\n'.join(dict.fromkeys(p['skill'] for p in policies if p.get('skill')))}


def reusable_baseline(source, state, selected_hash, entry):
    """Use verified normal-entry evidence from the exact selected source version."""
    candidates = []
    if state.get('selected_evidence'):
        candidates.append(state['selected_evidence'])
    final = state.get('final_evidence') or {}
    candidates.extend([final[entry['id']]] if entry['id'] in final else [])
    candidates.extend(value for key, value in final.items() if key != entry['id'])
    for row in reversed(state.get('rounds', [])):
        if row.get('selected_evidence'):
            candidates.append(row['selected_evidence'])
    for evidence in candidates:
        if evidence.get('version_hash') != selected_hash:
            continue
        group = evidence.get('group') or {}
        trace = evidence.get('trace') or {}
        if group.get('launch', 'scenario') is not None or trace.get('launch') is not None:
            continue
        if trace.get('errors') or not trace.get('frames') or not evidence.get('policy'):
            continue
        path = Path(evidence.get('path', '')).resolve()
        if not path.is_relative_to(source) or not (path / 'replay/video.mp4').is_file():
            continue
        artifacts = evidence.get('artifacts') or {}
        if not artifacts:
            continue
        base = path.parent
        if any(not (base / name).resolve().is_relative_to(base) or
               not (base / name).is_file() or init.digest(base / name) != sha
               for name, sha in artifacts.items()):
            continue
        return evidence
    return None


class RSIEngine(init.Engine):
    def __init__(self, args, services, documents):
        self.args, self.services, self.documents = args, services, documents
        self.root = Path(args.output).resolve()
        source = Path(args.source).resolve()
        if self.root == source or self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise ValueError('RSI output and source must be separate workspaces')
        self.state_path = self.root / 'rsi.json'
        doc_hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in documents.items()}
        original, version = source_checkpoint(source, args.task, doc_hashes)
        self.protocol = {key: getattr(args, key) for key in (
            'task', 'rounds', 'model', 'play_seconds', 'max_actions', 'material_rounds',
            'material_candidates', 'h3_steps', 'expected_account_hash')}
        self.protocol.update(documents=doc_hashes, provider=from_args(args), benchmark_commit=BENCH_COMMIT,
                             image_id=getattr(args, 'image_id', None), source=str(source),
                             source_hash=original['current_hash'], execution_mode='build_repair')
        if self.state_path.exists():
            self.state = read_json(self.state_path)
            if not args.resume or self.state.get('format') != FORMAT or self.state['protocol'] != self.protocol:
                raise ValueError('RSI resume requires the same source and frozen settings')
            for relative, identity in [('versions/source', self.protocol['source_hash']),
                                       (self.state['current'], self.state['current_hash'])]:
                path = (self.root / relative).resolve()
                if not path.is_relative_to(self.root) or init.version_hash(path) != identity:
                    raise ValueError('RSI selected/source snapshot was modified')
        else:
            if args.resume:
                raise ValueError('No RSI checkpoint to resume')
            dest = self.root / 'versions/source'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(version, dest)
            if init.version_hash(dest) != original['current_hash']:
                raise ValueError('RSI source copy failed identity validation')
            self.state = dict(format=FORMAT, protocol=self.protocol, status='planning', started_at=time.time(),
                              current='versions/source', current_hash=original['current_hash'], rounds=[],
                              source_status=original['status'],
                              prior_policy=inherited_policy(original),
                              goal_history=(original.get('goal_history', []) +
                                            ([original['goal']] if original.get('goal') else []))[-5:])
            self.check_version(dest)
            self.save()

    def candidate(self, current, plan, candidate, group, before, directory, feedback):
        dest = self.root / 'versions' / directory.name
        updated = c.apply_candidate(plan, candidate, self.documents, self.args.task)
        if dest.exists():
            dest.rename(dest.with_name(dest.name + '.interrupted-' + str(time.time_ns())))
        shutil.copytree(current, dest)
        write_json(dest / 'plan.json', updated)
        self.services.materials(dest, updated, only=[a['id'] for a in candidate['materials']],
                                feedback=json.dumps(feedback))
        self.services.build(dest, updated, candidate['instructions'], feedback)
        self.check_version(dest)
        evidence = self.play(dest, group, directory / 'playtest', prior=before['policy'], changes=candidate)
        if evidence['trace']['errors']:
            raise ValueError('Candidate browser runtime/Player failed; keep incumbent')
        return dest, evidence

    def full_build(self, group):
        if 'full_build' in self.state:
            return
        source = self.root / 'versions/source'
        plan = read_json(source / 'plan.json')
        before = self.state['baseline']
        attempts = self.state.setdefault('build_attempts', [])
        for number in range(len(attempts) + 1, 3):
            out = self.root / 'full-build' / f'attempt-{number:02d}'
            init.fresh_directory(out)
            self.state['status'] = 'full-build'; self.save()
            print(f'[ASG RSI] full build attempt {number}/2', flush=True)
            record = {'attempt': number, 'status': 'error'}
            try:
                dest, after = self.candidate(source, plan, build_candidate(self.state['goal']), group,
                    before, out, {'goal': self.state['goal'], 'previous_attempts': attempts})
                review = self.services.review_build(self.state['goal'], before, after, out)
                record['review'] = review
                if review['core_playable'] and review['evidence_valid'] and not review['critical_regression']:
                    self.select_version(dest)
                    self.state['selected_evidence'] = after
                    record['status'] = 'playable'
                else:
                    record['status'] = 'rejected_unplayable'
            except ERRORS as exc:
                record['error'] = str(exc)
            attempts.append(record)
            write_json(out / 'result.json', record)
            # Commit acceptance and build completion together for resumability.
            if record['status'] == 'playable':
                self.state['full_build'] = 'playable'
            self.save()
            if self.state.get('full_build') == 'playable':
                return
        self.state['full_build'] = 'failed'; self.save()

    def repair_round(self, ordinal, group):
        out = self.root / 'rounds' / f'round-{ordinal:02d}'
        init.fresh_directory(out)
        self.state['status'] = f'round-{ordinal:02d}'; self.save()
        print(f'[ASG RSI] repair round {ordinal}/{self.args.rounds}', flush=True)
        current = self.root / self.state['current']
        before = self.state['selected_evidence']
        record = dict(round=ordinal, group=group['id'], before_hash=self.state['current_hash'],
                      status='reviewed', failures={})
        options, versions = {'current': before}, {}
        verdict = {'winner': 'current', 'reason': 'No viable candidate; retain incumbent'}
        try:
            plan = read_json(current / 'plan.json')
            # As in Craft RSI, the Player gets another chance to use the last
            # reflection before diagnosis, including when the game did not change.
            before = self.play(current, group, out / 'playtest', prior=before['policy'],
                               changes={'goal': self.state['goal'], 'round': ordinal})
            options['current'] = before
            diagnosis = self.services.diagnose(current, plan, group, before, out)
            write_json(out / 'diagnosis.json', diagnosis)
            known = {r['id'] for r in plan['requirements']}
            if any(not set(i['requirement_ids']) <= known for i in diagnosis['issues']):
                raise ValueError('Diagnosis referenced unknown requirements')
            issues = c.diagnosis_issue_ids(diagnosis['issues'])
            repair = self.services.repair(current, plan, diagnosis, out)
            c.validate_repair_targets(repair, issues)
            write_json(out / 'repair-plan.json', repair)
            for candidate in repair['candidates']:
                name = candidate['id']
                directory = out / f'round-{ordinal:02d}-{name}'
                directory.mkdir()
                try:
                    dest, after = self.candidate(current, plan, candidate, group, before, directory, diagnosis)
                    versions[name], options[name] = dest, after
                except ERRORS as exc:
                    record['failures'][name] = str(exc)
            if versions:
                verdict = self.services.select(group, diagnosis, options, out)
                if verdict['winner'] not in options:
                    raise ValueError('Judge selected an unavailable candidate')
        except ERRORS as exc:
            record['failures']['round'] = str(exc)
            verdict = {'winner': 'current', 'reason': 'Round unavailable; keep incumbent'}
        if verdict['winner'] != 'current':
            self.select_version(versions[verdict['winner']])
        self.state['selected_evidence'] = options[verdict['winner']]
        record.update(selection=verdict, after_hash=self.state['current_hash'],
                      selected_evidence=self.state['selected_evidence'])
        if record['failures']:
            record['status'] = 'completed_with_errors'
        self.state['rounds'].append(record)
        write_json(out / 'result.json', record)
        self.save()
        print(f'[ASG RSI] round {ordinal}: {verdict["winner"]}, {record["status"]}', flush=True)

    def run(self):
        if not self.state['status'].startswith('completed'):
            source = self.root / 'versions/source'
            plan = read_json(source / 'plan.json')
            if 'baseline' not in self.state:
                entry = next(g for g in plan['groups'] if g['launch'] is None)
                original = read_json(Path(self.protocol['source']) /
                                     ('rsi.json' if (Path(self.protocol['source']) / 'rsi.json').exists()
                                      else 'initialization.json'))
                prior = reusable_baseline(Path(self.protocol['source']), original,
                                          self.protocol['source_hash'], entry)
                if prior:
                    self.state['baseline'] = prior
                    self.state['baseline_source'] = 'source_playtest'
                    print('[ASG RSI] reused verified source baseline playtest', flush=True)
                else:
                    self.state['baseline'] = self.play(source, entry, self.root / 'baseline-playtest',
                                                       prior=self.state['prior_policy'])
                    self.state['baseline_source'] = 'new_playtest'
                self.save()
            if 'goal' not in self.state:
                goal = self.services.upgrade_goal(source, plan, self.state['baseline'],
                    self.root / 'planning', self.state['goal_history'])
                self.state['goal'] = validate_goal(goal, plan, self.documents, self.args.task)
                self.save()
            self.services.goal = self.state['goal']
            group = dict(id='rsi', launch=None, requirement_ids=self.state['goal']['requirement_ids'],
                         goal=json.dumps(self.state['goal'], ensure_ascii=False))
            self.full_build(group)
            if self.state['full_build'] == 'playable':
                for ordinal in range(1, self.args.rounds + 1):
                    if not any(r['round'] == ordinal for r in self.state['rounds']):
                        self.repair_round(ordinal, group)
            self.check_version(self.root / self.state['current'])
            if init.version_hash(source) != self.protocol['source_hash']:
                raise ValueError('RSI modified its frozen input snapshot')
            _, original = source_checkpoint(self.protocol['source'], self.args.task, self.protocol['documents'])
            if init.version_hash(original) != self.protocol['source_hash']:
                raise ValueError('Source workspace changed while RSI was running')
            self.export(self.root / self.state['current'], 'final')
            evidence = self.state.get('selected_evidence', self.state['baseline'])
            write_json(self.root / 'behavior-skills-final.json', {
                'version_hash': self.state['current_hash'], 'policy': evidence['policy'],
                'behavior_skill': evidence.get('behavior_skill'),
                'verification': evidence['policy'].get('skill_verification', 'unverified')})
            errors = self.state['full_build'] != 'playable' or any(r['failures'] for r in self.state['rounds'])
            self.state.update(status='completed_with_errors' if errors else 'completed', finished_at=time.time(),
                              final_playtests='skipped', final_judge_run=False)
            self.save()
        # Scoring is deliberately after freezing. No scores enter planning.
        if self.args.score:
            target = self.root / 'exports/final'
            if not read_json(target / 'manifest.json').get('evaluation'):
                score(argparse.Namespace(workspace=target, exp_id=self.args.exp_id, docker=self.args.docker,
                    docker_host=self.args.docker_host, image=self.args.image, timeout=1800))
            self.state['evaluation'] = read_json(target / 'manifest.json')['evaluation']; self.save()
        write_json(self.root / 'result.json', {
            'profile': 'rsi', 'rsi_mode': 'upgrade', 'execution_mode': 'build_repair',
            'state': self.state['status'], 'task': self.args.task, 'checkpoint': str(self.state_path),
            'project': str(self.root / self.state['current'] / 'game'),
            'version': str(self.root / self.state['current']), 'version_hash': self.state['current_hash'],
            'frozen_export': str(self.root / 'exports/final'),
            'behavior_skill': str(self.root / 'behavior-skills-final.json'),
            'material_workspace': str(self.root / self.state['current'] / 'material'),
            'rounds_attempted': len(self.state['rounds']),
            'rounds_completed': sum(r['status'] == 'reviewed' for r in self.state['rounds']),
            'full_build_status': self.state['full_build'], 'final_judge_run': False,
            'evaluation': self.state.get('evaluation')})
        return self.state


def parser():
    p = init.parser()
    p.description = __doc__
    p.add_argument('--source', required=True, type=Path,
                   help='Completed grouped initialization or RSI workspace (not exports/final)')
    # Retain shared CLI/provider options without changing initialization defaults.
    p.set_defaults(stage='all', skip_final_playtests=True)
    for action in p._actions:
        if action.dest in ('stage', 'groups', 'skip_final_playtests'):
            action.help = argparse.SUPPRESS
        if action.dest == 'rounds':
            action.help = 'Repair rounds after the full upgrade build, default 3 (not per group)'
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 1 <= args.rounds <= 3 or args.stage != 'all':
            raise ValueError('RSI runs a complete cycle with 1..3 repair rounds')
        if not 1 <= args.play_seconds <= 600 or not 1 <= args.max_actions <= 2000:
            raise ValueError('Invalid Player time/action budget')
        if not 1 <= args.material_rounds <= 3 or not 1 <= args.material_candidates <= 4 or not 1 <= args.h3_steps <= 50:
            raise ValueError('Invalid material generation budget')
        if args.score and not args.exp_id:
            raise ValueError('--score requires a unique --exp-id')
        if not shutil.which(args.ffmpeg):
            raise ValueError('ffmpeg is required for playtests')
        args.bench = benchmark(args.bench)
        documents = {name: p.read_text() for name, p in public_documents(args.bench, args.task).items()}
        environment = preflight(argparse.Namespace(bench=args.bench, docker=args.docker,
                               docker_host=args.docker_host, image=args.image, cache=None))
        args.image_id = environment['image_id']
        if args.expected_account_hash and Path(args.codex).parent.name == 'codex-runtime':
            env = child_environment(from_args(args))
            env.update(CODEX_HOME=str(args.codex_home.resolve()),
                       GAMECRAFT_EXPECTED_CODEX_ACCOUNT_SHA256_12=args.expected_account_hash)
            subprocess.run([args.codex, '--check-account'], env=env, check=True, timeout=30)
        root = args.output.resolve()
        if root.exists() and not args.resume:
            raise ValueError('Output exists; use a new directory or --resume')
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = RSIEngine(args, RSIServices(args, documents), documents).run()
        print(json.dumps({'status': state['status'], 'workspace': str(root),
                          'rounds': len(state['rounds']), 'selected': state['current']}, indent=2))
    except ERRORS as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
