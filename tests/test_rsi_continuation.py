import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from tests import test_package_training as fixtures
from game_initialization.package import checkout, inspect_package
from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import continuation as c
from gamecraft_training.package.config import load
from gamecraft_training.package.edits import Generator
from gamecraft_video_skill import initialize


class ContinuationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = fixtures.PackageTrainingTests().package(self.root, 'test-counter', 'hero')
        self.calls = []

    def completed_run(self, package, output, *, accepted=True, video_skill=False, **kwargs):
        package, out = Path(package), Path(output)
        original, _, _ = checkout(package, out)
        self.calls.append((package, dict(kwargs, video_skill=video_skill)))
        candidate = out / 'round-001/candidate_project'
        candidate.parent.mkdir()
        shutil.copytree(original, candidate)
        (candidate / 'upgrade.gd').write_text('accepted change' if accepted else 'rejected change')
        plan = c.read(package / 'asset_plan.json')
        config = c.read(package / 'training.json')
        if accepted:
            # Bindings change along with frame count. Carry both into the next run.
            plan['assets'][0].update(frames=2, kind='animation', fps=2,
                                     loop=True, reference_prompt='one test hero')
            config['assets']['bindings']['hero'] = ['new0.png', 'new1.png']
            for name in config['assets']['bindings']['hero']:
                (candidate / name).write_bytes(b'new animation')
        selected = candidate if accepted else original
        write_json(out / 'selected_asset_plan.json', plan)
        write_json(out / 'selected_training.json', config)
        (out / 'best_skill.txt').write_text('wait for the moving target' if accepted else 'old skill')
        (out / 'working_skill.txt').write_text('do not inherit an unselected skill')
        write_json(out / 'rsi_upgrade_plan.json', {'goal': 'animated interaction'})
        write_json(out / 'run.json', {'profile': 'rsi', 'video_skill': video_skill})
        status = 'accepted' if accepted else 'not_run'
        write_json(out / 'final_upgrade_review.json', {
            'status': status, 'selected': 'upgrade' if accepted else 'original'})
        result = {'profile': 'rsi', 'rsi_mode': 'upgrade', 'state': 'completed',
                  'upgrade_status': status, 'project': str(selected),
                  'working_project': str(candidate if accepted else original),
                  'original_project': str(original), 'skill': str(out / 'best_skill.txt'),
                  'final_review': str(out / 'final_upgrade_review.json'),
                  'upgrade_plan': str(out / 'rsi_upgrade_plan.json'),
                  'selected_asset_plan': str(out / 'selected_asset_plan.json'),
                  'selected_training': str(out / 'selected_training.json'),
                  'rounds_attempted': 3, 'errors': []}
        if video_skill:
            snapshot = initialize(out / 'generation')
            write_json(snapshot / 'decisions.json', {'schema_version': 1, 'lessons': [{'id': 'learned'}]})
            result['selected_video_skill'] = str(snapshot)
        write_json(out / 'result.json', result)
        return result

    def test_checkpoint_matches_selected_version_and_is_ready_for_next_generator(self):
        original_hashes = digest(self.package)
        run = self.root / 'run'
        self.completed_run(self.package, run, video_skill=True)
        checkpoint = c.export_checkpoint(run, self.root / 'checkpoint')
        meta, adapter, scenarios = inspect_package(checkpoint)
        config, plan, _, _ = load(checkpoint, adapter, scenarios)
        self.assertEqual(meta['verification'], 'structural_only')
        self.assertEqual(plan['assets'][0]['frames'], 2)
        self.assertEqual(config['assets']['bindings']['hero'], ['new0.png', 'new1.png'])
        self.assertEqual((checkpoint / 'behavior_skill.txt').read_text(), 'wait for the moving target')
        self.assertEqual(c.read(checkpoint / 'video_skill/decisions.json')['lessons'][0]['id'], 'learned')
        config['_package_root'] = str(checkpoint)
        generator = Generator(self.root / 'next-generation', plan, config, sys.executable, True, 1)
        self.assertEqual(c.read(generator.root / 'video_skill/decisions.json')['lessons'][0]['id'], 'learned')
        self.assertEqual(digest(self.package), original_hashes)
        self.assertIsNone(c.read(checkpoint / 'checks/report.json')['passed'])

    def test_rejected_candidate_is_never_promoted(self):
        run = self.root / 'run'
        self.completed_run(self.package, run, accepted=False)
        checkpoint = c.export_checkpoint(run, self.root / 'checkpoint')
        self.assertFalse((checkpoint / 'game/upgrade.gd').exists())
        self.assertEqual((checkpoint / 'behavior_skill.txt').read_text(), 'old skill')
        self.assertEqual(c.read(checkpoint / 'asset_plan.json')['assets'][0]['frames'], 1)

    def test_three_cycles_chain_selected_packages_and_skills(self):
        out = self.root / 'chain'
        result = c.run_cycles(self.package, out, cycles=3, runner=self.completed_run,
                              rounds=3, video_skill=True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.calls), 3)
        for number, (package, options) in enumerate(self.calls[1:], 1):
            self.assertEqual(package, out / f'package-{number:03}')
            self.assertEqual(options['rounds'], 3)
            self.assertEqual(Path(options['initial_behavior_skill']).read_text(), 'wait for the moving target')
            self.assertIsNone(options['local_video_skill'])
        self.assertEqual(len(c.read(Path(result['package']) / 'rsi_lineage.json')['cycles']), 3)
        continued = c.run_cycles(None, self.root / 'next', continue_from=out, runner=self.completed_run)
        self.assertTrue(self.calls[-1][1]['video_skill'])
        self.assertEqual(len(c.read(Path(continued['package']) / 'rsi_lineage.json')['cycles']), 4)

    def test_cross_mount_paths_are_rebased(self):
        run = self.root / 'run'
        info = self.completed_run(self.package, run, video_skill=True)
        write_json(run / 'result.json', json.loads(json.dumps(info).replace(str(run), '/other-mount/run')))
        checkpoint = c.export_checkpoint(run, self.root / 'checkpoint')
        self.assertTrue((checkpoint / 'game/upgrade.gd').exists())

    def test_missing_binding_or_mismatched_selection_fails_without_publishing(self):
        run = self.root / 'run'
        info = self.completed_run(self.package, run)
        info['project'] = info['original_project']
        write_json(run / 'result.json', info)
        with self.assertRaisesRegex(ValueError, 'selection'):
            c.export_checkpoint(run, self.root / 'checkpoint')
        self.assertFalse((self.root / 'checkpoint').exists())
        info['project'] = info['working_project']
        write_json(run / 'result.json', info)
        (Path(info['project']) / 'new1.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing bound asset'):
            c.export_checkpoint(run, self.root / 'checkpoint')
        self.assertFalse((self.root / 'checkpoint').exists())

    def test_failed_cycle_preserves_last_checkpoint_and_can_continue(self):
        def runner(package, out, **kwargs):
            if self.calls:
                raise RuntimeError('service unavailable')
            return self.completed_run(package, out, **kwargs)
        out = self.root / 'chain'
        with self.assertRaisesRegex(RuntimeError, 'service unavailable'):
            c.run_cycles(self.package, out, cycles=3, runner=runner)
        summary = c.read(out / 'result.json')
        self.assertEqual(summary['state'], 'failed')
        inspect_package(summary['package'])
        result = c.run_cycles(None, self.root / 'next', continue_from=out, runner=self.completed_run)
        self.assertEqual(result['state'], 'completed')
        with self.assertRaises(FileExistsError):
            c.run_cycles(self.package, out, cycles=3, runner=self.completed_run)

    def test_legacy_video_history_ignores_rejected_workspace(self):
        run = self.root / 'run'
        info = self.completed_run(self.package, run, video_skill=True)
        info.pop('selected_video_skill')
        snapshot = initialize(run / 'accepted_generation')
        write_json(snapshot / 'decisions.json', {'schema_version': 1, 'lessons': [{'id': 'accepted'}]})
        write_json(run / 'history.json', [
            {'status': 'provisional', 'candidate': info['project'],
             'code_edit': {'generation_workspace': str(snapshot.parent)}},
            {'status': 'rejected_no_progress', 'candidate': str(run / 'bad'),
             'code_edit': {'generation_workspace': str(run / 'missing')}}])
        write_json(run / 'result.json', info)
        checkpoint = c.export_checkpoint(run, self.root / 'checkpoint')
        self.assertEqual(c.read(checkpoint / 'video_skill/decisions.json')['lessons'][0]['id'], 'accepted')

    def test_cli_uses_cycles_only_when_requested(self):
        from gamecraft_training.package import __main__ as cli
        args = ['runner', '--continue-from-rsi', 'previous', '--output', 'new',
                '--asset-python', sys.executable, '--godot', 'godot', '--codex-bin', sys.executable]
        with (patch.object(sys, 'argv', args), patch.object(c, 'run_cycles', return_value={'errors': []}) as run,
              patch.object(cli, 'run') as original):
            cli.main()
        original.assert_not_called()
        self.assertEqual(run.call_args.kwargs['continue_from'], 'previous')
        self.assertEqual(run.call_args.kwargs['rounds'], 3)
        with patch.object(sys, 'argv', args + ['--profile', 'initialization']):
            with self.assertRaises(SystemExit) as exc:
                cli.main()
        self.assertEqual(exc.exception.code, 2)

    def test_video_disabled_run_does_not_require_a_material_skill_directory(self):
        run = self.root / 'run'
        info = self.completed_run(self.package, run)
        write_json(run / 'history.json', [{'candidate': info['project'], 'status': 'provisional',
                                          'code_edit': {'generation_workspace': str(run / 'generation')}}])
        c.export_checkpoint(run, self.root / 'checkpoint')

    def test_selected_skill_is_self_contained_even_with_external_editor_queue(self):
        external = self.root / 'external-job'
        skill = initialize(external)
        write_json(skill / 'decisions.json', {'schema_version': 1, 'lessons': [{'id': 'kept'}]})
        selected = Path(c.save_selected_video_skill(self.root / 'run', SimpleNamespace(root=external)))
        shutil.rmtree(external)
        self.assertEqual(c.read(selected / 'decisions.json')['lessons'][0]['id'], 'kept')

    def test_next_goal_receives_prior_cycle_without_changing_fresh_goal_context(self):
        from gamecraft_training.package import upgrade
        run = self.root / 'run'
        self.completed_run(self.package, run)
        package = c.export_checkpoint(run, self.root / 'checkpoint')
        _, adapter, cases = inspect_package(package)
        with patch.object(upgrade.models, 'call', return_value={}) as call:
            upgrade._audit_baseline(package, package / 'game', adapter, cases,
                                     c.read(package / 'asset_plan.json'), [])
        self.assertEqual(call.call_args.args[1]['previous_rsi_cycles'][0]['goal']['goal'],
                         'animated interaction')
