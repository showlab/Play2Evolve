"""Sprite assets from H3 (run on the B300 node, H3 served with VARIANT=fl2va):  [SPEC=race_spec] python gen_assets.py [action ...]

1. GPT Image -> idle reference on pure green, refitted per REF_FIT:  <ASSETS>/<char>_idle.png
2. H3 FL2VA, first = last frame = idle ref, N seeds:      <ASSETS>/<char>/<action>/cand{k}/raw.mp4 (+ prompt.txt; regenerated
   only when missing or the prompt changed); actions in the spec's ONE_WAY (down, crash) never return to idle: first keyframe only
3. green-screen chroma key, resampled to the spec frames, re-centred so the subject never travels: cand{k}/frame%04d.png (RGBA)
4. Judge JUDGE_FRAMES keyed frames (on a checkerboard) + ref -> action_correct, identity, clean_matte;
   loops_to_idle and in_frame are measured from the alpha masks by code, never by the judge.
   best passing cand -> <ASSETS>/<char>/manifest.json, all verdicts -> <ASSETS>/judge_log.json, overview -> <action>/sheet.png
--stage: N stage backgrounds (default a frame of an H3 t2va clip, STAGE_BACKEND=api for gpt-image), judged usable/look -> spec.STAGE.
With LLM_BACKEND=local (serve_local.sh) the judge is free too; only step 1 (and STAGE_BACKEND=api) needs OPENAI_API_KEY.
"""
import base64
import importlib
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request

import cv2
import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw

import llm

spec = importlib.import_module(os.environ.get("SPEC", "game_spec"))  # game_spec (fighter) or race_spec
ACTIONS, CHARACTERS, ONE_WAY, ASSETS, REF_FIT = spec.ACTIONS, spec.CHARACTERS, spec.ONE_WAY, spec.ASSETS, spec.REF_FIT

N = int(os.environ.get("SEEDS", 4))  # candidates per action (seed = SEED0 + k)
SEED0 = 0  # first seed of this generation: a regeneration request (world_opt) raises it by N so H3 is asked again instead of the cached candidates
CHARS = [c for c in os.environ.get("CHARS", ",".join(CHARACTERS)).split(",") if c]  # characters to (re)generate; judging is never cached
PASS = 0.7  # min action_correct to be selectable
JUDGED = ["action_correct", "identity", "clean_matte"]  # scored by the judge (a "look" taste score was dropped 2026-09-30: no ground truth, and 235B gave everything 0.95-1.0)
MEASURED = ["loops_to_idle", "in_frame"]  # scored by code from the alpha masks
SCORES = JUDGED + MEASURED
JUDGE_FRAMES = 8
H3_URL = os.environ.get("H3_URL", "http://127.0.0.1:30010")
H3_TIMEOUT = int(os.environ.get("H3_TIMEOUT", 600))  # s per clip: a clip took ~15 s when the server had the GPUs; a job that never runs must not hang the caller
SECONDS = 4  # H3 minimum; the clip is resampled to the spec frame count
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-5.6-sol")  # via llm.call: LLM_BACKEND=local judges with the local Qwen server
NOTES = "prompt_notes.json"  # <ASSETS>/<char>/prompt_notes.json {action: constraint}: the asset-side edit, appended to the action prompt
IMAGE_MODEL = "gpt-image-2"  # the character reference (REF_BACKEND=api) and STAGE_BACKEND=api
REF_BACKEND = os.environ.get("REF_BACKEND", "h3")  # h3: the middle frame of a locked-off t2va clip on green (free); api: gpt-image
REF_SEED = int(os.environ.get("REF_SEED", 0))  # another seed = another candidate reference
REF_T2VA = """integrated_multimodal_description: [Shot 1] Locked-off static shot, square frame. {ref} The character holds the stance and does not move for the whole shot.

overall_soundscape: Quiet room tone.

non_diegetic_music: N/A
"""
STAGE_BACKEND = os.environ.get("STAGE_BACKEND", "h3")  # h3: one frame of a locked-off t2va clip from the same server (free); api: gpt-image
STAGE_T2VA = """integrated_multimodal_description: [Shot 1] Locked-off static wide shot. {stage} Nothing in the scene moves for the whole shot.

overall_soundscape: Quiet room tone.

non_diegetic_music: N/A"""  # H3's own prompt layout (minimax_h3/prompts_example.jsonl) around STAGE_PROMPT
LOG = f"{ASSETS}/judge_log.json"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local server: bypass http_proxy

H3_PROMPT = """How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video{last_align}.

integrated_multimodal_description: [Shot 1] {scene}. The subject, {desc}, begins in the pose and framing established by Picture 1, {action}{ending}. The whole subject stays inside the frame. The flat green background stays empty, with no shadows, floor or props.

overall_soundscape: Quiet room tone.

non_diegetic_music: N/A"""
LAST_ALIGN = "; Picture 2 (from Shot 1) aligns with the {sec:.2f}-second mark of the target video"  # only with a last keyframe
ENDING = ", and settles back into the same pose, position and framing established by Picture 2 at the end of the shot"
JUDGE_SYS = f"""You check one animation clip generated as a 2D game sprite. You get {JUDGE_FRAMES} frames in time order (first to last), already cut out of a green screen and shown on a grey checkerboard, then the subject's idle reference image (the last image, on green). You also get the intended action.
The game engine moves the subject across the screen itself, so the subject must not travel in the frame; only the pose has to change.{" " + spec.JUDGE_NOTE if spec.JUDGE_NOTE else ""}
Scores in [0, 1]:
- action_correct: the frames clearly show the intended action, with its key pose visible in at least one frame.
- identity: the same subject as the reference ({spec.IDENTITY}), no morphing.
- clean_matte: the subject is cleanly cut out: no green fringe or leftover background, no missing parts, no checkerboard showing through the body.
action_ok: true only if the intended action is really performed and in the intended direction (a walk backward shown as a walk forward is false).
failure: one short English sentence naming the most important problem, or "" if none."""
VERDICT = {"type": "object", "additionalProperties": False, "required": JUDGED + ["action_ok", "failure"],
           "properties": {**{s: {"type": "number"} for s in JUDGED}, "action_ok": {"type": "boolean"}, "failure": {"type": "string"}}}


def fit_ref(png):  # crop the keyed character and place it per REF_FIT on a fresh green square (same size)
    rgba = key(imageio.imread(png)[..., :3])
    side = rgba.shape[0]
    ys, xs = np.where(rgba[..., 3] > 127)
    crop = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h = int(REF_FIT[1] * side)
    w = int(crop.shape[1] * h / crop.shape[0])
    sprite = Image.fromarray(crop).resize((w, h), Image.LANCZOS)
    out = Image.new("RGBA", (side, side), (0, 255, 0, 255))
    out.paste(sprite, (int(REF_FIT[0] * side - w / 2), (side - h) // 2), sprite)
    out.convert("RGB").save(png)


def make_ref(c):  # the character's idle reference on green: one image from the API, or one frame of an H3 t2va clip (REF_BACKEND=h3)
    os.makedirs(os.path.dirname(c["ref"]), exist_ok=True)
    prompt = spec.REF_PROMPT.format(desc=c["desc"])
    if REF_BACKEND == "h3":
        mp4 = c["ref"].rsplit(".", 1)[0] + f"_s{REF_SEED}.mp4"
        h3_t2va(REF_T2VA.format(ref=prompt), REF_SEED, mp4, aspect="1:1")
        Image.fromarray(frames_at(mp4, 3)[1]).save(c["ref"])
    else:
        r = llm.api_client().images.generate(model=IMAGE_MODEL, prompt=prompt, size="1024x1024")
        open(c["ref"], "wb").write(base64.b64decode(r.data[0].b64_json))
    fit_ref(c["ref"])


def http(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(H3_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:2000]}")


def notes(char):  # the constraints the world judge asked for, per action
    path = f"{ASSETS}/{char}/{NOTES}"
    return json.load(open(path)) if os.path.exists(path) else {}


def h3_prompt(c, action_prompt, to_idle):  # to_idle=False: first keyframe only, the clip may end anywhere
    return H3_PROMPT.format(last_align=LAST_ALIGN.format(sec=SECONDS) if to_idle else "", scene=spec.SCENE, desc=c["desc"],
                            action=action_prompt, ending=ENDING if to_idle else "")


def h3_fl2va(c, prompt, seed, out_mp4, to_idle=True):
    ref = os.path.abspath(c["ref"])
    h3_video({"task": "fl2va", "prompt": prompt, "seed": seed,
              "conditions": [{"type": "image", "uri": ref, "role": "keyframe", "frame_index": 0}]
                            + ([{"type": "image", "uri": ref, "role": "keyframe", "frame_index": -1}] if to_idle else []),
              "seconds": SECONDS, "target": {"short_edge": 768, "aspect_ratio": "1:1", "duration_seconds": SECONDS}}, out_mp4)


def h3_t2va(prompt, seed, out_mp4, aspect="16:9", seconds=SECONDS):  # text only: static plates use one sampled frame
    h3_video({"task": "t2va", "prompt": prompt, "seed": seed, "conditions": [], "seconds": seconds,
              "target": {"short_edge": 768, "aspect_ratio": aspect, "duration_seconds": seconds}}, out_mp4)


def h3_video(body, out_mp4):  # submit one job to the H3 server and wait for its mp4
    backend = os.environ.get('H3_BACKEND', 'local')
    if backend == 'wan':
        from wan_video_backend import generate
        return generate(body, out_mp4, timeout=H3_TIMEOUT)
    if backend == 'minimax':
        from h3_minimax_api import generate
        return generate(body, out_mp4, timeout=H3_TIMEOUT)
    if backend != 'local':
        raise ValueError(f'Unknown H3_BACKEND: {backend}')
    vid = json.loads(http("POST", "/v1/videos", body))["id"]
    t0 = time.time()
    while True:
        time.sleep(5)
        job = json.loads(http("GET", f"/v1/videos/{vid}"))
        if time.time() - t0 > H3_TIMEOUT:
            raise RuntimeError(f"H3 job {vid} still {job.get('status')} after {H3_TIMEOUT}s: is the H3 server holding the GPUs?")
        if job.get("status") == "failed":
            raise RuntimeError(f"H3 job {vid} failed: {json.dumps(job)[:2000]}")
        if job.get("status") == "completed":
            break
    open(out_mp4, "wb").write(http("GET", f"/v1/videos/{vid}/content", timeout=600))


def frames_at(mp4, n):  # n frames evenly spaced over the whole clip, first and last included
    reader = imageio.get_reader(mp4)
    last = reader.count_frames() - 1
    want = {}
    for i in range(n):
        want.setdefault(round(i * last / (n - 1)), []).append(i)
    out = [None] * n
    for j, frame in enumerate(reader):  # stream: only n frames are kept
        for i in want.get(j, []):
            out[i] = frame
    return [f if f is not None else frame for f in out]  # count_frames can overshoot: fill with the last decoded frame


def key(rgb):  # green-screen chroma key -> RGBA uint8
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    green = (hsv[..., 0] > 35) & (hsv[..., 0] < 85) & (hsv[..., 1] > 80) & (hsv[..., 2] > 60)  # OpenCV hue 0-180, green = 60
    alpha = 255 - cv2.GaussianBlur(green.astype(np.uint8) * 255, (5, 5), 0)  # soft edge
    out = rgb.copy()
    out[..., 1] = np.minimum(out[..., 1], np.maximum(out[..., 0], out[..., 2]))  # despill: green never above max(r, b)
    return np.dstack([out, alpha])


def mask_iou(a, b):  # silhouette overlap of two RGBA frames (resized to the first)
    m1 = a[..., 3] > 127
    m2 = cv2.resize(b[..., 3], (a.shape[1], a.shape[0])) > 127
    return float((m1 & m2).sum() / max(1, (m1 | m2).sum()))


def anchor_x(rgba):  # x of the subject's footprint: the centroid of the bottom fifth of the mask (wheels / feet), or None if empty
    ys, xs = np.where(rgba[..., 3] > 127)
    if len(ys) == 0:
        return None
    return float(xs[ys >= ys.max() - 0.2 * (ys.max() - ys.min())].mean())


def recentre(rgba):  # the engine owns position, so a sprite must not travel: shift every frame back onto the first frame's footprint
    x0 = anchor_x(rgba[0])
    out = [rgba[0]]
    for f in rgba[1:]:
        x = anchor_x(f)
        out.append(f if x is None or x0 is None else
                   cv2.warpAffine(f, np.float32([[1, 0, x0 - x], [0, 1, 0]]), (f.shape[1], f.shape[0])))
    return out


def width_ratio(rgba):  # widest / narrowest silhouette: a car that yaws out of the rear view gets much wider. Measured, not yet gated.
    w = [max(1, int((f[..., 3] > 127).any(0).sum())) for f in rgba]
    return round(max(w) / min(w), 3)


def measure(rgba, ref_rgba, to_idle):  # code-scored criteria from the alpha masks
    ends = [rgba[0], rgba[-1]] if to_idle else [rgba[0]]  # a non-returning action only has to start from idle
    loops = min(mask_iou(f, ref_rgba) for f in ends)
    touching = [((f[:2, :, 3] > 127).any() or (f[-2:, :, 3] > 127).any() or (f[:, :2, 3] > 127).any() or (f[:, -2:, 3] > 127).any()) for f in rgba]
    return {"loops_to_idle": round(loops, 3), "in_frame": round(1 - sum(touching) / len(rgba), 3), "width_ratio": width_ratio(rgba)}


def on_checker(rgba):
    yy, xx = np.mgrid[0:rgba.shape[0], 0:rgba.shape[1]]
    bg = np.where(((yy // 32 + xx // 32) % 2)[..., None] == 0, 200.0, 150.0)
    a = rgba[..., 3:] / 255.0
    return (rgba[..., :3] * a + bg * (1 - a)).astype(np.uint8)


def pick(verdicts):  # correctness first (a candidate that fails action_correct / action_ok is never chosen), then the total score
    return max((v for v in verdicts if v["action_correct"] >= PASS and v.get("action_ok", True)), key=lambda v: v["score"], default=None)


def judge(frame_pngs, ref_png, action, action_prompt):
    text = f"INTENDED ACTION: {action} — {action_prompt}\n\nImages: {len(frame_pngs)} cut-out frames in time order, then the idle reference."
    return llm.call(JUDGE_MODEL, JUDGE_SYS, [{"type": "input_text", "text": text}] + [llm.img(p) for p in frame_pngs + [ref_png]],
                    "asset_verdict", VERDICT, backend=llm.OPTIMIZER_BACKEND)


def sheet(frame_rows, verdicts, out_png, tile=160):  # one row per candidate: the judged frames + scores
    im = Image.new("RGB", (tile * (JUDGE_FRAMES + 2), tile * len(frame_rows)), "white")
    d = ImageDraw.Draw(im)
    for r, (pngs, v) in enumerate(zip(frame_rows, verdicts)):
        for c, p in enumerate(pngs):
            im.paste(Image.open(p).resize((tile, tile)), (c * tile, r * tile))
        scores = "\n".join(f"{s}: {v[s]:.2f}" for s in SCORES + ["width_ratio"])
        d.multiline_text((JUDGE_FRAMES * tile + 8, r * tile + 4), f"cand{v['cand']}  total {v['score']:.2f}\n{scores}\n{textwrap.fill(v['failure'], 44)}", fill="black")
    im.save(out_png)


def gen_action(char, c, action):
    action_prompt, n_frames = ACTIONS[action][0], ACTIONS[action][1]
    if notes(char).get(action):
        action_prompt += "; " + notes(char)[action]  # a changed prompt regenerates the candidates
    to_idle = action not in ONE_WAY
    prompt = h3_prompt(c, action_prompt, to_idle)
    ref_rgba = key(imageio.imread(c["ref"])[..., :3])
    verdicts, rows = [], []
    for k in range(N):
        d = f"{ASSETS}/{char}/{action}/cand{k}"
        os.makedirs(d, exist_ok=True)
        old = open(f"{d}/prompt.txt").read() if os.path.exists(f"{d}/prompt.txt") else None
        seed, old_seed = SEED0 + k, int(open(f"{d}/seed.txt").read()) if os.path.exists(f"{d}/seed.txt") else k
        if not os.path.exists(f"{d}/raw.mp4") or old != prompt or old_seed != seed:
            t0 = time.time()
            h3_fl2va(c, prompt, seed, f"{d}/raw.mp4", to_idle)
            open(f"{d}/prompt.txt", "w").write(prompt)
            open(f"{d}/seed.txt", "w").write(str(seed))
            print(f"[{char}/{action} cand{k}] H3 {time.time() - t0:.0f}s", flush=True)
        rgba = [key(f) for f in frames_at(f"{d}/raw.mp4", n_frames)]
        if to_idle:  # ONE_WAY actions (crash, down) end somewhere else by design, so their footprint is allowed to move
            rgba = recentre(rgba)
        for i, f in enumerate(rgba):
            imageio.imwrite(f"{d}/frame{i:04d}.png", f)
        shots = [f"{d}/judge_f{i}.png" for i in range(JUDGE_FRAMES)]
        for i, p in enumerate(shots):
            imageio.imwrite(p, on_checker(rgba[round(i * (n_frames - 1) / (JUDGE_FRAMES - 1))]))
        v = judge(shots, c["ref"], action, action_prompt)
        v.update(measure(rgba, ref_rgba, to_idle))
        v.update(cand=k, seed=seed, score=round(sum(v[s] for s in SCORES), 3))
        print(f"[{char}/{action} cand{k}] {json.dumps(v, ensure_ascii=False)}", flush=True)
        verdicts.append(v)
        rows.append(shots)
    sheet(rows, verdicts, f"{ASSETS}/{char}/{action}/sheet.png")
    best = pick(verdicts)
    entry = {"cand": best["cand"], "dir": f"{ASSETS}/{char}/{action}/cand{best['cand']}", "frames": n_frames, "score": best["score"], "look": best.get("look")} if best else "missing"
    return entry, verdicts


def rejudge(char, c, action):  # re-run the judge on the existing candidates (no H3), and re-pick: for a changed judge or rubric
    verdicts = []
    for k in range(N):
        d = f"{ASSETS}/{char}/{action}/cand{k}"
        shots = [f"{d}/judge_f{i}.png" for i in range(JUDGE_FRAMES)]
        if not all(os.path.exists(p) for p in shots):
            continue
        old = next((v for v in json.load(open(LOG)).get(f"{char}/{action}", []) if v["cand"] == k), {})
        v = judge(shots, c["ref"], action, ACTIONS[action][0])
        v.update({m: old.get(m, 1.0) for m in MEASURED}, cand=k, seed=k)
        v["score"] = round(sum(v[s] for s in SCORES), 3)
        verdicts.append(v)
    best = pick(verdicts)
    entry = {"cand": best["cand"], "dir": f"{ASSETS}/{char}/{action}/cand{best['cand']}", "frames": ACTIONS[action][1], "score": best["score"], "look": best.get("look")} if best else "missing"
    return entry, verdicts


STAGE_SYS = """You judge candidate background images for a 2D side-view game stage. The characters are drawn on top of it later, standing on a
ground line near the bottom of the frame. Scores in [0, 1]:
- usable: no people, creatures or text; a clear, level ground surface across the whole width in the lower part; nothing in the scene competes with a character in the foreground.
- look: how good it is as game art: coherent style, clean shapes, pleasant colours, depth without clutter.
failure: one short English sentence, or ""."""
STAGE_VERDICT = {"type": "object", "additionalProperties": False, "required": ["usable", "look", "failure"],
                 "properties": {"usable": {"type": "number"}, "look": {"type": "number"}, "failure": {"type": "string"}}}


def make_stage():  # N background candidates from the image model, judged for usability and look; the best becomes spec.STAGE
    verdicts = []
    for k in range(N):
        png, txt = f"{ASSETS}/stage_cand{k}.png", f"{ASSETS}/stage_cand{k}.txt"  # txt = the prompt: a changed prompt regenerates
        prompt = STAGE_T2VA.format(stage=spec.STAGE_PROMPT) if STAGE_BACKEND == "h3" else spec.STAGE_PROMPT
        if not (os.path.exists(png) and os.path.exists(txt) and open(txt).read() == prompt):
            if STAGE_BACKEND == "h3":
                h3_t2va(prompt, SEED0 + k, f"{ASSETS}/stage_cand{k}.mp4")
                Image.fromarray(frames_at(f"{ASSETS}/stage_cand{k}.mp4", 3)[1]).save(png)
            else:
                r = llm.api_client().images.generate(model=IMAGE_MODEL, prompt=prompt, size="1536x1024")
                open(png, "wb").write(base64.b64decode(r.data[0].b64_json))
            open(txt, "w").write(prompt)
        v = llm.call(JUDGE_MODEL, STAGE_SYS, [{"type": "input_text", "text": f"Candidate {k}."}, llm.img(png)], "stage_verdict", STAGE_VERDICT, backend=llm.OPTIMIZER_BACKEND)
        v.update(cand=k, png=png)
        print(f"[stage cand{k}] {json.dumps(v, ensure_ascii=False)}", flush=True)
        verdicts.append(v)
    best = max((v for v in verdicts if v["usable"] >= PASS), key=lambda v: v["look"], default=None)
    if best:
        Image.open(best["png"]).convert("RGB").save(spec.STAGE)
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    log["stage"] = verdicts
    json.dump(log, open(LOG, "w"), indent=2, ensure_ascii=False)
    print(f"[stage] -> {spec.STAGE if best else 'none usable'}", flush=True)
    return best, verdicts


def main(actions, judge_only=False):
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    for char, c in ((k, v) for k, v in CHARACTERS.items() if k in CHARS):
        if not os.path.exists(c["ref"]):
            make_ref(c)
            print(f"wrote {c['ref']}", flush=True)
        path = f"{ASSETS}/{char}/manifest.json"
        manifest = json.load(open(path)) if os.path.exists(path) else {}
        for action in actions:
            if action not in c.get("actions", ACTIONS):  # a spec may give a character its own shorter action list
                continue
            manifest[action], log[f"{char}/{action}"] = (rejudge if judge_only else gen_action)(char, c, action)
            json.dump(manifest, open(path, "w"), indent=2)  # saved after every action: partial runs keep their results
            json.dump(log, open(LOG, "w"), indent=2, ensure_ascii=False)
            print(f"[{char}/{action}] -> {manifest[action]}", flush=True)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    args = sys.argv[1:]
    judge_only = "--rejudge" in args  # python gen_assets.py --rejudge walk_b: re-score the existing candidates without H3
    if "--stage" in args:  # python gen_assets.py --stage: N background candidates (H3 t2va, or gpt-image with STAGE_BACKEND=api), the best judged one becomes the stage
        make_stage()
    else:
        main([a for a in args if a != "--rejudge"] or list(ACTIONS), judge_only)
