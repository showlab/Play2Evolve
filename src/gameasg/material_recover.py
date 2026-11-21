"""Finish a material candidate whose H3 job outlived its waiting client.

This only reads an existing H3 job; it never submits a generation request.
"""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

from gamecraft_assets.generate import _judge, _process, acceptable
from gamecraft_assets.plans import validate_generic, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('job_id')
    parser.add_argument('--h3-url', default='http://127.0.0.1:30010')
    args = parser.parse_args()
    root = args.workspace.resolve()
    plan = json.loads((root / 'asset_plan.json').read_text())
    validate_generic(plan)
    if len(plan['assets']) != 1:
        raise ValueError('Recovery expects exactly one material asset')
    asset = plan['assets'][0]
    dest = root / 'generated' / asset['id'] / 'v0001'
    request = json.loads((dest / 'request.json').read_text())
    expected_hash = hashlib.sha256(json.dumps(asset, sort_keys=True).encode()).hexdigest()
    if request['plan_sha256'] != expected_hash or request['asset'] != asset['id']:
        raise ValueError('Candidate no longer matches the material plan')
    manifest = json.loads((root / 'asset_manifest.json').read_text())
    if asset['id'] in manifest['assets']:
        raise ValueError('Material is already accepted')

    base = args.h3_url.rstrip('/') + '/v1/videos/' + args.job_id
    with urllib.request.urlopen(base, timeout=30) as response:
        job = json.load(response)
    if job.get('id') != args.job_id or job.get('status') != 'completed':
        raise RuntimeError(f"H3 job has not completed: {job.get('status')}")
    video = dest / 'raw.mp4'
    if not video.exists():
        with urllib.request.urlopen(base + '/content', timeout=600) as response:
            temporary = video.with_suffix('.mp4.pending')
            with temporary.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            temporary.replace(video)
    import gen_assets as g
    paths, samples, metrics = _process(g, video, asset, dest)
    verdict = _judge(g, asset, samples)
    eligible = acceptable(verdict, metrics, asset['kind'])
    record = {**request, 'h3_job_id': args.job_id, 'video_skill': True,
              'verdict': verdict, 'metrics': metrics, 'eligible': eligible,
              'quality_status': 'judged',
              'files': [str(path.relative_to(root)) for path in paths]}
    write_json(dest / 'result.json', record)
    if not eligible:
        raise RuntimeError(f'H3 material was rejected by visual judge: {verdict}')
    manifest['assets'][asset['id']] = {**asset, 'version': 1,
        'files': record['files'], 'plan_sha256': expected_hash,
        'verdict': verdict, 'metrics': metrics, 'quality_status': 'judged'}
    write_json(root / 'asset_manifest.json', manifest)
    write_json(root / 'generation_status.json',
               {'pending': [], 'accepted': [asset['id']]})
    print(json.dumps({'asset': asset['id'], 'job_id': args.job_id,
                      'files': record['files'], 'verdict': verdict}, indent=2))


if __name__ == '__main__':
    main()
