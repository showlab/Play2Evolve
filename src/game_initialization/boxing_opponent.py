"""Versioned opponent upgrade for the initialized boxing lab (not the full benchmark)."""
import argparse
import json
import shutil
from pathlib import Path
from game_initialization.package import inspect_package, separate
from gamecraft_behavior.core import write_json


def prepare(package, output):
    package,output=separate(package,output)
    meta,adapter,_=inspect_package(package)
    if adapter.task_id!='boxing-video-lab':raise ValueError('Expected initialized boxing lab')
    shutil.copytree(package,output,ignore=shutil.ignore_patterns('.godot','__pycache__','.git','checks'))
    case=Path(__file__).parent/'cases/boxing_opponent'
    for name in ('main.gd','combat.gd','rules.json'):shutil.copy2(case/name,output/'game'/name)
    for source,target in [('adapter.py','__init__.py'),('bridge.gd','bridge.gd')]:
        shutil.copy2(case/source,output/'adapter'/target)
    meta={k:v for k,v in meta.items() if k!='files'}
    meta.update(state='draft',version='v2-opponent',parent=str(package),
                opponent_source='Authored from public Sports Boxing Gym brief; explicit implementation choices in rules.json',
                asset_note='Reused H3 clips. Hook/uppercut use jab imagery plus procedural tells; dodges transform block imagery. No new H3 generation.',
                scope='Opponent mechanics and corresponding combat inputs only. Career, training mini-games and multi-round progression not implemented.')
    write_json(output/'initialization.json',meta)
    spec=json.loads((output/'composition_spec.json').read_text())
    spec['fixed']=list(dict.fromkeys(spec['fixed']+['combat.gd']))
    write_json(output/'composition_spec.json',spec)
    plan=json.loads((output/'game/game_plan.json').read_text())
    plan.update(scope=meta['scope'],opponent_version='v2-opponent')
    write_json(output/'game/game_plan.json',plan)
    scenarios=[]
    for purpose,seed in [('initialization',73),('train',100),('validation',1000),('confirmation',2000),('test',3000)]:
        for i,level in enumerate(('rookie','contender','champion')):
            scenarios.append({'id':f'bout_{level}_seed_{seed+i}','purpose':purpose,'step_frames':6,
                              'actions':(['jab','wait','block','wait','dodge_high','wait','hook','wait','dodge_low','wait','uppercut','wait']*5)})
    write_json(output/'scenarios.json',scenarios)
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(prepare(a.package,a.output))

if __name__=='__main__':main()
