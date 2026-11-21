"""Local integration checks; no real model requests or training."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gamecraft_evaluation import direct

from play2evolve.runtime import benchmark_root
BENCH = benchmark_root()
# The benchmark is supplied externally via the release runtime configuration.
sys.path.insert(0, str(BENCH))
from gamecraft_bench.verifier.judges.base import JudgeResponse


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rubric = {'score_formula':'BUILD * (0.5*M1 + 0.5*A1)',
                      'build_check':{'id':'BUILD','cmd':'godot --headless --path /workspace/game --quit-after 5'},
                      'categories':[{'name':'Mechanics','items':['M1']},{'name':'Art','items':['A1']}],
                      'requirements':[{'id':'M1','description':'Moves','agg':'max'},
                                      {'id':'A1','description':'Art','agg':'mean'}],
                      'max_demos':10,'max_demo_seconds':20}

    def tearDown(self):
        self.tmp.cleanup()

    def video_setup(self):
        videos=[]
        for name in ['a','b']:
            p=self.root/f'{name}.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=64x64:r=30',
                            '-t','1','-c:v','libx264','-threads','1',str(p)],check=True)
            videos.append(str(p))
        direct.dump(self.root/'rubric.json',self.rubric)
        config={'mode':'score-videos','task':'test','videos':videos,'passes':2,'judge':'openai',
                'judge_model':'test','judge_label':'test','window_seconds':20,
                'interpretation':'video only','input_hashes':{Path(p).name:direct.sha(p) for p in videos}}
        direct.dump(self.root/'configuration.json',config)

    def test_video_real_sampling_and_max_mean_aggregation(self):
        self.video_setup()
        class FakeJudge:
            def score(self, req):
                assert len(req.frame_paths)==2
                assert len(req.requirements)==2
                s={'M1':0.2,'A1':1.0} if req.demo_id=='a' else {'M1':0.8,'A1':0.0}
                return JudgeResponse(s,raw=json.dumps({'scores':s}))
        with patch('gamecraft_bench.verifier.judges.get_judge',return_value=FakeJudge()):
            direct.worker(self.root)
        d=json.loads((self.root/'summary.json').read_text())
        self.assertAlmostEqual(d['video_rubric_mean'],0.65)
        self.assertNotIn('overall_mean',d)
        self.assertFalse(d['build_checked'])
        self.assertEqual(len(d['passes']),2)

    def test_missing_item_is_not_silently_zero(self):
        self.video_setup()
        class FakeJudge:
            def score(self, req):
                return JudgeResponse({'M1':1.0,'A1':0.0},raw='{"scores":{"M1":1}}')
        with patch('gamecraft_bench.verifier.judges.get_judge',return_value=FakeJudge()):
            with self.assertRaisesRegex(ValueError,'omitted'):
                direct.worker(self.root)
        self.assertFalse((self.root/'summary.json').exists())

    def test_project_uses_upstream_replay_and_build_gate(self):
        self.video_setup()
        project=self.root/'project';project.mkdir()
        (project/'project.godot').write_text('[application]\n')
        (project/'demo_outputs').mkdir()
        for name in ['a','b']:
            direct.dump(project/'demo_outputs'/f'{name}.json',{'duration_frames':30,'events':[]})
        c=json.loads((self.root/'configuration.json').read_text())
        c.update(mode='score-project',project=str(project),godot='/bin/true',
                 input_hashes=direct.tree_hash(project),passes=1)
        direct.dump(self.root/'configuration.json',c)
        from gamecraft_bench.verifier.replay import ReplayResult
        import shutil
        def replay(**kwargs):
            shutil.copyfile(self.root/f"{kwargs['trace_path'].stem}.mp4",kwargs['output_mp4'])
            return ReplayResult(kwargs['output_mp4'],1.0,0)
        class FakeJudge:
            model = 'test'
            def score(self,req):
                return JudgeResponse({'M1':1.0,'A1':0.5})
        with patch('gamecraft_bench.verifier.judges.get_judge',return_value=FakeJudge()), \
             patch('gamecraft_bench.verifier.score.replay_trace',side_effect=replay) as mocked:
            direct.worker(self.root)
        d=json.loads((self.root/'summary.json').read_text())
        self.assertEqual(mocked.call_count,2)
        self.assertAlmostEqual(d['overall_mean'],0.75)
        self.assertTrue(d['build_checked'])
        self.assertEqual(direct.tree_hash(project),c['input_hashes'])

    def test_invalid_values(self):
        for value in [float('nan'),True,1.1,-0.1,'0.5']:
            with self.assertRaises(ValueError):
                direct.checked_response(JudgeResponse({'M1':value}),['M1'])

    def test_build_failure_scores_zero_without_calling_judge(self):
        project=self.root/'project';project.mkdir()
        (project/'project.godot').write_text('[application]\n')
        (project/'demo_outputs').mkdir()
        direct.dump(project/'demo_outputs/a.json',{'duration_frames':30,'events':[]})
        direct.dump(self.root/'rubric.json',self.rubric)
        direct.dump(self.root/'configuration.json',{'mode':'score-project','task':'test',
            'project':str(project),'godot':'/bin/true','input_hashes':direct.tree_hash(project),
            'passes':1,'judge':'openai','judge_model':'test','judge_label':'test','interpretation':'test'})
        class NoCallJudge:
            model='test'
            def score(self,req):raise AssertionError('Build failed; judge should not run')
        with patch('gamecraft_bench.verifier.judges.get_judge',return_value=NoCallJudge()), \
             patch('gamecraft_bench.verifier.score._run_build_check',return_value=(False,'failed')):
            direct.worker(self.root)
        d=json.loads((self.root/'summary.json').read_text())
        self.assertEqual(d['overall_mean'],0)
        b=json.loads((self.root/'pass-01/breakdown.json').read_text())
        self.assertFalse(b['build_ok'])

    def test_empty_project_does_not_get_a_plausible_score(self):
        p=self.root/'game';p.mkdir();(p/'project.godot').write_text('[application]\n')
        args=argparse.Namespace(bench=BENCH,task='sports-boxing-gym',output=self.root/'out',
            command='score-project',project=p,passes=1,timeout=30,judge='openai',judge_model='local')
        with self.assertRaisesRegex(ValueError,'traces'):
            direct.run(args)
        self.assertFalse(args.output.exists())

    def test_existing_output_not_overwritten(self):
        p=self.root/'x.mp4';p.write_bytes(b'video')
        out=self.root/'out';out.mkdir();(out/'keep').write_text('preserve')
        args=argparse.Namespace(bench=BENCH,task='sports-boxing-gym',output=out,
            command='score-videos',videos=[p],passes=1,timeout=30,judge='openai',judge_model='local')
        with self.assertRaises(FileExistsError):direct.run(args)
        self.assertEqual((out/'keep').read_text(),'preserve')


if __name__=='__main__':unittest.main()
