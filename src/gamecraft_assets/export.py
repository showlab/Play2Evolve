"""Export accepted assets plus an explicit case-specific initial game template."""
import hashlib
import json
import shutil
from pathlib import Path
from .plans import CASE, validate, write_json
from .generate import acceptable


def export_game(workspace, output):
    root, output = Path(workspace).resolve(), Path(output).resolve()
    if root == output or root in output.parents:
        raise ValueError('Game output must be outside the asset workspace')
    plan = json.loads((root / 'asset_plan.json').read_text())
    validate(plan)
    manifest = json.loads((root / 'asset_manifest.json').read_text())
    if set(manifest['assets']) != {'bird', 'pipe', 'skyline'}:
        raise ValueError('All three assets must be accepted before export')
    for asset in plan['assets']:
        expected = hashlib.sha256(json.dumps(asset, sort_keys=True).encode()).hexdigest()
        if manifest['assets'][asset['id']].get('plan_sha256') != expected:
            raise ValueError('Accepted assets are stale relative to the current plan; finish regeneration first')
    for asset in manifest['assets'].values():
        if not acceptable(asset.get('verdict', {}), asset.get('metrics', {}), asset['kind']):
            raise ValueError('An asset fails the current quality checks')
    files = []
    for name, a in manifest['assets'].items():
        if len(a['files']) != a['frames']:
            raise ValueError('Manifest frame count mismatch')
        for rel in a['files']:
            source = (root / rel).resolve()
            if root not in source.parents or not source.is_file():
                raise ValueError('Missing frame or manifest path outside workspace')
            files.append((source, Path('assets') / name / source.name))
    output.mkdir(parents=True, exist_ok=False)
    for source, rel in files:
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    bird = manifest['assets']['bird']
    lines = [f'[gd_resource type="SpriteFrames" load_steps={len(bird["files"])+1} format=3]']
    for i, rel in enumerate(bird['files']):
        lines.append(f'[ext_resource type="Texture2D" path="res://assets/bird/{Path(rel).name}" id="{i+1}"]')
    frames = ',\n'.join('{"duration": 1.0, "texture": ExtResource("%d")}' % (i+1) for i in range(len(bird['files'])))
    lines += ['[resource]', 'animations = [{"frames": [' + frames + '], "loop": ' + str(bird['loop']).lower() + ', "name": &"fly", "speed": ' + str(float(bird['fps'])) + '}]']
    (output / 'assets/bird/animations.tres').write_text('\n\n'.join(lines)+'\n')
    (output / 'project.godot').write_text('''config_version=5
[application]
config/name="Dread Wings"
run/main_scene="res://Main.tscn"
[display]
window/size/viewport_width=1280
window/size/viewport_height=720
window/stretch/mode="canvas_items"
[rendering]
renderer/rendering_method="gl_compatibility"
''')
    (output / 'Main.tscn').write_text('''[gd_scene load_steps=2 format=3]
[ext_resource type="Script" path="res://main.gd" id="1"]
[node name="DreadWings" type="Node2D"]
script = ExtResource("1")
''')
    shutil.copyfile(CASE / 'main.gd', output / 'main.gd')
    shutil.copyfile(root / 'game_plan.json', output / 'game_plan.json')
    exported_manifest = json.loads(json.dumps(manifest))
    for name, a in exported_manifest['assets'].items():
        a['source_files'] = a['files']
        a['files'] = [f'assets/{name}/{Path(rel).name}' for rel in a['files']]
    write_json(output / 'asset_manifest.json', exported_manifest)
    # Fixed diagnostic input, not a learned policy or proof of task completion.
    demos = output / 'demo_outputs'
    demos.mkdir()
    write_json(demos / '01_title_flow.json', {'scenario': 'title_flow', 'duration_frames': 360,
        'events': [{'frame': f, 'type': 'key_press', 'keycode': 'SPACE'} for f in [30, 60, 78, 96, 114, 132, 150, 168, 186, 204, 280]]})
    write_json(demos / '02_ready.json', {'scenario': 'ready', 'duration_frames': 360,
        'events': [{'frame': f, 'type': 'key_press', 'keycode': 'SPACE'} for f in range(10, 220, 18)]})
    return output
