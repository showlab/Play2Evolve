import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class BoxingOpponentTests(unittest.TestCase):
    def test_adapter_exposes_current_tells_and_requires_complete_bouts(self):
        from game_initialization.cases.boxing_opponent.adapter import BoxingOpponentAdapter
        a=BoxingOpponentAdapter()
        self.assertIn('opponent_target',a.observation_fields)
        for hidden in ('events','pattern','cooldown','next_counter'):
            self.assertNotIn(hidden,a.observation_fields)
        with self.assertRaises(ValueError):
            a.behavior_metrics({'replay':{'terminal':False}})
        self.assertEqual(a.behavior_metrics({'replay':{'terminal':True,'outcome':'draw','player_hp':50,'opponent_hp':50}})['win_rate'],0)

    def test_real_combat_state_machine(self):
        godot=os.environ.get('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot')
        if not godot:self.skipTest('Set GAMECRAFT_BENCH_GODOT_BIN for real-engine combat tests')
        case=Path(__file__).resolve().parents[1]/'game_initialization/cases/boxing_opponent'
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'project.godot').write_text('config_version=5\n')
            for name in ('combat.gd','rules.json'):shutil.copy2(case/name,root/name)
            r=subprocess.run([godot,'--headless','--path',str(root),'--script',str(Path(__file__).parent/'fixtures/boxing_opponent_checks.gd')],
                             capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            lines=[s for s in r.stdout.splitlines() if s.startswith('{')]
            self.assertTrue(lines,r.stdout+r.stderr)
            report=json.loads(lines[-1])
            self.assertTrue(report['passed'],report['failures'])
            self.assertEqual(len(report['policies']),27)
