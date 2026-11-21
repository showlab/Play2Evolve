import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamecraft_assets.plans import CASE, initialize, validate, validate_generic, write_json, revise
from gamecraft_assets.export import export_game
from gamecraft_assets.generate import acceptable, repair_viable, generate, _h3_with_oom_wait
from gamecraft_assets.contract import compile_generation_prompt, descriptor, generation_contract, validate_bound

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class AssetTests(unittest.TestCase):
    def test_wan_static_material_uses_planned_short_source_clip(self):
        from PIL import Image
        skyline = next(a for a in self.plan['assets'] if a['id'] == 'skyline')
        skyline['source_duration_seconds'] = .2
        write_json(self.workspace / 'asset_plan.json', self.plan)
        submitted = []
        fake = SimpleNamespace(h3_t2va=lambda *args, **kwargs: submitted.append(kwargs))
        def process(_backend, _video, asset, dest):
            frame = dest / 'frame0000.png'
            Image.new('RGB', tuple(asset['size'])).save(frame)
            return [frame], [], {'coverage': 1, 'in_frame': 1}
        with patch.dict('os.environ', {'H3_BACKEND': 'wan'}), \
             patch('gamecraft_assets.generate._backend', return_value=fake), \
             patch('gamecraft_assets.generate._process', side_effect=process):
            self.assertEqual(generate(self.workspace, only=['skyline'], rounds=1, candidates=1,
                                      validator=validate_generic, judge=False), [])
        self.assertEqual(submitted, [{'aspect': '16:9', 'seconds': .2}])
        request = json.loads((self.workspace / 'generated/skyline/v0001/h3_request.json').read_text())
        self.assertEqual(request['seconds'], .2)
        self.assertEqual(request['target']['duration_seconds'], .2)

    def test_h3_oom_wait_retries_only_resource_failures(self):
        attempts = []
        def transient():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('CUDA out of memory')
            return 'generated'
        with patch.dict('os.environ', {'GAMECRAFT_H3_OOM_WAIT_SECONDS':'5',
                                        'GAMECRAFT_H3_OOM_RETRY_SECONDS':'1'}), \
             patch('gamecraft_assets.generate.time.sleep') as sleep:
            self.assertEqual(_h3_with_oom_wait(transient), 'generated')
            sleep.assert_called_once()
        self.assertEqual(len(attempts), 2)

        with self.assertRaisesRegex(RuntimeError, 'invalid prompt'):
            _h3_with_oom_wait(lambda: (_ for _ in ()).throw(RuntimeError('invalid prompt')))

        with patch.dict('os.environ', {'GAMECRAFT_H3_OOM_WAIT_SECONDS':'0'}):
            with self.assertRaisesRegex(RuntimeError, 'remained out of memory'):
                _h3_with_oom_wait(lambda: (_ for _ in ()).throw(RuntimeError('CUDA out of memory')))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        instruction = self.root / 'instruction.md'
        instruction.write_text('# Dread Wings\nOne-button endless flyer')
        self.workspace = initialize(instruction, self.root / 'workspace')
        self.plan = json.loads((self.workspace / 'asset_plan.json').read_text())

    def populate(self):
        assets = {}
        for a in self.plan['assets']:
            files = []
            for i in range(a['frames']):
                p = self.workspace / 'generated' / a['id'] / f'frame{i:04d}.png'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(PNG)
                files.append(str(p.relative_to(self.workspace)))
            assets[a['id']] = {**a, 'files': files, 'version': 1,
                'verdict': {'usable': .9, 'identity': .9, 'matte': .9},
                'metrics': {'coverage': .2, 'in_frame': 1, 'bbox_fill': .4},
                'plan_sha256': hashlib.sha256(json.dumps(a, sort_keys=True).encode()).hexdigest()}
        write_json(self.workspace / 'asset_manifest.json', {'assets': assets})

    def test_design_is_case_specific_and_path_safe(self):
        self.assertEqual(self.plan['task_id'], 'platformer-dread-wings')
        self.plan['assets'][0]['id'] = '../escape'
        with self.assertRaises(ValueError): validate(self.plan)

    def test_gate_rejects_bad_matte_and_invalid_score(self):
        v = {'usable': .9, 'identity': .9, 'matte': .9}
        m = {'coverage': .2, 'in_frame': 1, 'bbox_fill': .4}
        self.assertTrue(acceptable(v, m, 'sprite'))
        self.assertFalse(acceptable({**v, 'matte': .2}, m, 'sprite'))
        self.assertFalse(acceptable({**v, 'usable': float('nan')}, m, 'background'))
        self.assertFalse(acceptable(v, {**m, 'coverage': 0}, 'animation'))
        self.assertFalse(acceptable(v, {**m, 'bbox_fill': .98}, 'animation'))

    def test_repair_viability_admits_imperfect_candidate_but_rejects_broken_geometry(self):
        v={'usable':.48,'identity':.94,'matte':.25}
        m={'coverage':.02,'in_frame':.8,'bbox_fill':.7}
        self.assertFalse(acceptable(v,m,'animation'))
        self.assertTrue(repair_viable(v,m,'animation'))
        self.assertFalse(repair_viable(v,{**m,'coverage':0},'animation'))
        self.assertFalse(repair_viable({**v,'matte':float('nan')},m,'animation'))

    def test_rgba_contract_preserves_dark_opaque_pixels(self):
        from PIL import Image
        asset={'id':'fighter','kind':'sprite','size':[16,16],'frames':1,'fps':1}
        image=Image.new('RGBA',(16,16),(0,0,0,0))
        image.putpixel((8,8),(2,2,2,255))
        image.save(self.root/'fighter.png')
        self.assertEqual(descriptor(asset)['alpha_mode'],'rgba')
        self.assertEqual(validate_bound(self.root,asset,['fighter.png'])['anchor'],'bottom_center')
        self.assertEqual(Image.open(self.root/'fighter.png').getpixel((8,8)),(2,2,2,255))

    def test_export_requires_all_assets_and_preserves_existing_output(self):
        with self.assertRaises(ValueError): export_game(self.workspace, self.root / 'game')
        self.populate()
        game = export_game(self.workspace, self.root / 'game')
        self.assertIn('SpriteFrames', (game / 'assets/bird/animations.tres').read_text())
        self.assertEqual(len(list((game / 'demo_outputs').glob('*.json'))), 2)
        with self.assertRaises(FileExistsError): export_game(self.workspace, game)

    def test_export_rejects_manifest_escape(self):
        self.populate()
        p = self.workspace / 'asset_manifest.json'
        data = json.loads(p.read_text())
        data['assets']['pipe']['files'] = ['../instruction.md']
        write_json(p, data)
        with self.assertRaises(ValueError): export_game(self.workspace, self.root / 'game')

    def test_accepted_assets_are_cached_without_model_calls(self):
        self.populate()
        with patch('gamecraft_assets.generate._backend', return_value=object()):
            self.assertEqual(generate(self.workspace), [])

    def test_revision_invalidates_export_without_deleting_old_assets(self):
        self.populate()
        old = json.loads((self.workspace / 'asset_manifest.json').read_text())
        revise(self.workspace, 'bird', 'Keep body centered during flapping.')
        self.assertEqual(json.loads((self.workspace / 'asset_manifest.json').read_text()), old)
        self.assertTrue((self.workspace / 'plan_revisions.json').exists())
        with self.assertRaises(ValueError): export_game(self.workspace, self.root / 'game')

    def test_interrupted_version_directory_is_not_overwritten(self):
        abandoned = self.workspace / 'generated/pipe/v0001'
        abandoned.mkdir(parents=True)
        (abandoned / 'request.json').write_text('preserve')
        with patch('gamecraft_assets.generate._backend', return_value=object()):
            self.assertEqual(generate(self.workspace, ['pipe'], rounds=1, candidates=1), ['pipe'])
        self.assertEqual((abandoned / 'request.json').read_text(), 'preserve')
        history = json.loads((self.workspace / 'asset_history.json').read_text())
        self.assertEqual(history[0]['version'], 2)

    def test_generation_failure_preserves_old_version_and_reports_stale(self):
        self.populate()
        p = self.workspace / 'asset_plan.json'
        plan = json.loads(p.read_text())
        plan['assets'][1]['prompt'] += ' Revised constraint.'
        write_json(p, plan)
        with patch('gamecraft_assets.generate._backend', return_value=object()):
            pending = generate(self.workspace, ['pipe'], rounds=1, candidates=1)
        self.assertEqual(pending, ['pipe'])
        data = json.loads((self.workspace / 'asset_manifest.json').read_text())
        self.assertEqual(data['assets']['pipe']['version'], 1)
        self.assertTrue(json.loads((self.workspace / 'asset_history.json').read_text())[0]['error'])


    def test_scope_contract_keeps_legacy_static_and_supports_joint_action(self):
        skyline = self.plan['assets'][2]
        self.assertEqual(generation_contract(skyline)['motion'], 'static')
        joint = {'id':'rope_action','kind':'animation','description':'boxer turning a jump rope',
                 'requirements':'Hands, rope and jumps stay synchronized',
                 'prompt':'One boxer completes three jumps with a rope',
                 'size':[256,256],'frames':12,'fps':3,'loop':True,
                 'reference_prompt':'Full body {desc}',
                 'generation_scope':'coupled_action','motion':'loop',
                 'scope_reason':'The hands, rope, and feet must move together.',
                 'includes':['boxer','jump rope'],'game_controlled':['score','input timing']}
        validate_generic({'schema_version':1,'task_id':'test','assets':[joint]})
        contract = generation_contract(joint)
        self.assertEqual(contract['representation'], 'coupled_sequence')
        self.assertIn('all interacting subjects and props', compile_generation_prompt('painted',joint))
        broken = {**joint,'motion':'event'}
        with self.assertRaisesRegex(ValueError,'disagree'):
            validate_generic({'schema_version':1,'task_id':'test','assets':[broken]})

    def test_animated_scene_keeps_frames_and_conditions_on_accepted_material(self):
        from PIL import Image
        self.populate()
        skyline = next(a for a in self.plan['assets'] if a['id'] == 'skyline')
        source = self.workspace / 'generated/skyline/frame0000.png'
        Image.new('RGB',(32,32),(20,30,40)).save(source)
        scene = {'id':'sky_motion','kind':'background','description':'moving distant haze',
                 'requirements':'Haze drifts without obstructing the flight corridor',
                 'prompt':'Slow haze drift behind the play area',
                 'size':[32,32],'frames':4,'fps':1,
                 'generation_scope':'scene','motion':'loop',
                 'scope_reason':'The haze should change within the scene over time.',
                 'game_controlled':['bird','pipes','score'],
                 'reference_asset_id':skyline['id']}
        self.plan['assets'].append(scene)
        write_json(self.workspace/'asset_plan.json',self.plan)
        submitted=[]
        fake=SimpleNamespace(h3_video=lambda body,out:submitted.append(body))
        def process(_backend,_video,asset,dest):
            files=[]
            for i in range(asset['frames']):
                p=dest/f'frame{i:04d}.png'
                Image.new('RGB',(32,32),(i,0,0)).save(p)
                files.append(p)
            return files,[],{'coverage':1,'in_frame':1}
        with patch('gamecraft_assets.generate._backend',return_value=fake), \
             patch('gamecraft_assets.generate._process',side_effect=process):
            self.assertEqual(generate(self.workspace,only=['sky_motion'],rounds=1,candidates=1,
                                      validator=validate_generic,judge=False),[])
        self.assertEqual(len(submitted),1)
        self.assertEqual(submitted[0]['task'],'fl2va')
        self.assertEqual([c['frame_index'] for c in submitted[0]['conditions']],[0,-1])
        self.assertEqual(submitted[0]['target']['aspect_ratio'],'16:9')
        with Image.open(submitted[0]['conditions'][0]['uri']) as reference:
            self.assertEqual(reference.size,(768,432))
        record=json.loads((self.workspace/'asset_manifest.json').read_text())['assets']['sky_motion']
        self.assertEqual(len(record['files']),4)
        self.assertTrue((self.workspace/'generated/sky_motion/v0001/h3_request.json').exists())

    def test_later_repair_keeps_bootstrap_reference_available(self):
        from PIL import Image
        from gamecraft_training.package.edits import Generator
        package=self.root/'sealed';source=package/'game/assets/skyline/frame0000.png'
        source.parent.mkdir(parents=True)
        Image.new('RGB',(32,32),(20,30,40)).save(source)
        scene={'id':'sky_motion','kind':'background','description':'moving sky',
               'requirements':'a moving sky','prompt':'drifting clouds',
               'size':[32,32],'frames':4,'fps':1,'generation_scope':'scene',
               'motion':'loop','scope_reason':'Clouds move independently.',
               'game_controlled':['bird'],'reference_asset_id':'skyline'}
        plan={'schema_version':1,'task_id':'test','style':'painted',
              'assets':[self.plan['assets'][2],scene]}
        config={'_package_root':str(package),
                'assets':{'bindings':{'skyline':['assets/skyline/frame0000.png']}}}
        generator=Generator(self.root/'repair',plan,config,'python')
        record=json.loads((generator.root/'asset_manifest.json').read_text())['assets']['skyline']
        self.assertTrue((generator.root/record['files'][0]).is_file())
        self.assertEqual(record['plan_sha256'],hashlib.sha256(
            json.dumps(self.plan['assets'][2],sort_keys=True).encode()).hexdigest())

    def test_coupled_action_uses_one_foreground_h3_request(self):
        from PIL import Image
        self.populate()
        source=self.workspace/'generated/bird/frame0000.png'
        Image.new('RGBA',(32,32),(80,80,80,255)).save(source)
        joint={'id':'bird_wings_and_sparks','kind':'animation',
               'description':'bird and trailing sparks','requirements':'sparks track the wingbeats',
               'prompt':'One metallic bird flaps with synchronized sparks',
               'size':[32,32],'frames':4,'fps':1,'loop':True,
               'generation_scope':'coupled_action','motion':'loop',
               'scope_reason':'The sparks must remain attached to moving wings.',
               'includes':['bird','sparks'],'game_controlled':['flight physics'],
               'reference_asset_id':'bird'}
        self.plan['assets'].append(joint)
        write_json(self.workspace/'asset_plan.json',self.plan)
        submitted=[]
        fake=SimpleNamespace(h3_video=lambda body,out:submitted.append(body))
        def process(_backend,_video,asset,dest):
            files=[]
            for i in range(asset['frames']):
                p=dest/f'frame{i:04d}.png'
                Image.new('RGBA',(32,32),(1,2,3,255)).save(p)
                files.append(p)
            return files,[],{'coverage':.2,'in_frame':1,'bbox_fill':.3}
        with patch('gamecraft_assets.generate._backend',return_value=fake), \
             patch('gamecraft_assets.generate._process',side_effect=process):
            self.assertEqual(generate(self.workspace,only=[joint['id']],rounds=1,candidates=1,
                                      validator=validate_generic,judge=False),[])
        self.assertEqual(len(submitted),1)
        self.assertEqual(submitted[0]['task'],'fl2va')
        self.assertEqual(submitted[0]['target']['aspect_ratio'],'1:1')
        self.assertEqual(len(submitted[0]['conditions']),2)


if __name__ == '__main__':
    unittest.main()
