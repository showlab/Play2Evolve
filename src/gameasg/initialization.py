"""Bootstrap a complete HTML game, then optimize each requirement group three times.

Official benchmark tests are used only by the optional scoring step, after freeze.
The legacy one-shot and method-pilot commands remain separate and unchanged.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from codex_provider import add_arguments, from_args, child_environment
from . import contracts as c
from .__main__ import (DOCS, BENCH_COMMIT, benchmark, public_documents, digest,
                       read_json, write_json, prepare, freeze, score)
from .environment import IMAGE, preflight
from .html_assets import verify_bundle
from .services import Services, compact_trace
from .behavior_skill import reflect_playtest

FORMAT = 'gameasg-grouped-initialization-v1'


def version_hash(version):
    """Bind evidence to code, accepted asset bytes, and the material/coverage plan."""
    version = Path(version)
    files = ['game/index.html', 'game/gameasg-assets.js', 'plan.json', 'material/asset_manifest.json']
    manifest = read_json(version / 'material/asset_manifest.json')
    for record in manifest['assets'].values():
        files += ['material/' + name for name in record['files']]
    records = {}
    for name in sorted(set(files)):
        path = (version / name).resolve()
        if not path.is_relative_to(version.resolve()):
            raise ValueError('Version asset escapes its package')
        records[name] = digest(path)
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def fresh_directory(path):
    """Preserve incomplete attempts for diagnosis; never overwrite them."""
    if path.exists():
        path.rename(path.with_name(path.name + '.interrupted-' + str(time.time_ns())))
    path.mkdir(parents=True)


def observed(evidence):
    return {r['requirement_id'] for r in evidence['assessment']['requirements'] if r['observed']}


class Engine:
    def __init__(self, args, services, documents):
        self.args, self.services, self.documents = args, services, documents
        self.root = Path(args.output).resolve()
        self.state_path = self.root / 'initialization.json'
        self.protocol = {key: getattr(args, key) for key in (
            'task', 'groups', 'rounds', 'model', 'play_seconds', 'max_actions',
            'material_rounds', 'material_candidates', 'h3_steps', 'expected_account_hash')}
        self.protocol['provider'] = from_args(args)
        self.protocol['documents'] = {name: hashlib.sha256(text.encode()).hexdigest()
                                      for name, text in documents.items()}
        self.protocol['benchmark_commit'] = BENCH_COMMIT
        self.protocol['image_id'] = getattr(args, 'image_id', None)
        if self.state_path.exists():
            if not args.resume:
                raise ValueError('Workspace already exists; use --resume')
            self.state = read_json(self.state_path)
            if self.state.get('format') != FORMAT or self.state.get('protocol') != self.protocol:
                raise ValueError('Resume requires the same task, documents and frozen settings')
            if self.state.get('current'):
                current = self.root / self.state['current']
                if version_hash(current) != self.state['current_hash']:
                    raise ValueError('Selected game or its accepted materials changed since checkpoint')
        else:
            if args.resume:
                raise ValueError('No grouped initialization checkpoint to resume')
            self.state = dict(format=FORMAT, protocol=self.protocol, status='planning',
                              started_at=time.time(), current=None, current_hash=None,
                              rounds=[], bootstrap_evidence={}, final_evidence={})
            self.save()

    def save(self):
        self.state['updated_at'] = time.time()
        write_json(self.state_path, self.state)

    def select_version(self, version):
        self.state['current'] = str(version.relative_to(self.root))
        self.state['current_hash'] = version_hash(version)

    def check_version(self, version):
        verify_bundle(version / 'game')
        for name, value in self.protocol['documents'].items():
            if digest(version / 'game' / name) != value:
                raise ValueError('Version public documents no longer match the original task')

    def play(self, version, group, output, *, prior=None, changes=None, floor=()):
        identity = version_hash(version)
        receipt = output / 'evidence.json'
        if receipt.exists():
            evidence = read_json(receipt)
            if evidence['version_hash'] != identity or evidence['group'] != group:
                raise ValueError('Cached playtest belongs to a different game or group')
            for name, sha in evidence['artifacts'].items():
                if digest(output / name) != sha:
                    raise ValueError('Cached playtest artifact changed')
            return reflect_playtest(self.services, version, group, output, evidence, prior=prior, changes=changes)
        output.mkdir(parents=True, exist_ok=True)
        plan = read_json(version / 'plan.json')
        evidence = None
        retry = None
        for attempt in range(2):
            run = output / f'attempt-{attempt + 1}'
            fresh_directory(run)
            initial = self.services.probe(version / 'game', run / 'initial',
                'function step(){return {type:"done",value:"initial observation"};}', group)
            proposal = self.services.player(version / 'game', plan, group, run, initial,
                prior=prior, changes={'changes': changes, 'retry': retry, 'bootstrap_coverage_floor': list(floor)})
            write_json(run / 'policy.json', proposal)
            (run / 'policy.js').write_text(proposal['policy'])
            (run / 'policy_skill.md').write_text(proposal['skill'])
            try:
                trace = self.services.probe(version / 'game', run / 'replay', proposal['policy'], group)
            except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                # Compilation/container failures still consume only one bounded retry.
                trace_path = run / 'replay/trace.json'
                trace = read_json(trace_path) if trace_path.exists() else dict(frames=[], actions=[], errors=[])
                trace['errors'].append(str(exc))
                trace['stop_reason'] = 'probe_error'
                write_json(trace_path, trace)
            if not trace['frames'] or not (run / 'replay/video.mp4').is_file():
                trace['errors'].append('Playable video evidence is missing')
                write_json(run / 'replay/trace.json', trace)
            if trace['errors']:
                assessment = {'requirements': [dict(requirement_id=rid, observed=False, passed=False,
                    evidence='Browser/Player error; inspect trace', frames=[]) for rid in group['requirement_ids']],
                    'retry_needed': True, 'retry_reason': str(trace['errors'])}
            else:
                try:
                    assessment = self.services.assess(group, plan, run, trace)
                except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    write_json(run / 'assessment-error.json', {'error': str(exc)})
                    assessment = {'requirements': [dict(requirement_id=rid, observed=False,
                        passed=False, evidence='Assessment unavailable; inspect replay', frames=[])
                        for rid in group['requirement_ids']], 'retry_needed': True,
                        'retry_reason': f'Assessment unavailable: {exc}'}
            write_json(run / 'assessment.json', assessment)
            evidence = dict(path=str(run), version_hash=identity, group=group,
                            policy=proposal, assessment=assessment, trace=compact_trace(trace))
            missing = set(group['requirement_ids']) - observed(evidence)
            if not trace['errors'] and not (assessment['retry_needed'] or missing):
                break
            retry = {'assessment': assessment, 'trace': compact_trace(trace), 'missing': sorted(missing)}
            prior = proposal
        if version_hash(version) != identity:
            raise ValueError('Playtest mutated the frozen game/material version')
        # Keep the better-covered valid attempt if the bounded retry was less useful.
        if attempt == 1:
            first = output / 'attempt-1'
            t1, a1 = read_json(first / 'replay/trace.json'), read_json(first / 'assessment.json')
            if not t1['errors'] and (evidence['trace']['errors'] or
                    sum(r['observed'] for r in a1['requirements']) > len(observed(evidence))):
                evidence = dict(path=str(first), version_hash=identity, group=group,
                    policy=read_json(first / 'policy.json'), assessment=a1, trace=compact_trace(t1))
        chosen = Path(evidence['path'])
        files = [chosen / 'policy.json', chosen / 'policy.js', chosen / 'assessment.json',
                 chosen / 'replay/trace.json', chosen / 'replay/video.mp4']
        files += [chosen / 'replay' / f['image'] for f in read_json(chosen / 'replay/trace.json')['frames']]
        evidence['artifacts'] = {str(p.relative_to(output)): digest(p) for p in files if p.exists()}
        write_json(receipt, evidence)
        # Save raw evidence first: a failed reflection can resume without recording
        # either attempt again. Reflection runs once after the bounded playtest.
        return reflect_playtest(self.services, version, group, output, evidence, prior=prior, changes=changes)

    def bootstrap(self):
        plan_path = self.root / 'bootstrap-plan.json'
        if not plan_path.exists():
            plan = self.services.plan(self.root / 'planning')
            c.validate_plan(plan, self.documents, self.args.groups, self.args.task)
            write_json(plan_path, plan)
        plan = read_json(plan_path)
        c.validate_plan(plan, self.documents, self.args.groups, self.args.task)
        version = self.root / 'versions/bootstrap'
        if not self.state.get('bootstrap_hash'):
            self.state['status'] = 'bootstrap'; self.save()
            version.mkdir(parents=True, exist_ok=True)
            write_json(version / 'plan.json', plan)
            self.services.materials(version, plan)
            feedback = None
            for attempt in range(2):
                try:
                    self.services.build(version, plan, 'Build the complete initial game.', feedback)
                    self.check_version(version)
                    break
                except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    if attempt: raise
                    feedback = str(exc)
            self.select_version(version)
            self.state['bootstrap_hash'] = self.state['current_hash']; self.save()
        if version_hash(version) != self.state['bootstrap_hash']:
            raise ValueError('Frozen bootstrap package changed')
        self.export(version, 'bootstrap')
        self.state['status'] = 'bootstrapped'; self.save()
        return version

    def optimize(self):
        plan = read_json(self.root / 'bootstrap-plan.json')
        for group_index, group in enumerate(plan['groups'], 1):
            if group['id'] not in self.state['bootstrap_evidence']:
                baseline = self.play(self.root / 'versions/bootstrap', group,
                                     self.root / 'optimization' / 'baseline-playtests' / group['id'])
                self.state['bootstrap_evidence'][group['id']] = baseline
                self.save()
            for round_index in range(1, self.args.rounds + 1):
                key = f'group-{group_index:02d}-round-{round_index:02d}'
                if any(row['id'] == key for row in self.state['rounds']):
                    continue
                self.state['status'] = key; self.save()
                out = self.root / 'optimization' / key
                fresh_directory(out)
                current = self.root / self.state['current']
                current_plan = read_json(current / 'plan.json')
                history = [self.state['bootstrap_evidence'][group['id']]] + [
                    row['selected_evidence'] for row in self.state['rounds'] if row['group'] == group['id']]
                prior = history[-1]['policy']
                # Reuse the winner's actual evidence when code/material identity is
                # unchanged, rather than replaying the same game a second time.
                matching = next((ev for ev in reversed(history)
                                 if ev['version_hash'] == self.state['current_hash']), None)
                cache = (Path(matching['path']).parent if matching else
                         self.root / 'playtests' / self.state['current_hash'] / group['id'])
                before = self.play(current, group, cache, prior=prior)
                diagnosis = self.services.diagnose(current, current_plan, group, before, out)
                write_json(out / 'diagnosis.json', diagnosis)
                known_requirements = {r['id'] for r in plan['requirements']}
                issue_ids = c.diagnosis_issue_ids(diagnosis['issues'])
                for issue in diagnosis['issues']:
                    if not set(issue['requirement_ids']) <= known_requirements:
                        raise ValueError('Judge referenced an unknown requirement')
                failures = {}
                try:
                    repair = self.services.repair(current, current_plan, diagnosis, out)
                    targeted = c.validate_repair_targets(repair, issue_ids)
                except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    failures['repair_plan'] = str(exc)
                    write_json(out / 'repair-plan-error.json', {'error': str(exc)})
                    repair = {'reason': 'Repair plan unavailable; keep incumbent', 'candidates': []}
                    targeted = set()
                write_json(out / 'repair-plan.json', repair)
                options, versions = {'current': before}, {}
                for candidate in repair['candidates']:
                    name = candidate['id']
                    dest = self.root / 'versions' / (key + '-' + name)
                    try:
                        candidate_plan = c.apply_candidate(current_plan, candidate, self.documents, self.args.task)
                        if dest.exists():
                            dest.rename(dest.with_name(dest.name + '.interrupted-' + str(time.time_ns())))
                        shutil.copytree(current, dest)
                        write_json(dest / 'plan.json', candidate_plan)
                        self.services.materials(dest, candidate_plan,
                            only=[a['id'] for a in candidate['materials']], feedback=json.dumps(diagnosis))
                        self.services.build(dest, candidate_plan, candidate['instructions'], diagnosis)
                        self.check_version(dest)
                        after = self.play(dest, group, out / ('candidate-' + name), prior=before['policy'], changes=candidate)
                        if after['trace']['errors']:
                            raise ValueError('Candidate browser runtime/Player failed')
                        options[name], versions[name] = after, dest
                    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                        failures[name] = str(exc)
                        write_json(out / ('candidate-' + name + '-failure.json'), {'error': str(exc)})
                verdict = {'winner': 'current', 'reason': 'No viable candidate; retain incumbent'}
                if versions:
                    try:
                        verdict = self.services.select(group, diagnosis, options, out)
                        if verdict['winner'] not in options:
                            raise ValueError('Judge selected an unavailable candidate')
                    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        failures['selection'] = str(exc)
                        write_json(out / 'selection-error.json', {'error': str(exc)})
                        verdict = {'winner': 'current', 'reason': 'Selection unavailable; retain incumbent'}
                # Publication is one state-file update. Rejected materials never become incumbent assets.
                if verdict['winner'] != 'current':
                    self.select_version(versions[verdict['winner']])
                self.state['rounds'].append(dict(id=key, group=group['id'], round=round_index,
                    before_hash=before['version_hash'], after_hash=self.state['current_hash'],
                    selection=verdict, failures=failures, deferred_issue_ids=sorted(issue_ids - targeted), path=str(out),
                    selected_evidence=options[verdict['winner']]))
                self.save()
        self.finalize()

    def finalize(self):
        current = self.root / self.state['current']
        plan = read_json(current / 'plan.json')
        if getattr(self.args, 'skip_final_playtests', False):
            # Export remains mandatory. Prior evidence is useful for continuation,
            # but must not be presented as validation of the integrated final game.
            self.check_version(current)
            skills = {}
            for group in plan['groups']:
                history = [self.state['bootstrap_evidence'][group['id']]] + [
                    row['selected_evidence'] for row in self.state['rounds'] if row['group'] == group['id']]
                ev = history[-1]
                skills[group['id']] = {'source_version_hash': ev['version_hash'],
                                      'policy': ev['policy'], 'behavior_skill': ev.get('behavior_skill')}
            write_json(self.root / 'behavior-skills-final.json', {
                'version_hash': self.state['current_hash'], 'final_playtests': 'skipped', 'groups': skills})
            self.export(current, 'final')
            self.state.update(status='completed', finished_at=time.time(),
                              final_playtests='skipped', final_version_evaluated=False)
            self.save()
            return
        self.state['status'] = 'final-playtests'; self.save()
        missing, lost, asset_usage = {}, {}, {}
        for group in plan['groups']:
            baseline = self.state['bootstrap_evidence'][group['id']]
            relevant = [row for row in self.state['rounds'] if row['group'] == group['id']]
            prior = relevant[-1]['selected_evidence']['policy'] if relevant else baseline['policy']
            # Reauthor against the fully integrated game, not an earlier group's winner.
            evidence = self.play(current, group, self.root / 'final-playtests' / group['id'],
                                 prior=prior, changes='All groups are now integrated.', floor=observed(baseline))
            self.state['final_evidence'][group['id']] = evidence; self.save()
            missing[group['id']] = sorted(set(group['requirement_ids']) - observed(evidence))
            lost[group['id']] = sorted(observed(baseline) - observed(evidence))
            full_trace = read_json(Path(evidence['path']) / 'replay/trace.json')
            for frame in full_trace['frames']:
                for aid, usage in frame.get('assets', {}).items():
                    asset_usage.setdefault(aid, set()).update(usage.get('frames', []))
        missing_assets = [a['id'] for a in plan['assets'] if
                          len(asset_usage.get(a['id'], set())) < (1 if a['motion'] == 'static' else 2)]
        errors = [g for g, ev in self.state['final_evidence'].items() if ev['trace']['errors']]
        coverage = dict(missing=missing, lost_from_bootstrap=lost, missing_material_playback=missing_assets,
                        runtime_errors=errors, version_hash=self.state['current_hash'],
                        complete=not (any(missing.values()) or any(lost.values()) or missing_assets or errors))
        write_json(self.root / 'final-coverage.json', coverage)
        write_json(self.root / 'behavior-skills-final.json', {
            'version_hash': self.state['current_hash'],
            'groups': {gid: ev['behavior_skill'] for gid, ev in self.state['final_evidence'].items()}})
        self.export(current, 'final')
        self.state.update(status='completed' if coverage['complete'] else 'completed_with_coverage_gaps',
                          finished_at=time.time(), coverage=coverage)
        self.save()

    def export(self, version, name):
        target = self.root / 'exports' / name
        if (target / 'manifest.json').exists():
            manifest = read_json(target / 'manifest.json')
            if manifest.get('generation'):
                expected = digest(version / 'game/index.html')
                if (manifest['generation']['html_sha256'] != expected or
                        digest(target / 'game/index.html') != expected):
                    raise ValueError('Existing frozen export differs from selected HTML')
                return target
            if manifest.get('evaluation'):
                raise ValueError('Cannot replace an export with an evaluation receipt')
        if target.exists():
            target.rename(target.with_name(target.name + '.interrupted-' + str(time.time_ns())))
        prepare(argparse.Namespace(bench=self.args.bench, task=self.args.task, output=target))
        freeze(argparse.Namespace(workspace=target, html=version / 'game/index.html'))
        return target

    def run(self):
        incomplete = not self.state.get('bootstrap_hash')
        if self.args.stage == 'optimize' and incomplete:
            raise ValueError('optimize requires a completed bootstrap; resume with --stage all to finish it')
        if incomplete:
            self.bootstrap()
        else:
            version = self.root / 'versions/bootstrap'
            if version_hash(version) != self.state['bootstrap_hash']:
                raise ValueError('Frozen bootstrap package changed')
            self.export(version, 'bootstrap')
        if self.args.stage != 'bootstrap':
            self.optimize()
        if self.args.score:
            name = 'bootstrap' if self.args.stage == 'bootstrap' else 'final'
            target = self.root / 'exports' / name
            manifest = read_json(target / 'manifest.json')
            if manifest.get('evaluation') is None:
                score(argparse.Namespace(workspace=target, exp_id=self.args.exp_id, docker=self.args.docker,
                    docker_host=self.args.docker_host, image=self.args.image, timeout=1800))
            self.state['evaluation'] = read_json(target / 'manifest.json')['evaluation']; self.save()
        return self.state


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bench', type=Path, required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--stage', choices=['bootstrap', 'optimize', 'all'], default='all')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--groups', type=int, default=3)
    p.add_argument('--rounds', type=int, default=3, help='Rounds PER group')
    p.add_argument('--codex-home', type=Path, required=True)
    p.add_argument('--expected-account-hash', default='', help='Optional SHA-256 account ID prefix (12 characters)')
    p.add_argument('--codex', required=True)
    p.add_argument('--model', default='gpt-6-sol')
    p.add_argument('--codex-timeout', type=int, default=3600)
    p.add_argument('--asset-python', required=True)
    p.add_argument('--h3-url', default='http://127.0.0.1:30010')
    p.add_argument('--h3-timeout', type=int, default=3600)
    p.add_argument('--h3-steps', type=int, default=50)
    p.add_argument('--material-rounds', type=int, default=1,
                   help='Compatibility setting; integrated material generation uses one attempt')
    p.add_argument('--material-candidates', type=int, default=1,
                   help='Compatibility setting; integrated material generation uses one candidate')
    p.add_argument('--play-seconds', type=int, default=120)
    p.add_argument('--max-actions', type=int, default=240)
    p.add_argument('--docker', required=True)
    p.add_argument('--docker-host', default='')
    p.add_argument('--image', default=IMAGE)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--score', action='store_true', help='Run official evaluator only after export is frozen')
    p.add_argument('--skip-final-playtests', action='store_true',
                   help='Export the selected game and skills without an extra final replay pass')
    p.add_argument('--exp-id', help='Required with --score')
    add_arguments(p)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 1 <= args.groups <= 10 or not 1 <= args.rounds <= 3:
            raise ValueError('groups must be 1..10; rounds per group 1..3')
        if not 1 <= args.play_seconds <= 600 or not 1 <= args.max_actions <= 2000:
            raise ValueError('Invalid Player time/action budget')
        if not 1 <= args.material_rounds <= 3 or not 1 <= args.material_candidates <= 4 or not 1 <= args.h3_steps <= 50:
            raise ValueError('Invalid material generation budget')
        if args.score and not args.exp_id:
            raise ValueError('--score requires a unique --exp-id')
        if args.stage == 'optimize' and not args.resume:
            raise ValueError('optimize requires --resume on a bootstrapped workspace')
        if not shutil.which(args.ffmpeg):
            raise ValueError('ffmpeg is required to record playtest video')
        args.bench = benchmark(args.bench)
        documents = {name: path.read_text() for name, path in public_documents(args.bench, args.task).items()}
        environment = preflight(argparse.Namespace(bench=args.bench, docker=args.docker,
                                    docker_host=args.docker_host, image=args.image, cache=None))
        args.image_id = environment['image_id']
        # Check the actual wrapper in the actual execution host before planning or H3 calls.
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
            engine = Engine(args, Services(args, documents), documents)
            result = engine.run()
            print(json.dumps({'status': result['status'], 'workspace': str(root),
                              'rounds': len(result['rounds']), 'coverage': result.get('coverage')}, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
