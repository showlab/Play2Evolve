import argparse
import json
import os
import shutil
from pathlib import Path
from .package import inspect_package, freeze, checkout


def main():
    p = argparse.ArgumentParser(description='Check, freeze and load initialized game packages')
    p.add_argument('command', choices=['inspect', 'freeze', 'checkout'])
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--draft', action='store_true', help='Structural inspection only, not execution verification')
    p.add_argument('--godot', default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot'))
    a = p.parse_args()
    if a.command == 'inspect':
        m, adapter, cases = inspect_package(a.package, sealed=not a.draft)
        print(json.dumps({'task_id': adapter.task_id, 'version': m['version'], 'state': m.get('state'),
                          'actions': adapter.actions, 'scenarios': [c['id'] for c in cases]}))
    elif a.command == 'freeze':
        if not a.output or not a.godot: p.error('freeze requires --output and --godot')
        print(freeze(a.package, a.output, a.godot))
    else:
        if not a.output: p.error('checkout requires --output')
        project, _, _ = checkout(a.package, a.output)
        print(project)


if __name__ == '__main__':
    main()
