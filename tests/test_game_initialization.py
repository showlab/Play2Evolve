import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from game_initialization.package import inspect_package, freeze, checkout, check_control_effects
from gamecraft_behavior.core import write_json
from tests.fixtures.counter_adapter import CounterAdapter


class PackageTests(unittest.TestCase):
    def test_control_check_requires_observed_effect_after_the_named_action(self):
        check = [{'action': 'drag', 'field': 'plants', 'op': 'increase'}]
        result = {'decisions': [
            {'action': 'drag', 'evaluation_state': {'plants': 1}},
            {'action': 'wait', 'evaluation_state': {'plants': 2}}],
            'stepped': {'plants': 2}}
        self.assertTrue(check_control_effects(result, check)[0]['passed'])
        result['decisions'][1]['evaluation_state']['plants'] = 1
        self.assertFalse(check_control_effects(result, check)[0]['passed'])

    def test_new_pointer_package_requires_an_observable_control_check(self):
        with tempfile.TemporaryDirectory() as td:
            d=self.draft(Path(td)/'draft')
            meta=json.loads((d/'initialization.json').read_text())
            meta['control_contract_version']=1
            write_json(d/'initialization.json',meta)
            scenarios=json.loads((d/'scenarios.json').read_text())
            pointer={'x':.2,'y':.3,'to_x':.8,'to_y':.7}
            scenarios[0]['actions'].append({'action':'drag','pointer':pointer})
            write_json(d/'scenarios.json',scenarios)
            adapter=CounterAdapter()
            adapter.pointer_actions=('drag',)
            with patch('game_initialization.package.load_adapter',return_value=adapter):
                with self.assertRaisesRegex(ValueError,'observable pointer control'):
                    inspect_package(d,sealed=False)
                scenarios[0]['control_checks']=[{'action':'drag','field':'points','op':'increase'}]
                write_json(d/'scenarios.json',scenarios)
                inspect_package(d,sealed=False)

    def draft(self, root):
        fixture = Path(__file__).parent/'fixtures'
        shutil.copytree(fixture/'counter_game', root/'game')
        (root/'adapter').mkdir()
        (root/'adapter/__init__.py').write_text(
            (fixture/'counter_adapter.py').read_text().replace("Path(__file__).with_suffix('.gd')", "Path(__file__).with_name('bridge.gd')") +
            '\ncreate_adapter = CounterAdapter\n')
        shutil.copy2(fixture/'counter_adapter.gd', root/'adapter/bridge.gd')
        for name, data in {
            'initialization.json': {'schema_version': 1, 'task_id': 'test-counter', 'version': 'v1', 'state': 'draft'},
            'asset_plan.json': {'task_id': 'test-counter', 'assets': []},
            'composition_spec.json': {'mutable': {}},
            'scenarios.json': [{'id': 'counter', 'purpose': 'initialization', 'actions': ['jab', 'block', 'jab']},
                               {'id': 'holdout', 'purpose': 'test', 'actions': ['wait']}]}.items():
            write_json(root/name, data)
        return root

    def test_draft_cannot_load_as_checked(self):
        with tempfile.TemporaryDirectory() as td:
            d=self.draft(Path(td)/'draft')
            self.assertEqual(inspect_package(d, sealed=False)[1].task_id, 'test-counter')
            with self.assertRaises(ValueError): checkout(d, Path(td)/'run')

    def test_seal_then_relocate_adapter_and_detect_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            with patch('gamecraft_behavior.runtime.rollout', return_value={'replay_verified': True, 'decisions': [1]}) as run:
                freeze(d,b/'frozen','godot')
                self.assertEqual(run.call_count,1)  # Never consume held-out scenarios.
            project,adapter,_=checkout(b/'frozen',b/'run')
            self.assertEqual(adapter.bridge_script, b/'run/initial/adapter/bridge.gd')
            (project/'main.gd').write_text('run edit')
            inspect_package(b/'frozen')
            (b/'frozen/game/main.gd').write_text('tampered')
            with self.assertRaises(ValueError): checkout(b/'frozen',b/'bad')
            self.assertFalse((b/'bad').exists())

    def test_mismatch_bad_action_symlink_and_nested_destination(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            with self.assertRaises(ValueError): freeze(d,d/'nested','godot')
            write_json(d/'scenarios.json',[{'id':'counter','purpose':'initialization','actions':['tap']}])
            with self.assertRaises(ValueError): inspect_package(d,sealed=False)
            (d/'outside').symlink_to(b/'secret')
            with self.assertRaises(ValueError): inspect_package(d,sealed=False)

    def test_failed_replay_cannot_be_frozen(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            with patch('gamecraft_behavior.runtime.rollout', return_value={'replay_verified':False,'decisions':[]}):
                with self.assertRaises(RuntimeError):freeze(d,b/'failed','godot')
            self.assertEqual(json.loads((b/'failed/initialization.json').read_text())['state'],'draft')
            with self.assertRaises(ValueError): checkout(b/'failed',b/'run')

    def test_freeze_rejects_an_input_that_does_not_change_its_declared_state(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            scenarios=json.loads((d/'scenarios.json').read_text())
            scenarios[0]['control_checks']=[{'action':'jab','field':'points','op':'increase'}]
            write_json(d/'scenarios.json',scenarios)
            result={'replay_verified':True,
                    'decisions':[{'action':'jab','evaluation_state':{'points':0}},
                                 {'action':'block','evaluation_state':{'points':0}}],
                    'stepped':{'points':0}}
            with patch('gamecraft_behavior.runtime.rollout',return_value=result):
                with self.assertRaisesRegex(RuntimeError,'control did not produce'):
                    freeze(d,b/'failed','godot')

    def test_default_launch_scenario_is_forwarded_to_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            scenarios=json.loads((d/'scenarios.json').read_text())
            scenarios[0]['launch']='default';write_json(d/'scenarios.json',scenarios)
            with patch('gamecraft_behavior.runtime.rollout',
                       return_value={'replay_verified':True,'decisions':[1]}) as run:
                freeze(d,b/'frozen','godot')
            self.assertIs(run.call_args.kwargs['normal_launch'],True)

    def test_bootstrap_smoke_replays_only_selected_initialization_scene(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);d=self.draft(b/'draft')
            scenarios=json.loads((d/'scenarios.json').read_text())
            scenarios.insert(1,{'id':'second','purpose':'initialization','actions':['wait']})
            write_json(d/'scenarios.json',scenarios)
            with patch('gamecraft_behavior.runtime.rollout',
                       return_value={'replay_verified':True,'decisions':[1]}) as run:
                frozen=freeze(d,b/'frozen','godot',scenario_ids=['counter'])
            self.assertEqual(run.call_count,1)
            report=json.loads((frozen/'checks/report.json').read_text())
            self.assertEqual(report['replay_scope'],'bootstrap_smoke')
            self.assertEqual(report['unchecked_initialization_scenarios'],['second'])


    def test_unknown_scenario_launch_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d=self.draft(Path(td)/'draft')
            scenarios=json.loads((d/'scenarios.json').read_text())
            scenarios[0]['launch']='menu';write_json(d/'scenarios.json',scenarios)
            with self.assertRaisesRegex(ValueError,'Scenario launch'):
                inspect_package(d,sealed=False)
