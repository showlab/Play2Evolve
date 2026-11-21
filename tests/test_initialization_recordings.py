import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gamecraft_behavior.core import digest
from gamecraft_training.package.recordings import SelectedRecordings


class InitializationRecordingsTests(unittest.TestCase):
    def fixture(self, root):
        project = root / 'game'
        project.mkdir()
        (project / 'main.gd').write_text('extends Node\n')
        return project

    def episode(self, root, project, name='play', policy=False):
        directory = root / name
        directory.mkdir()
        files = {'video': 'replay.mp4', 'trace': 'trace.json'}
        if policy:
            files['player_policy'] = 'policy.py'
        result = {'scenario': name, 'normal_launch': True,
                  'replay_verified': True, 'replay_protocol': 'physics_frames_v1',
                  'project_sha256': digest(project), 'decision_frames': 6,
                  'playtest_files': {}}
        for key, filename in files.items():
            path = directory / filename
            path.write_bytes((name + ':' + key).encode())
            result[key] = str(path)
            result['playtest_files'][key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def test_archive_is_self_contained_and_videos_have_unique_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            archive = SelectedRecordings(root / 'recordings', ['one', 'two'])
            for name in ('one', 'two'):
                archive.remember(project, {'id': name},
                                 [self.episode(root, project, name, policy=True)], 3)
            manifest = json.loads(archive.finish(project).read_text())
            self.assertTrue(manifest['complete'])
            self.assertTrue(manifest['all_groups_match_final_selected_project'])
            moved = root / 'moved'
            shutil.copytree(archive.directory, moved)
            shutil.rmtree(archive.directory)
            for name in ('one', 'two'):
                shutil.rmtree(root / name)
            names = []
            for group in manifest['groups'].values():
                episode = group['episodes'][0]
                names.append(Path(episode['files']['video']['path']).stem)
                for file in episode['files'].values():
                    self.assertFalse(Path(file['path']).is_absolute())
                    self.assertEqual(hashlib.sha256((moved / file['path']).read_bytes()).hexdigest(),
                                     file['sha256'])
            self.assertEqual(len(set(names)), 2)

    def test_retained_evidence_is_copied_once_and_updates_selection_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            archive = SelectedRecordings(root / 'recordings', ['one'])
            episode = self.episode(root, project)
            archive.remember(project, {'id': 'one'}, [episode], 1)
            with patch('gamecraft_training.package.recordings.shutil.copyfile') as copy:
                archive.remember(project, {'id': 'one'}, [episode], 3)
            copy.assert_not_called()
            manifest = json.loads(archive.finish(project).read_text())
            self.assertEqual(manifest['groups']['one']['selected_round'], 3)
            self.assertEqual(len(list(archive.directory.rglob('*.mp4'))), 1)

    def test_later_groups_do_not_relabel_earlier_video_as_final_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            archive = SelectedRecordings(root / 'recordings', ['one', 'two'])
            archive.remember(project, {'id': 'one'}, [self.episode(root, project, 'first')], 3)
            (project / 'main.gd').write_text('extends Node\n# later repair\n')
            archive.remember(project, {'id': 'two'}, [self.episode(root, project, 'last')], 3)
            manifest = json.loads(archive.finish(project).read_text())
            self.assertTrue(manifest['complete'])
            self.assertFalse(manifest['groups']['one']['matches_final_selected_project'])
            self.assertTrue(manifest['groups']['two']['matches_final_selected_project'])
            self.assertFalse(manifest['all_groups_match_final_selected_project'])

    def test_new_winner_replaces_archive_and_interrupted_runs_keep_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            archive = SelectedRecordings(root / 'recordings', ['one', 'two'])
            archive.remember(project, {'id': 'one'}, [self.episode(root, project, 'old')], 1)
            archive.remember(project, {'id': 'one'}, [self.episode(root, project, 'winner')], 2)
            manifest = json.loads((archive.directory / 'manifest.json').read_text())
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['missing_groups'], ['two'])
            self.assertIsNone(manifest['groups']['one']['matches_final_selected_project'])
            video = archive.directory / manifest['groups']['one']['episodes'][0]['files']['video']['path']
            self.assertEqual(video.read_bytes(), b'winner:video')
            self.assertEqual(list(archive.directory.rglob('*.mp4')), [video])

    def test_invalid_or_tampered_evidence_is_not_promoted(self):
        for failure in ('missing', 'empty', 'hash', 'version', 'replay'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = self.fixture(root)
                archive = SelectedRecordings(root / 'recordings', ['one'])
                good = self.episode(root, project, 'good')
                archive.remember(project, {'id': 'one'}, [good], 1)
                bad = self.episode(root, project, 'bad')
                if failure == 'missing':
                    Path(bad['video']).unlink()
                elif failure == 'empty':
                    Path(bad['video']).write_bytes(b'')
                elif failure == 'hash':
                    Path(bad['video']).write_bytes(b'tampered')
                elif failure == 'version':
                    bad['project_sha256'] = {}
                else:
                    bad['replay_verified'] = False
                with self.assertRaises(ValueError):
                    archive.remember(project, {'id': 'one'}, [bad], 2)
                manifest = json.loads(archive.finish(project).read_text())
                self.assertEqual(manifest['groups']['one']['selected_round'], 1)

    def test_finish_detects_corrupted_archived_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.fixture(root)
            archive = SelectedRecordings(root / 'recordings', ['one'])
            archive.remember(project, {'id': 'one'}, [self.episode(root, project)], 1)
            next(archive.directory.rglob('*.mp4')).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                archive.finish(project)

    def test_loop_archives_selected_fixed_videos_in_all_scopes(self):
        from tests import test_scene_initialization
        for options in ({}, {'initialization_mode': 'whole_game'},
                        {'selected_scene': 'second'},
                        {'initialization_mode': 'grouped', 'initialization_groups': [
                            {'id': 'both', 'scenarios': ['initialization', 'second']}]},
                        {'reject': True, 'distinct_candidates': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result, _ = test_scene_initialization.SceneInitializationTests().execute(root, **options)
                path = Path(result['selected_recordings'])
                manifest = json.loads(path.read_text())
                self.assertTrue(manifest['complete'])
                self.assertFalse(manifest['final_replay_run'])
                self.assertFalse(manifest['official_benchmark_metric'])
                self.assertTrue(manifest['all_groups_match_final_selected_project'])
                for group in manifest['groups'].values():
                    for row in group['episodes']:
                        self.assertTrue((path.parent / row['files']['video']['path']).is_file())
                    if options.get('reject'):
                        self.assertNotIn('fixed.gd', group['project_sha256'])
                        self.assertIn('initialization_current', group['episodes'][0]['files']['video']['source'])
                if 'package' in result:
                    self.assertTrue(path.is_relative_to(Path(result['package'])))
                else:
                    self.assertEqual(path.parent, root / 'run/initialization_recordings')

    def test_program_archive_keeps_bootstrap_videos_separate_from_selected_versions(self):
        from tests import test_initialization_player
        for reject, count, expected_calls in ((False, 1, 4), (True, 1, 6), (False, 2, 7)):
            with self.subTest(reject=reject, count=count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result, _, calls = test_initialization_player.InitializationPlayerTests().exercise(
                    root, reject=reject, count=count)
                self.assertEqual(len(calls), expected_calls)
                bootstrap_path = Path(result['bootstrap_recordings'])
                bootstrap = json.loads(bootstrap_path.read_text())
                self.assertTrue(bootstrap['complete'])
                self.assertEqual(bootstrap['final_selected_project_sha256'],
                                 digest(root/'run/initial/game'))
                self.assertEqual(len(result['bootstrap_benchmark_videos']),
                                 sum(len(group['episodes']) for group in bootstrap['groups'].values()))
                for video in result['bootstrap_benchmark_videos']:
                    self.assertTrue(Path(video).is_file())
                final_path = Path(result['final_recordings'])
                final = json.loads(final_path.read_text())
                self.assertTrue(final['complete'])
                self.assertTrue(final['all_groups_match_final_selected_project'])
                self.assertEqual(len(result['final_benchmark_videos']),
                                 sum(len(group['episodes']) for group in final['groups'].values()))
                for video in result['final_benchmark_videos']:
                    self.assertTrue(Path(video).is_file())
                path = Path(result['selected_recordings'])
                manifest = json.loads(path.read_text())
                selected = json.loads(Path(result['selected_playtest']).read_text())
                self.assertEqual(set(manifest['groups']), set(selected['groups']))
                for key, group in manifest['groups'].items():
                    self.assertEqual(group['project_sha256'], selected['groups'][key]['project_sha256'])
                    self.assertEqual(len(group['episodes']), len(selected['groups'][key]['episodes']))
                    for archived, original in zip(group['episodes'], selected['groups'][key]['episodes']):
                        for kind in ('video', 'trace', 'player_policy'):
                            file = archived['files'][kind]
                            self.assertEqual((path.parent / file['path']).read_bytes(),
                                             Path(original[kind]).read_bytes())
                            self.assertEqual(file['sha256'], original['playtest_files'][kind])
                        policy = path.parent / archived['files']['player_policy']['path']
                        if count == 1:
                            self.assertIn('policy 2' if reject else 'policy 3', policy.read_text())


if __name__ == '__main__':
    unittest.main()
