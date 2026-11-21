"""GameCraft-Bench multimodal judge backed by the authenticated Codex CLI."""
from __future__ import annotations

import json
import math
import os

os.environ["LLM_BACKEND"] = "codex"
os.environ["OPTIMIZER_BACKEND"] = "codex"

from gamecraft_bench.verifier.judges import _common
from gamecraft_bench.verifier.judges.base import JudgeError, JudgeRequest, JudgeResponse, MultimodalJudge
from gamecraft_bench.verifier.judges.openai_gpt import _select_frames
from llm import call, img


class CodexJudge(MultimodalJudge):
    """Use codex exec's existing login to judge the benchmark's sampled frames."""

    name = "codex"
    default_model = "gpt-6-sol"

    def _call_with_image_fallback(self, instructions, content, frames, name, schema):
        """Use the original PNG request; retry only a Bad Request with JPEG frames."""
        try:
            return call(self.model, instructions, content, name, schema,
                        effort="high", backend="codex", max_tokens=2048), "png"
        except Exception as exc:
            if "Bad Request" not in str(exc):
                raise
            print(f"[codex] {name}: PNG request rejected; retrying the same frames as JPEG", flush=True)
            fallback = list(content)
            fallback[1:1 + len(frames)] = [img(frame, width=960, jpeg_quality=85)
                                         for frame in frames]
            return call(self.model, instructions, fallback, name, schema,
                        effort="high", backend="codex", max_tokens=2048), "jpeg_fallback"

    @staticmethod
    def _object(result: object, label: str) -> dict:
        # codex exec may wrap a schema-constrained object as a JSON string.
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise JudgeError(f"Codex {label} returned a non-JSON string: {result[:300]!r}") from exc
        if not isinstance(result, dict):
            raise JudgeError(f"Codex {label} returned {type(result).__name__}, expected a JSON object")
        return result

    def explain(self, request: JudgeRequest, scores: dict[str, float]) -> dict[str, str]:
        """Explain fixed scores in a separate call so scoring stays comparable."""
        ids = [r.id for r in request.requirements]
        if set(scores) != set(ids):
            raise JudgeError("Cannot explain incomplete rubric scores")
        frames = _select_frames(request.frame_paths)
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {rid: {"type": "string"} for rid in ids},
            "required": ids,
        }
        content = [{"type": "input_text", "text": (
            f"Demo: {request.demo_id}. These {len(frames)} sampled frames are in temporal order. "
            "The scores below were already assigned and MUST NOT be changed. "
            "For each requirement, explain in one concise sentence what visible evidence "
            "supports that score, or what evidence is missing. Refer to frame positions "
            "when useful; do not invent actions or states that the frames do not show. "
            "Return a flat JSON object keyed by requirement ID, with string values only.\n"
            + "\n".join(f"- {r.id} (fixed score {scores[r.id]}): {r.description}"
                        for r in request.requirements)
        )}]
        content.extend(img(frame, width=960) for frame in frames)
        try:
            result, image_format = self._call_with_image_fallback(
                "You explain fixed benchmark scores using only the supplied frames. "
                "Do not rescore. If an event is not visible, say so. Reply with strict JSON only.",
                content, frames, "gamecraft_bench_rationales", schema)
        except Exception as exc:
            raise JudgeError(f"Codex rationale call failed: {type(exc).__name__}: {exc}") from exc
        rationales = self._object(result, "rationale judge")
        if set(rationales) != set(ids) or any(
            not isinstance(value, str) or not value.strip() for value in rationales.values()
        ):
            raise JudgeError("Codex rationale judge omitted or emptied rubric explanations")
        return {rid: rationales[rid].strip() for rid in ids}, image_format

    def score(self, request: JudgeRequest) -> JudgeResponse:
        if not request.frame_paths:
            raise JudgeError(f"no sampled frames available for demo {request.demo_id!r}")

        requirements = request.requirements
        ids = [r.id for r in requirements]
        # Keep the structured response flat. The nested scores+rationales
        # schema used by the API backend currently makes Codex CLI emit only
        # the top-level key for this 13-item rubric.
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {rid: {"type": "number", "minimum": 0, "maximum": 1} for rid in ids},
            "required": ids,
        }
        frames = _select_frames(request.frame_paths)
        content = [{"type": "input_text", "text": (
            f"Demo: {request.demo_id}. The following {len(frames)} images are frames sampled "
            "in temporal order from one complete playthrough. Judge only visible evidence."
        )}]
        content.extend(img(frame, width=960) for frame in frames)
        rubric = "\n".join(f"- {r.id}: {r.description}" for r in requirements)
        content.append({"type": "input_text", "text": (
            "Score each requirement based only on visible evidence in the recording. "
            "Use 0.0 for not demonstrated, 0.5 for partial or ambiguous, and 1.0 for clear. "
            "Return one JSON object with each requirement ID as a top-level key and its score "
            "as a number from 0 to 1. No prose.\n" + rubric
        )})
        try:
            result, score_image_format = self._call_with_image_fallback(
                _common.SYSTEM_INSTRUCTION, content, frames, "gamecraft_bench_scores", schema)
        except Exception as exc:
            raise JudgeError(f"Codex CLI judge call failed: {type(exc).__name__}: {exc}") from exc

        scores = self._object(result, "score judge")
        if set(scores) != set(ids):
            raise JudgeError("Codex judge omitted or added rubric score IDs")
        for value in scores.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise JudgeError("Codex judge returned a score outside [0, 1]")
        rationales, rationale_image_format = self.explain(request, scores)
        raw = json.dumps({"scores": scores, "rationales": rationales,
                          "image_encoding": {"score": score_image_format,
                                             "rationale": rationale_image_format}}, ensure_ascii=False)
        return JudgeResponse(scores=scores, rationales=rationales, raw=raw)
