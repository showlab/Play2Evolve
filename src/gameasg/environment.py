"""Portable, pinned Docker evaluator setup for GameASG-Bench."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

from .__main__ import BENCH_COMMIT, DEFAULT_BENCH, benchmark

IMAGE = 'gamebench-env:v2.0'
FORMAT = 'gameasg-image-cache-v1'


def docker(args, *command, check=True):
    env = dict(os.environ)
    if args.docker_host:
        env['DOCKER_HOST'] = args.docker_host
    executable = str(Path(args.docker).expanduser())
    return subprocess.run([executable, *command], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def image_id(args):
    result = docker(args, 'image', 'inspect', args.image, '--format', '{{.Id}}', check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def daemon_version(args):
    result = docker(args, 'info', '--format', '{{.ServerVersion}}', check=False)
    if result.returncode:
        raise RuntimeError('Docker daemon unavailable: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout.decode().strip()


def start_daemon(args):
    if not args.docker_host or not args.docker_host.startswith('unix://'):
        raise ValueError('--start-daemon requires an explicit unix:// --docker-host')
    socket = Path(args.docker_host.removeprefix('unix://')).resolve()
    state = Path(args.state_dir).resolve()
    if state == Path('/') or socket.parent != state:
        raise ValueError('Docker socket must be directly inside --state-dir')
    dockerd = shutil.which(args.dockerd)
    if not dockerd:
        raise ValueError(f'dockerd not found: {args.dockerd}')
    if not shutil.which('iptables'):
        raise ValueError('iptables is required for the private Docker bridge')
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise ValueError('Starting a private dockerd requires root on this host')
    if docker(args, 'info', '--format', '{{.ServerVersion}}', check=False).returncode == 0:
        return
    if socket.exists():
        raise RuntimeError(f'Stale or occupied Docker socket: {socket}; inspect it before retrying')
    state.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PATH'] = str(Path(dockerd).resolve().parent) + os.pathsep + env.get('PATH', '')
    command = [dockerd,
               f'--host={args.docker_host}', f'--data-root={state / "data"}',
               f'--exec-root={state / "exec"}', f'--pidfile={state / "dockerd.pid"}',
               '--storage-driver=vfs', f'--bip={args.bip}',
               f'--default-address-pool=base={args.address_pool},size=26']
    with (state / 'dockerd.log').open('a') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    for _ in range(60):
        if docker(args, 'info', '--format', '{{.ServerVersion}}', check=False).returncode == 0:
            return
        if process.poll() is not None:
            raise RuntimeError(f'Private dockerd exited; see {state / "dockerd.log"}')
        time.sleep(2)
    raise RuntimeError(f'Private dockerd did not become ready; see {state / "dockerd.log"}')


def preflight(args, require_image=True):
    bench = benchmark(args.bench)
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise RuntimeError('Official pinned image requires Linux x86_64')
    if not (bench / 'docker_test.sh').is_file() or not (bench / 'Dockerfile').is_file():
        raise ValueError('Incomplete GameASG checkout')
    if not shutil.which(args.docker):
        raise ValueError(f'Docker CLI not found: {args.docker}')
    version = daemon_version(args)
    ident = image_id(args)
    if require_image and ident is None:
        raise RuntimeError(f'Missing evaluator image {args.image}; run install')
    if args.cache and require_image:
        expected = cache_manifest(args.cache)
        if expected['image'] != args.image or expected['image_id'] != ident:
            raise ValueError('Installed evaluator image differs from portable cache')
    return {'benchmark': str(bench), 'benchmark_commit': BENCH_COMMIT,
            'docker_server': version, 'image': args.image, 'image_id': ident,
            'docker_host': args.docker_host or os.getenv('DOCKER_HOST', 'default')}


def sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def cache_manifest(archive):
    archive = Path(archive)
    manifest = archive.with_name(archive.name + '.json')
    if not archive.is_file() or not manifest.is_file():
        raise ValueError('Image cache and its .json manifest are both required')
    data = json.loads(manifest.read_text())
    if (data.get('format') != FORMAT or data.get('benchmark_commit') != BENCH_COMMIT
            or data.get('archive_sha256') != sha256(archive)):
        raise ValueError('Image cache identity or SHA-256 mismatch')
    return data


def install(args):
    if args.start_daemon:
        start_daemon(args)
    state = preflight(args, require_image=False)
    if state['image_id']:
        if args.cache:
            expected = cache_manifest(args.cache)
            if expected['image'] != args.image or expected['image_id'] != state['image_id']:
                raise ValueError('Existing image differs from portable cache; refusing to replace it')
        print(json.dumps({**state, 'action': 'reused'}, indent=2))
        return
    if args.cache:
        expected = cache_manifest(args.cache)
        if expected['image'] != args.image:
            raise ValueError('Image cache tag differs from requested image')
        env = dict(os.environ)
        if args.docker_host:
            env['DOCKER_HOST'] = args.docker_host
        with gzip.open(args.cache, 'rb') as stream:
            with subprocess.Popen([str(Path(args.docker).expanduser()), 'load'],
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE) as process:
                try:
                    shutil.copyfileobj(stream, process.stdin)
                    process.stdin.close()
                    error = process.stderr.read()
                    rc = process.wait()
                except BaseException:
                    process.terminate()
                    process.wait()
                    raise
        if rc:
            raise RuntimeError('docker load failed: ' + error.decode(errors='replace').strip())
        action = 'imported'
    else:
        build = ['build', '--platform', 'linux/amd64']
        if args.build_network:
            build.extend(('--network', args.build_network))
        for name in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                     'no_proxy', 'NO_PROXY'):
            if os.environ.get(name):
                build.extend(('--build-arg', name))
        build.extend(('-t', args.image, str(Path(args.bench).resolve())))
        docker(args, *build)
        expected = None
        action = 'built'
    state = preflight(args)
    if expected and state['image_id'] != expected['image_id']:
        raise RuntimeError('Imported image ID differs from cache manifest')
    print(json.dumps({**state, 'action': action}, indent=2))


def export(args):
    state = preflight(args)
    archive = Path(args.cache).resolve()
    manifest = archive.with_name(archive.name + '.json')
    if archive.exists() or manifest.exists():
        raise ValueError('Cache output already exists; choose a new path')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=archive.parent, prefix='.gameasg-image-', delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        env = dict(os.environ)
        if args.docker_host:
            env['DOCKER_HOST'] = args.docker_host
        with subprocess.Popen([str(Path(args.docker).expanduser()), 'save', args.image],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                with temporary.open('wb') as raw:
                    with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6, mtime=0) as zipped:
                        shutil.copyfileobj(process.stdout, zipped)
                error = process.stderr.read()
                rc = process.wait()
            except BaseException:
                process.terminate()
                process.wait()
                raise
        if rc:
            raise RuntimeError('docker save failed: ' + error.decode(errors='replace').strip())
        data = {'format': FORMAT, 'image': args.image, 'image_id': state['image_id'],
                'benchmark_commit': BENCH_COMMIT, 'archive_sha256': sha256(temporary)}
        temporary.replace(archive)
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        print(json.dumps({'archive': str(archive), **data}, indent=2))
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'install', 'export'))
    parser.add_argument('--bench', type=Path, default=DEFAULT_BENCH)
    parser.add_argument('--docker', default='docker', help='Docker CLI path or command')
    parser.add_argument('--docker-host', help='Docker daemon URI; otherwise use environment/default')
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--build-network', choices=('default', 'host', 'none'),
                        help='Docker build network mode; host is useful behind a host proxy')
    parser.add_argument('--cache', type=Path, help='Compressed image cache; required for export')
    parser.add_argument('--start-daemon', action='store_true', help='Install only: start private local dockerd if absent')
    parser.add_argument('--dockerd', default='dockerd', help='Private dockerd path or command')
    parser.add_argument('--state-dir', type=Path, default=Path('/tmp/gameasg-docker'),
                        help='Local private daemon data and socket directory')
    parser.add_argument('--bip', default='192.168.251.1/24', help='Private Docker bridge address')
    parser.add_argument('--address-pool', default='192.168.252.0/24', help='Private container address pool')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            if args.start_daemon:
                raise ValueError('--start-daemon is only valid with install')
            print(json.dumps(preflight(args), indent=2))
        elif args.command == 'install':
            install(args)
        else:
            if args.start_daemon:
                raise ValueError('--start-daemon is only valid with install')
            if not args.cache:
                raise ValueError('export requires --cache')
            export(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
