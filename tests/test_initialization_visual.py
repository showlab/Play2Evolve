import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from game_initialization.agentic import visual as v
from game_initialization.batch.__main__ import write, sha
from game_initialization.package import inventory

class VisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'draft').mkdir();(self.root/'generation').mkdir()
        (self.root/'checks/1/package').mkdir(parents=True)
        (self.root/'sheet.png').write_bytes(b'frame')
        write(self.root/'generation/asset_plan.json',{'assets':[{'id':'actor'}]})
        write(self.root/'latest_check.json',{'passed':True,'package':'checks/1/package'})
        self.runtime={'package':'checks/1/package'}
        self.evidence=[{'image':'sheet.png','video':'video.mp4','result':'result.json'}]
        self.report={'passed':True,'observations':[{'evidence':'sheet.png','observation':'one actor visible'}],'issues':[],'limitations':[]}
    def test_rejection_preserves_actionable_report(self):
        self.report['passed']=False
        self.report['issues']=[{'blocking':True,'problem':'extra actor','evidence':['sheet.png'],
                              'asset_ids':['actor'],'suggested_repair':'regenerate isolated actor'}]
        with patch.object(v,'collect_evidence',return_value=self.evidence),patch.object(v,'codex',return_value=self.report) as call:
            with self.assertRaisesRegex(ValueError,'rejected'):v.review_visual(self.root,{},'visual-0',self.runtime)
        self.assertTrue((self.root/'latest_visual_review.json').exists())
        self.assertEqual(call.call_args.kwargs['images'],[self.root/'sheet.png'])
    def test_every_scene_must_be_observed(self):
        self.report['observations']=[]
        with self.assertRaisesRegex(ValueError,'every'):v.validate_review(self.root,self.report,self.evidence)
    def test_pass_cannot_hide_blocking_issue(self):
        self.report['issues']=[{'blocking':True,'problem':'green actor','evidence':['sheet.png'],
                             'asset_ids':['actor'],'suggested_repair':'fix keying'}]
        with self.assertRaisesRegex(ValueError,'rejected'):v.validate_review(self.root,self.report,self.evidence)
    def test_modified_image_invalidates_acceptance(self):
        with patch.object(v,'collect_evidence',return_value=self.evidence),patch.object(v,'codex',return_value=self.report):
            v.review_visual(self.root,{},'visual-0',self.runtime)
        v.require_current_visual_review(self.root,self.runtime)
        (self.root/'sheet.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'current'):v.require_current_visual_review(self.root,self.runtime)
    def test_reviewer_cannot_change_game(self):
        def mutate(*args,**kwargs):
            (self.root/'draft/game.gd').write_text('changed');return self.report
        with patch.object(v,'collect_evidence',return_value=self.evidence),patch.object(v,'codex',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'changed'):v.review_visual(self.root,{},'visual-0',self.runtime)

if __name__=='__main__':unittest.main()
