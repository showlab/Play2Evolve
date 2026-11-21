import unittest

from gamecraft_training.package.scenario_groups import build


class ScenarioGroupTests(unittest.TestCase):
    def setUp(self):
        self.scenarios=[{'id':'menu','actions':['wait']},
                        {'id':'training','actions':['train']},
                        {'id':'fight','actions':['jab']}]

    def test_grouping_preserves_original_scripts_and_covers_all_scenes(self):
        groups=build(self.scenarios,[{'id':'intro','scenarios':['menu','training']},
                                     {'id':'match','scenarios':['fight']}])
        self.assertEqual(groups[0]['scenario_ids'],['menu','training'])
        self.assertEqual(groups[0]['members'],self.scenarios[:2])
        self.assertEqual(groups[1]['members'],self.scenarios[2:])

    def test_grouping_rejects_missing_or_duplicate_scenes(self):
        with self.assertRaisesRegex(ValueError,'cover every'):
            build(self.scenarios,[{'id':'intro','scenarios':['menu','training']}])
        with self.assertRaisesRegex(ValueError,'once'):
            build(self.scenarios,[{'id':'intro','scenarios':['menu','training']},
                                  {'id':'match','scenarios':['training','fight']}])


if __name__=='__main__':
    unittest.main()
