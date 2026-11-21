import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from game_initialization.batch import bridge
from game_initialization.batch.__main__ import write,read


class BridgeTests(unittest.TestCase):
    def test_roundtrip_exact_model_and_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);queue=root/'jobs';task=root/'boxing';task.mkdir()
            config={'tasks':['boxing'],'codex_queue':str(queue),'model':'gpt-5.5','timeout':5}
            write(root/'batch.json',config)
            received=[]
            def fake(task_arg,cfg,stage,prompt,schema=None):
                received.append((task_arg,cfg,stage,prompt))
                return {'ok':True}
            with patch.object(bridge,'codex',side_effect=fake):
                thread=threading.Thread(target=bridge.worker,args=(root,'/developer/codex'),daemon=True)
                thread.start()
                try:
                    answer=bridge.submit(task,config,'probe','exact prompt',{'type':'object'})
                    self.assertEqual(answer,{'ok':True})
                    self.assertEqual(received[0][1]['model'],'gpt-5.5')
                    self.assertNotIn('codex_queue',received[0][1])
                    self.assertEqual(received[0][1]['codex'],'/developer/codex')
                finally:
                    (root/'run.exit').write_text('0')
                    thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(read(queue/'worker_state.json')['state'],'stopped')

    def test_remote_error_propagated(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);queue=root/'jobs';task=root/'boxing';task.mkdir()
            cfg={'tasks':['boxing'],'codex_queue':str(queue),'model':'gpt-5.5','timeout':5}
            write(root/'batch.json',cfg)
            with patch.object(bridge,'codex',side_effect=RuntimeError('unavailable model')):
                thread=threading.Thread(target=bridge.worker,args=(root,'codex'),daemon=True)
                thread.start()
                try:
                    with self.assertRaisesRegex(RuntimeError,'unavailable model'):
                        bridge.submit(task,cfg,'plan','x',None)
                finally:
                    (root/'run.exit').write_text('1');thread.join(6)
