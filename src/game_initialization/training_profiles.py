"""Initialization-time examples. Game-specific choices live here/in the package,
never in the shared training loop. Existing frozen v0 packages are not changed.
"""
import argparse
import json
import shutil
from pathlib import Path
from gamecraft_behavior.core import write_json
from game_initialization.package import inspect_package, separate


def prepare(source,output):
    source,output=separate(source,output)
    meta,adapter,_=inspect_package(source)
    shutil.copytree(source,output,ignore=shutil.ignore_patterns('.godot','__pycache__','.git','checks'))
    meta={k:v for k,v in meta.items() if k!='files'}
    meta.update(state='draft',version='v1',parent=str(source),training_contract='three-side-v1')
    write_json(output/'initialization.json',meta)
    task=adapter.task_id
    plan=json.loads((output/'asset_plan.json').read_text())
    plan.setdefault('style','Readable side-view game art, coherent identity, fixed camera, isolated sprites on pure green background.')
    script=output/'adapter/__init__.py'
    text=script.read_text()
    if task=='platformer-dread-wings':
        from gamecraft_training.seeds import install
        install(output/'game')
        prefix='ready_seed_'
        text=text.replace('    return a', '    a.behavior_metrics = lambda result: {"points": result["replay"]["score"], "survival_seconds": result["replay"]["survival_seconds"]}\n    return a')
        metrics=[{'id':'points','direction':'max','min_gain':0},{'id':'survival_seconds','direction':'max','min_gain':.25}]
        manifest=json.loads((output/'game/asset_manifest.json').read_text())
        bindings={key:asset['files'] for key,asset in manifest['assets'].items()}
        write_json(output/'game/composition.json',{'bird_scale':1.0,'bird_offset_y':0.0,'animation_speed':1.0})
        main=output/'game/main.gd';s=main.read_text()
        s=s.replace('var plan: Dictionary','var presentation: Dictionary\nvar plan: Dictionary',1)
        s=s.replace('    cfg = plan.mechanics', '    cfg = plan.mechanics\n    presentation = JSON.parse_string(FileAccess.get_file_as_string("res://composition.json"))',1)
        s=s.replace('float(cfg.player_size[1]) / 192.0)', 'float(cfg.player_size[1]) / 192.0) * float(presentation.bird_scale)')
        s=s.replace('    bird.play("fly")\n    add_child(bird)', '    bird.speed_scale = float(presentation.animation_speed)\n    bird.play("fly")\n    add_child(bird)',1)
        s=s.replace('bird.position = Vector2(float(cfg.player_position[0]), bird_y)', 'bird.position = Vector2(float(cfg.player_position[0]), bird_y + float(presentation.bird_offset_y))')
        main.write_text(s)
        pars={key:{'file':'composition.json','path':[key],'type':'float','min':lo,'max':hi}
              for key,lo,hi in [('bird_scale',.8,1.2),('bird_offset_y',-8,8),('animation_speed',.5,2)]}
        criteria=[{'id':'asset_quality','description':'Generated bird, obstacle and background have coherent identity, clean silhouettes and readable motion.'},
                  {'id':'composition_quality','description':'Rendered bird boundaries align with the fixed collision body; sprite scale/offset and animation timing fit observed flight.'},
                  {'id':'readability','description':'Bird and obstacle gaps remain distinguishable; UI does not obscure the trajectory.'}]
        init_actions=['tap','wait']*4
        references={}
        # Preserve the identity reference from the existing initialization provenance.
        origin=output/'references/bird-source.png'
        if origin.is_file():
            (output/'references').mkdir(exist_ok=True);shutil.copy2(origin,output/'references/bird.png');references={'bird':'references/bird.png'}
    elif task=='boxing-video-lab':
        prefix='bout_seed_'
        text=text.replace('def create_adapter():\n    return BoxingAdapter()', '''def create_adapter():
    adapter = BoxingAdapter()
    adapter.behavior_metrics = lambda result: {
        'win_rate': {'win': 1, 'draw': .5, 'playing': .5, 'loss': 0}[result['replay']['outcome']],
        'hp_balance': result['replay']['player_hp']-result['replay']['opponent_hp']}
    return adapter''')
        metrics=[{'id':'win_rate','direction':'max','min_gain':0},{'id':'hp_balance','direction':'max','min_gain':1}]
        manifest=json.loads((output/'game/asset_manifest.json').read_text())
        bindings={key:asset['files'] for key,asset in manifest['clips'].items()}
        from PIL import Image
        for a in plan['assets']:
            with Image.open(output/'game'/bindings[a['id']][0]) as im:a['size']=list(im.size)
            a.update(fps=30,loop=a['id']=='idle',description='A male martial artist in a red gi, black belt, side view facing right',
                     reference_prompt='Full-body side view of {desc}, holding a fighting stance. Fixed camera, pure green #00FF00 background, generous margins, entire body visible.')
        plan['schema_version']=1
        pars={}
        for clip in ('idle','jab','block','hit'):
            for field,lo,hi in [('hold_ticks',0,12),('start_frame',0,6),('offset_y',-20,20)]:
                pars[f'{clip}_{field}']={'file':'composition.json','path':['clips',clip,field],
                                       'type':'int','min':lo,'max':hi}
        scale=json.loads((output/'game/composition.json').read_text())['scale']
        pars['scale']={'file':'composition.json','path':['scale'],'type':'float','min':scale*.8,'max':scale*1.2}
        criteria=[{'id':'asset_quality','description':'Fighter identity and silhouette remain coherent across idle, jab, block and hit clips; no cropping or sudden body drift.'},
                  {'id':'composition_quality','description':'Visible fist contact agrees with damage/HP timing; guard appears during actual block; feet align with floor and transitions avoid jumps.'},
                  {'id':'readability','description':'Opponent attack tells, HP/stamina feedback and left/right fighter roles are readable during interaction.'}]
        init_actions=['jab','wait','wait','wait','block','wait']*2
        references={}
        origin=output/'references/fighter-source.png'
        if origin.is_file():
            (output/'references').mkdir(exist_ok=True);shutil.copy2(origin,output/'references/fighter.png')
            references={a['id']:'references/fighter.png' for a in plan['assets']}
    else:raise ValueError('This initialization helper has examples only for bird and boxing; other packages supply their own training.json')
    script.write_text(text)
    write_json(output/'asset_plan.json',plan)
    write_json(output/'composition_spec.json',{'mutable':pars,'parameters':pars,'fixed':['game_plan.json','rules.json','main.gd']})
    write_json(output/'training.json',{'schema_version':1,'behavior':{'metrics':metrics},
               'selection':{'mode':'adaptive_pairwise_v1','max_reviews':2},
               'repair_verification':{'mode':'blind_issue_v1'},
               'assets':{'bindings':bindings,'references':references,'quality':{'min_usable':.7,'min_in_frame':.95}},
               'quality':{'criteria':criteria,'targets':{'asset':'asset_quality','composition':'composition_quality'},
                          'min_gain':.05,'regression_tolerance':.02}})
    write_json(output/'scenarios.json',[{'id':prefix+str(seed),'purpose':purpose,'step_frames':6,'actions':init_actions}
               for purpose,seed in [('initialization',73),('train',100),('validation',1000),('confirmation',2000),('test',3000)]])
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(prepare(a.package,a.output))


if __name__=='__main__':main()
