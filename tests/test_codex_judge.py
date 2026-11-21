"""The Codex benchmark judge keeps scoring and explanations separate."""
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from play2evolve.runtime import configure_benchmark

configure_benchmark()

from gamecraft_bench.verifier.judges.base import JudgeError, JudgeRequest, RequirementSpec
from gamecraft_evaluation.codex_judge import CodexJudge


class CodexJudgeTests(unittest.TestCase):
    def setUp(self):
        self.request = JudgeRequest(
            demo_id="fight", video_path=Path("fight.mp4"),
            frame_paths=[Path("frame0.png"), Path("frame1.png")],
            requirements=[RequirementSpec("M1", "Show a defensive action"),
                          RequirementSpec("M2", "Show different punches")],
        )

    def test_scores_are_frozen_before_separate_rationale_call(self):
        scores = {"M1": 0.5, "M2": 0.0}
        reasons = {"M1": "One dodge is visible in frame 1.",
                   "M2": "The frames show no distinct punch types."}
        with patch("gamecraft_evaluation.codex_judge.img", return_value={"type": "input_image"}), \
             patch("gamecraft_evaluation.codex_judge.call", side_effect=[scores, reasons]) as model:
            result = CodexJudge().score(self.request)
        self.assertEqual(result.scores, scores)
        self.assertEqual(result.rationales, reasons)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args_list[0].args[3], "gamecraft_bench_scores")
        self.assertEqual(model.call_args_list[1].args[3], "gamecraft_bench_rationales")
        self.assertIn("fixed score 0.5", model.call_args_list[1].args[2][0]["text"])

    def test_missing_rationale_is_not_silently_logged_as_a_valid_score(self):
        with patch("gamecraft_evaluation.codex_judge.img", return_value={"type": "input_image"}), \
             patch("gamecraft_evaluation.codex_judge.call", side_effect=[
                 {"M1": 0.5, "M2": 0.0}, {"M1": "One dodge visible", "M2": ""}]):
            with self.assertRaisesRegex(JudgeError, "omitted or emptied"):
                CodexJudge().score(self.request)

    def test_json_string_envelopes_are_supported(self):
        with patch("gamecraft_evaluation.codex_judge.img", return_value={"type": "input_image"}), \
             patch("gamecraft_evaluation.codex_judge.call", side_effect=[
                 '{"M1": 1, "M2": 0}',
                 '{"M1": "The dodge appears in frame 1.", "M2": "No punches are visible."}']):
            result = CodexJudge().score(self.request)
        self.assertEqual(result.scores["M1"], 1)
        self.assertIn("No punches", result.rationales["M2"])

    def test_bad_request_retries_same_frames_as_jpeg_only_for_that_call(self):
        scores = {'M1': 1.0, 'M2': 0.0}
        reasons = {'M1': 'Visible.', 'M2': 'Absent.'}
        with patch('gamecraft_evaluation.codex_judge.img',
                   side_effect=lambda frame, **kw: {'type': 'input_image',
                                                     'frame': str(frame), 'quality': kw.get('jpeg_quality')}) as image, \
             patch('gamecraft_evaluation.codex_judge.call',
                   side_effect=[RuntimeError('Bad Request'), scores, reasons]) as model:
            result = CodexJudge().score(self.request)
        self.assertEqual(model.call_count, 3)
        self.assertEqual(image.call_count, 6)
        self.assertEqual([x['quality'] for x in model.call_args_list[0].args[2]
                          if x['type'] == 'input_image'], [None, None])
        self.assertEqual([x['quality'] for x in model.call_args_list[1].args[2]
                          if x['type'] == 'input_image'], [85, 85])
        self.assertEqual(json.loads(result.raw)['image_encoding'],
                         {'score': 'jpeg_fallback', 'rationale': 'png'})

    def test_other_errors_do_not_change_image_encoding(self):
        with patch('gamecraft_evaluation.codex_judge.img', return_value={'type': 'input_image'}), \
             patch('gamecraft_evaluation.codex_judge.call',
                   side_effect=RuntimeError('429 capacity')) as model:
            with self.assertRaisesRegex(JudgeError, '429 capacity'):
                CodexJudge().score(self.request)
        self.assertEqual(model.call_count, 1)


if __name__ == "__main__":
    unittest.main()
