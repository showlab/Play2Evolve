import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wan_video_backend as wan


class WanBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root/'ref.png'
        self.image.write_bytes(b'image')
        self.body = {'task':'t2va', 'prompt':'integrated_multimodal_description: [Shot 1] A bird flies.\n\noverall_soundscape: wind',
                     'seconds':4, 'seed':42, 'target':{'aspect_ratio':'16:9'}}
        self.env = patch.dict(os.environ, {'WAN_SHORT_EDGE':'480','WAN_STEPS':'30',
                                            'WAN_LOOP_POLICY':'prompt','WAN_PERSISTENT':'0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_text_mapping_preserves_seed_ratio_duration_and_strips_audio(self):
        r=wan.translate(self.body)
        self.assertEqual(r['prompt'],'A bird flies.')
        self.assertEqual(r['seed'],42)
        self.assertEqual((r['width'],r['height']),(848,480))
        self.assertEqual(r['frames']%4,1)
        self.assertLessEqual(abs(r['actual_seconds']-4),2/24)
        self.assertIsNone(r['image'])

    def loop_body(self):
        return {**self.body,'task':'fl2va','conditions':[
            {'type':'image','role':'keyframe','frame_index':i,'uri':str(self.image)} for i in (0,-1)]}

    def test_loop_has_explicit_approximation_and_first_frame(self):
        r=wan.translate(self.loop_body())
        self.assertEqual(r['loop_conditioning'],'prompt_only')
        self.assertEqual(r['image'],str(self.image))
        self.assertIn('Return to the opening',r['prompt'])

    def test_strict_policy_rejects_last_frame(self):
        with patch.dict(os.environ,{'WAN_LOOP_POLICY':'error'}), self.assertRaisesRegex(ValueError,'last-frame'):
            wan.translate(self.loop_body())

    def test_distinct_last_frame_is_not_silently_dropped(self):
        last=self.root/'last.png';last.write_bytes(b'different')
        body=self.loop_body();body['conditions'][1]['uri']=str(last)
        with self.assertRaisesRegex(ValueError,'distinct'):
            wan.translate(body)

    def test_invalid_conditions_and_duration(self):
        for body in [{**self.body,'seconds':float('nan')},
                     {**self.body,'task':'fl2va'},
                     {**self.loop_body(),'task':'t2va'},
                     {**self.body,'target':{'aspect_ratio':'0:1'}}]:
            with self.subTest(body=body),self.assertRaises(ValueError):
                wan.translate(body)

    def test_a14b_distilled_requires_image_and_uses_four_steps(self):
        with patch.dict(os.environ, {'WAN_MODEL_VARIANT':'i2v_a14b_distill4'}):
            with patch.dict(os.environ, {'WAN_STEPS':''}):
                del os.environ['WAN_STEPS']
                cfg=wan.settings()
                self.assertIn('I2V-A14B-distill4step-native',cfg['weights'])
                with self.assertRaisesRegex(ValueError,'requires a first-frame image'):
                    wan.translate(self.body,cfg['variant'])
                request=wan.translate(self.loop_body(),cfg['variant'])
                self.assertEqual(request['steps'],4)
                self.assertEqual(request['variant'],'i2v_a14b_distill4')

    def test_a14b_text_only_request_uses_separate_persistent_5b_service(self):
        out=self.root/'fallback.mp4'
        cfg={'python':'wan-python','source':'source','weights':'5b-weights',
             'device':'1','variant':'ti2v_5b'}
        def run(args,**kwargs):
            self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'],'1')
            self.assertEqual(kwargs['env']['WAN_MODEL_VARIANT'],'ti2v_5b')
            Path(args[-1]).write_bytes(b'generated')
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ,{'WAN_MODEL_VARIANT':'i2v_a14b_distill4',
                                     'WAN_T2V_FALLBACK_DEVICE':'1', 'WAN_T2V_STEPS':'30',
                                     'WAN_STEPS':'4'}), \
             patch.object(wan,'preflight',return_value=cfg) as preflight, \
             patch.object(wan.subprocess,'run',side_effect=run):
            wan.generate(self.body,out)
        preflight.assert_called_once_with('ti2v_5b')
        request=json.loads(out.with_suffix('.wan_request.json').read_text())
        self.assertEqual(request['variant'],'ti2v_5b')
        self.assertEqual(request['steps'],30)
        self.assertIsNone(request['image'])

    def test_a14b_fallback_requires_another_gpu(self):
        with patch.dict(os.environ,{'WAN_MODEL_VARIANT':'i2v_a14b_distill4',
                                     'WAN_CUDA_VISIBLE_DEVICES':'0',
                                     'WAN_T2V_FALLBACK_DEVICE':'0'}), \
             self.assertRaisesRegex(ValueError,'another GPU'):
            wan.settings('ti2v_5b')

    def test_worker_is_isolated_and_output_committed_on_success(self):
        out=self.root/'raw.mp4'
        cfg={'python':'wan-python','source':'source','weights':'weights','device':'1'}
        def run(args,**kwargs):
            self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'],'1')
            self.assertEqual(args[0],'wan-python')
            Path(args[-1]).write_bytes(b'generated')
            return SimpleNamespace(returncode=0)
        with patch.object(wan,'preflight',return_value=cfg),patch.object(wan.subprocess,'run',side_effect=run):
            wan.generate(self.body,out)
        self.assertEqual(out.read_bytes(),b'generated')
        self.assertEqual(json.loads(out.with_suffix('.wan_request.json').read_text())['seed'],42)

    def test_failed_worker_preserves_existing_output(self):
        out=self.root/'raw.mp4';out.write_bytes(b'old')
        with patch.object(wan,'preflight',return_value={'python':'p','source':'s','weights':'w','device':'1'}), \
             patch.object(wan.subprocess,'run',return_value=SimpleNamespace(returncode=1)), \
             self.assertRaisesRegex(RuntimeError,'Wan generation failed'):
            wan.generate(self.body,out)
        self.assertEqual(out.read_bytes(),b'old')

    def test_persistent_server_loads_pipeline_once_for_two_requests(self):
        sock=self.root/'wan.sock';lock=self.root/'wan.lock';log=self.root/'wan.log'
        cfg={'python':'wan-python','source':'source','weights':'weights','device':'1'}
        loads=[];renders=[]
        def render(pipe,request,path):
            renders.append(request['seed'])
            Path(path).write_bytes(b'video-'+str(request['seed']).encode())
        def load(request):
            loads.append(request.get('seed','warmup'))
            return object()
        with patch.dict(os.environ,{'WAN_PERSISTENT':'1','WAN_IDLE_SECONDS':'0'}), \
             patch.object(wan,'preflight',return_value=cfg), \
             patch.object(wan,'_runtime_paths',return_value=(lock,sock,log)), \
             patch.object(wan,'_load_pipeline',side_effect=load), \
             patch.object(wan,'_render',side_effect=render):
            thread=threading.Thread(target=wan.serve,args=(sock,),daemon=True)
            thread.start()
            for _ in range(100):
                if sock.exists():break
                time.sleep(.01)
            self.assertTrue(sock.exists())
            self.assertTrue(wan.warmup_server()['model_loaded'])
            self.assertTrue(wan.server_health()['model_loaded'])
            for seed in (42,43):
                wan.generate({**self.body,'seed':seed},self.root/f'{seed}.mp4')
            self.assertEqual(loads,['warmup'])
            self.assertEqual(renders,[42,43])
            self.assertEqual((self.root/'43.mp4').read_bytes(),b'video-43')
            self.assertTrue(wan.stop_server())
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_public_entry_routes_wan_only_when_selected(self):
        with patch.dict(os.environ,{'LLM_BACKEND':'local','OPTIMIZER_BACKEND':'local'}):
            import gen_assets
        with patch.dict(os.environ,{'H3_BACKEND':'wan'}),patch.object(wan,'generate') as generate:
            gen_assets.h3_video(self.body,'out.mp4')
            generate.assert_called_once_with(self.body,'out.mp4',timeout=gen_assets.H3_TIMEOUT)

if __name__ == '__main__':
    unittest.main()
