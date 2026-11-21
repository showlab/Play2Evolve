import json,hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image
from gamecraft_assets.generate import generate
from gamecraft_assets.plans import validate_generic
from game_initialization.batch.unjudged import reuse
from game_initialization.batch.__main__ import write,read

class UnjudgedTests(unittest.TestCase):
 def test_no_judge_no_repair_no_quality_retry(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);asset={'id':'icon','kind':'sprite','description':'icon','requirements':['R1'],'prompt':'icon','size':[32,32],'frames':1,'fps':1}
   plan={'schema_version':1,'task_id':'test','style':'simple','assets':[asset]}
   write(root/'asset_plan.json',plan);write(root/'asset_manifest.json',{'assets':{}})
   write(root/'asset_history.json',[{'asset':'icon','version':1,'eligible':False,'verdict':{'failure':'wrong background'}}])
   def process(g,video,a,dest):
    p=dest/'frame0000.png';Image.new('RGBA',(32,32)).save(p)
    return [p],[],{'coverage':0,'in_frame':0,'bbox_fill':1}
   backend=Mock()
   with patch('gamecraft_assets.generate._backend',return_value=backend),patch('gamecraft_assets.generate._process',side_effect=process),patch('gamecraft_assets.generate._judge',side_effect=AssertionError('Judge called')),patch('gamecraft_assets.generate._repair_prompt',side_effect=AssertionError('Repair called')):
    self.assertEqual(generate(root,rounds=3,candidates=4,validator=validate_generic,judge=False),[])
   self.assertEqual(backend.h3_t2va.call_count,1)
   record=read(root/'asset_manifest.json')['assets']['icon']
   self.assertEqual(record['verdict'],{})
   self.assertEqual(record['quality_status'],'not_evaluated')
 def test_reuse_rejected_decodable_candidate(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);dest=root/'generated/icon/v0001';dest.mkdir(parents=True)
   asset={'id':'icon','frames':1};signature=hashlib.sha256(json.dumps(asset,sort_keys=True).encode()).hexdigest()
   Image.new('RGBA',(32,32)).save(dest/'frame0000.png')
   write(dest/'request.json',{'plan_sha256':signature,'version':1})
   write(dest/'result.json',{'eligible':False,'files':['generated/icon/v0001/frame0000.png'],'verdict':{'usable':0}})
   write(root/'asset_manifest.json',{'assets':{}})
   reuse(root,{'assets':[asset]})
   self.assertEqual(read(root/'asset_manifest.json')['assets']['icon']['quality_status'],'not_evaluated')
   self.assertFalse(read(dest/'result.json')['eligible'])
