"""Reproducible initialization examples; not an automatic task-to-game generator."""
import argparse
import json
import shutil
from pathlib import Path
from gamecraft_behavior.core import write_json
from gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter


def base(output, task, origin):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output/'adapter').mkdir()
    write_json(output/'initialization.json', {'schema_version':1,'task_id':task,'version':'v0',
               'state':'draft','origin':origin,'generator':'Codex-authored initialization in this workspace',
               'framework':'game_initialization v1 / GameAdapter v1'})
    return output


def bird(project, plan, output):
    b=base(output,'platformer-dread-wings',{'game':str(Path(project).resolve()),'assets':'existing accepted H3 bird/pipe/skyline; previously refined'})
    shutil.copytree(project,b/'game',ignore=shutil.ignore_patterns('.godot','__pycache__','.git'))
    shutil.copy2(DreadWingsAdapter().bridge_script,b/'adapter/bridge.gd')
    (b/'adapter/__init__.py').write_text('from pathlib import Path\nfrom gamecraft_behavior.adapters.dread_wings import DreadWingsAdapter\ndef create_adapter():\n    a = DreadWingsAdapter()\n    a.bridge_script = Path(__file__).with_name("bridge.gd")\n    return a\n')
    shutil.copy2(plan,b/'asset_plan.json')
    write_json(b/'composition_spec.json',{'mutable':{},'fixed':['game mechanics'],
               'note':'Existing bird case imported to verify loading; no new composition optimization enabled'})
    write_json(b/'scenarios.json',[{'id':'ready','purpose':'initialization','step_frames':6,
                                  'actions':['tap','wait','tap','wait']}])
    return b


def boxing(assets_root, output):
    from PIL import Image
    b=base(output,'boxing-video-lab',{'assets':str(Path(assets_root).resolve()),
        'note':'Reuse existing H3 martial-artist clips (not freshly generated boxing-glove assets); programmatic ring background; not a benchmark submission'})
    root=Path(assets_root).resolve()
    manifest=json.loads((root/'A/manifest.json').read_text())
    case=Path(__file__).parent/'cases/boxing'
    g=b/'game';g.mkdir()
    shutil.copy2(case/'main.gd',g/'main.gd')
    shutil.copy2(case/'bridge.gd',b/'adapter/bridge.gd')
    shutil.copy2(case/'adapter.py',b/'adapter/__init__.py')
    (g/'project.godot').write_text('config_version=5\n[application]\nconfig/name="Boxing Video Lab"\nrun/main_scene="res://Main.tscn"\n[display]\nwindow/size/viewport_width=1280\nwindow/size/viewport_height=720\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
    (g/'Main.tscn').write_text('[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://main.gd" id="1"]\n[node name="BoxingLab" type="Node2D"]\nscript=ExtResource("1")\n')
    assets={}; plan=[]
    for action, old in [('idle','idle'),('jab','punch'),('block','block'),('hit','hit')]:
        item=manifest[old];relative=Path(item['dir'])
        # Legacy manifest paths are rooted at code_world/assets.
        source=root/Path(*relative.parts[1:]) if relative.parts[0]=='assets' else root/relative
        dest=g/'assets'/action;dest.mkdir(parents=True)
        files=[]
        for i in range(item['frames']):
            file=f'frame{i:04}.png';shutil.copy2(source/file,dest/file);files.append(f'assets/{action}/{file}')
        assets[action]={'files':files,'source':str(source),'kind':'existing_h3_animation','frames':len(files)}
        plan.append({'id':action,'kind':'animation','source':str(source),'frames':len(files),
                     'identity':'same side-view martial artist, red outfit, facing right',
                     'requirements':'Fixed feet/body anchor, full silhouette in frame, coherent identity; action begins/ends at compatible stance',
                     'prompt':{'idle':'Hold a fighting stance with a subtle breathing loop.',
                     'jab':'Throw one straight punch and return to stance; clear extension and contact pose.',
                     'block':'Raise guard visibly before impact, hold guard, then return to stance.',
                     'hit':'React to one torso impact then return to stance; fixed feet.'}[action]})
    with Image.open(g/'assets/idle/frame0000.png') as im:
        bbox=im.convert('RGBA').getchannel('A').getbbox()
        if bbox is None:raise ValueError('Empty initial idle sprite')
        anchor=[(bbox[0]+bbox[2])/2/im.width,bbox[3]/im.height]
        scale=330/(bbox[3]-bbox[1])
    composition={'scale':scale,'anchor':anchor,'actor_x':[500,780],'ground_y':610,
                 'clips':{k:{'hold_ticks':0,'start_frame':0,'offset_y':0} for k in assets}}
    write_json(g/'composition.json',composition)
    write_json(g/'rules.json',{'durations':{'idle':48,'jab':48,'block':36,'hit':24},
               'costs':{'jab':12,'block':6},'contact_tick':24,'damage':10,'regen_per_tick':.08,
               'block_recoil_ticks':18,'opponent_interval_ticks':84,'bout_ticks':720,
               'range_policy':'fixed fighting positions always in strike range; no movement in this initial case'})
    write_json(g/'game_plan.json',{'task_id':'boxing-video-lab','scope':'minimal video-interaction lab; not the benchmark game',
               'rules':'rules.json','presentation':'composition.json','viewport':[1280,720]})
    write_json(g/'asset_manifest.json',{'task_id':'boxing-video-lab','clips':assets})
    write_json(b/'asset_plan.json',{'schema_version':1,'task_id':'boxing-video-lab','assets':plan,
               'initial_assets':'reused existing H3 generation; no new H3 requests'})
    write_json(b/'composition_spec.json',{'file':'game/composition.json',
              'fixed':['game/rules.json','adapter/'],
              'mutable':{'scale':[scale*.8,scale*1.2],'ground_y':[580,630],
                         'clips.*.hold_ticks':[0,12],'clips.*.start_frame':[0,6],'clips.*.offset_y':[-20,20]},
              'checks':['visual contact versus contact_tick','guard pose versus block interval','feet alignment','clip transition continuity'],
              'status':'declared interface; automated composition optimizer integration pending'})
    write_json(b/'scenarios.json',[{'id':'bout_seed_100','purpose':'initialization','step_frames':6,
                  'actions':['jab','wait','wait','wait','block','wait','wait','wait']*8},
                 {'id':'bout_seed_2000','purpose':'test','step_frames':6,'actions':['wait']*60}])
    return b


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('case',choices=['bird','boxing']);p.add_argument('--output',required=True)
    p.add_argument('--project');p.add_argument('--asset-plan');p.add_argument('--assets-root')
    a=p.parse_args()
    if a.case=='bird':
        if not a.project or not a.asset_plan:p.error('bird needs --project and --asset-plan')
        print(bird(a.project,a.asset_plan,a.output))
    else:
        if not a.assets_root:p.error('boxing needs --assets-root')
        print(boxing(a.assets_root,a.output))


if __name__=='__main__':main()
