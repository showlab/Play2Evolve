"""Keep the selected initialization playtests independently of round workspaces."""
import copy
import hashlib
import shutil
from pathlib import Path

from gamecraft_behavior.core import digest, write_json


def _sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class SelectedRecordings:
    """Archive incumbents after selection, without another Player or Judge call."""

    def __init__(self, directory, group_ids):
        self.directory = Path(directory)
        self.directory.mkdir()
        self.group_ids = list(group_ids)
        self.groups = {}

    def remember(self, project, unit, episodes, round_number):
        group_id = unit['id'] if unit else 'whole_game'
        ordinal = self.group_ids.index(group_id) + 1
        fingerprint = digest(project)
        if not episodes:
            raise ValueError('Selected initialization recordings require episodes')
        rows = []
        for episode in episodes:
            if not episode.get('replay_verified') or episode.get('project_sha256') != fingerprint:
                raise ValueError('Selected recording does not belong to the selected project')
            files = {}
            for key in ('video', 'trace', 'player_policy'):
                if key == 'player_policy' and not episode.get(key):
                    continue
                source = Path(episode.get(key, ''))
                if not source.is_file() or not source.stat().st_size:
                    raise ValueError(f'Missing selected recording file: {key}')
                checksum = _sha256(source)
                expected = episode.get('playtest_files', {}).get(key)
                if expected is not None and expected != checksum:
                    raise ValueError(f'Selected recording checksum mismatch: {key}')
                files[key] = {'source': str(source.resolve()), 'sha256': checksum}
            rows.append({
                'scenario': episode['scenario'],
                'normal_launch': episode.get('normal_launch', False),
                'decision_frames': episode.get('decision_frames'),
                'replay_protocol': episode.get('replay_protocol'),
                'replay_verified': True,
                'behavior_skill_sha256': hashlib.sha256(
                    episode.get('player_skill', '').encode('utf-8')).hexdigest(),
                'files': files,
            })
        previous = self.groups.get(group_id)
        # A later round can retain exactly the same evidence. Do not duplicate
        # the movie or rerun the player just to update the selection round.
        previous_rows = copy.deepcopy(previous['episodes']) if previous else None
        for row in previous_rows or []:
            for file in row['files'].values():
                file.pop('path', None)
        if previous and previous['project_sha256'] == fingerprint and previous_rows == rows:
            previous['selected_round'] = round_number
            self._save()
            return
        target = self.directory / f'group-{ordinal:03}' / f'round-{round_number:03}'
        target.mkdir(parents=True)
        try:
            for index, row in enumerate(rows, 1):
                for key, file in row['files'].items():
                    source = Path(file['source'])
                    suffix = {'trace': '.json', 'player_policy': '.py'}.get(key, source.suffix or '.mp4')
                    destination = target / f'group-{ordinal:03}-episode-{index:03}{suffix}'
                    shutil.copyfile(source, destination)
                    if _sha256(destination) != file['sha256']:
                        raise ValueError(f'Archived recording checksum mismatch: {key}')
                    file['path'] = str(destination.relative_to(self.directory))
        except Exception:
            shutil.rmtree(target)
            raise
        self.groups[group_id] = {
            'unit': copy.deepcopy(unit) if unit else {'id': group_id},
            'selected_round': round_number,
            'project': str(project),
            'project_sha256': fingerprint,
            'episodes': rows,
        }
        self._save()
        if previous:
            old_directory = self.directory / Path(previous['episodes'][0]['files']['video']['path']).parent
            shutil.rmtree(old_directory)

    def _save(self, final_project=None):
        groups = copy.deepcopy(self.groups)
        fingerprint = digest(final_project) if final_project is not None else None
        for entry in groups.values():
            entry['matches_final_selected_project'] = (
                entry['project_sha256'] == fingerprint if fingerprint is not None else None)
        missing = [key for key in self.group_ids if key not in groups]
        manifest = {
            'purpose': 'selected_group_playtests',
            'path_base': 'directory_containing_this_manifest',
            'complete': final_project is not None and not missing,
            'expected_groups': self.group_ids,
            'missing_groups': missing,
            'final_selected_project_sha256': fingerprint,
            'all_groups_match_final_selected_project': (
                not missing and bool(groups) and
                all(row['matches_final_selected_project'] for row in groups.values())
                if fingerprint is not None else None),
            'final_replay_run': False,
            'official_benchmark_metric': False,
            'scope': 'Selected playtest per group. Earlier groups may predate later game edits. '
                     'Version comparisons refer to the selected source game, before packaging declared demos. '
                     'Recordings do not establish requirement acceptance or a benchmark score.',
            'groups': groups,
        }
        write_json(self.directory / 'manifest.json', manifest)
        return self.directory / 'manifest.json'

    def finish(self, project):
        for entry in self.groups.values():
            for row in entry['episodes']:
                for file in row['files'].values():
                    path = self.directory / file['path']
                    if not path.is_file() or _sha256(path) != file['sha256']:
                        raise ValueError(f'Archived recording missing or changed: {path}')
        return self._save(final_project=project)
