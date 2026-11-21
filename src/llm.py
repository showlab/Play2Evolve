"""The OpenAI client shared by gen_assets.py, play.py and skillopt_lite.py, plus the image-input and JSON-schema call helpers.

LLM_BACKEND picks where call() goes (every process reads it, spawned workers included):
  api   (default) OpenAI Responses API with OPENAI_API_KEY, billed per token
  codex `codex exec`, one subprocess per call, defaulting to the logged-in Codex
        account. An explicit GAMECRAFT_CODEX_* provider override is supported;
        unrelated API credentials are removed from the child environment
  local an SGLang server started by serve_local.sh (LOCAL_URL, default http://127.0.0.1:30020/v1), free and unmetered;
        chat completions + json_schema; the served name is "local", so the model argument of call() is ignored
OPTIMIZER_BACKEND (default: LLM_BACKEND) is the backend for calls made with backend="optimizer", so the Player can run on the free
local model while the optimizer stays on Sol, the split SkillOpt uses (target vs optimizer backend).
gen_assets.py always needs the API (image generation goes through `client`).

Every call() adds to USAGE, so any script can report where its tokens went; the totals of one process are printed at exit.
A forked/spawned worker keeps its own USAGE, so play.episode() returns its own delta and skillopt_lite sums those.
"""
import atexit
import base64
import json

import httpx
import os
import subprocess
import tempfile
from pathlib import Path

from openai import OpenAI
from codex_provider import child_environment as codex_environment
from codex_provider import cli_args as codex_provider_args
from codex_provider import from_environment as codex_provider_from_environment

BACKEND = os.environ.get("LLM_BACKEND", "api")
OPTIMIZER_BACKEND = os.environ.get("OPTIMIZER_BACKEND", BACKEND)
CODEX = os.environ.get("CODEX_BIN", "codex")
CODEX_TIMEOUT = int(os.environ.get("GAMECRAFT_CODEX_TIMEOUT", "300"))
if CODEX_TIMEOUT < 1:
    raise ValueError("GAMECRAFT_CODEX_TIMEOUT must be a positive integer")
CODEX_TRIES = 3  # a failed subprocess is retried, since one error would kill a whole rollout
client = None  # Initialized lazily when an API request is made.
local = OpenAI(base_url=os.environ.get("LOCAL_URL", "http://127.0.0.1:30020/v1"), api_key="none", max_retries=1,
               http_client=httpx.Client(trust_env=False, timeout=600)) if "local" in (BACKEND, OPTIMIZER_BACKEND) else None  # trust_env=False: never via http_proxy
USAGE = {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0}


def img(png, detail="auto", width=None, jpeg_quality=None):  # detail="low" = one 512px pass (85 tokens) instead of tiling a 1280x720 frame (~1.1k); codex ignores it
    if width or jpeg_quality:  # Downscale/compress replay evidence before it reaches the Codex gateway.
        import io
        from PIL import Image
        im = Image.open(png).convert("RGB")
        buf = io.BytesIO()
        if width and im.width > width:
            im = im.resize((width, im.height * width // im.width))
        fmt = "JPEG" if jpeg_quality else "PNG"
        im.save(buf, fmt, quality=jpeg_quality or 80)
        data = buf.getvalue()
    else:
        data = open(png, "rb").read()
    mime = "image/jpeg" if jpeg_quality else "image/png"
    return {"type": "input_image", "detail": detail, "image_url": f"data:{mime};base64," + base64.b64encode(data).decode()}


def call(model, instructions, content, name, schema, effort=None, backend=None, max_tokens=600,
         read_only_project=None):  # one call whose output must match a strict JSON schema
    backend = backend or BACKEND  # max_tokens only matters for the local backend: a one-line decision vs a multi-edit skill patch
    extra = {}
    if read_only_project is not None:
        if backend != 'codex':
            raise ValueError('On-demand project reading requires the Codex backend')
        extra['read_only_project'] = read_only_project
    out, u = {"api": _api, "codex": _codex, "local": _local}[backend](model, instructions, content, name, schema, effort, max_tokens, **extra)
    for k, v in u.items():
        USAGE[k] += v
    USAGE["calls"] += 1
    return out


def api_client():
    """Initialize credentials only when the API backend is actually used."""
    global client
    if client is None:
        client = OpenAI()
    return client


def _api(model, instructions, content, name, schema, effort, max_tokens=None):
    r = api_client().responses.create(model=model, instructions=instructions, input=[{"role": "user", "content": content}],
                                text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
                                **({"reasoning": {"effort": effort}} if effort else {}))
    u = r.usage
    return json.loads(r.output_text), {"input": u.input_tokens, "cached": getattr(u.input_tokens_details, "cached_tokens", 0),
                                       "output": u.output_tokens, "reasoning": getattr(u.output_tokens_details, "reasoning_tokens", 0)}


def _local(model, instructions, content, name, schema, effort, max_tokens=600):  # effort is ignored: an Instruct model has no reasoning switch
    msgs = [{"role": "system", "content": instructions},
            {"role": "user", "content": [{"type": "text", "text": c["text"]} if c["type"] == "input_text" else
                                         {"type": "image_url", "image_url": {"url": c["image_url"]}} for c in content]}]
    budget = max_tokens
    usage = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    for attempt in range(3):  # A strict schema still needs enough tokens to finish the JSON.
        r = local.chat.completions.create(model="local", messages=msgs, temperature=0.2 * attempt, max_tokens=max_tokens,
                                          response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}})
        u = r.usage
        usage['input'] += u.prompt_tokens
        usage['output'] += u.completion_tokens
        try:
            if r.choices[0].finish_reason == 'length':
                raise json.JSONDecodeError('Truncated response', r.choices[0].message.content or '', 0)
            return json.loads(r.choices[0].message.content), usage
        except json.JSONDecodeError as e:
            print(f"[local] {name} attempt {attempt + 1}: unparsable output ({r.choices[0].finish_reason}, {u.completion_tokens} tokens), retrying", flush=True)
            if attempt == 2:
                raise RuntimeError(f"local model returned no parsable {name} in 3 attempts: {r.choices[0].message.content[:200]!r}") from e
            if r.choices[0].finish_reason == 'length':
                max_tokens = min(max_tokens * 2, max(budget, 4096))


def _codex(model, instructions, content, name, schema, effort, max_tokens=None, *, read_only_project=None):
    provider = codex_provider_from_environment()
    env = codex_environment(provider)
    project = Path(read_only_project).resolve(strict=True) if read_only_project is not None else None
    if project is not None and not project.is_dir():
        raise ValueError('Codex source workspace must be a directory')
    for attempt in range(CODEX_TRIES):
        with tempfile.TemporaryDirectory(prefix="codex_") as d:
            # Keep response/schema/image files outside the read-only candidate.
            # Ordinary calls retain their temporary working directory.
            Path(d, 'schema.json').write_text(json.dumps(schema))
            cmd = [CODEX, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "-c", 'approval_policy="never"',
                   *codex_provider_args(provider), "--cd", str(project) if project is not None else d, "--model", model,
                   "--output-schema", f"{d}/schema.json", "--output-last-message", f"{d}/out.txt"]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            texts = [instructions]  # codex exec has no system slot: the instructions lead the prompt
            for i, c in enumerate(content):
                if c["type"] == "input_text":
                    texts.append(c["text"])
                else:
                    extension = "jpg" if c["image_url"].startswith("data:image/jpeg;") else "png"
                    image_path = f"{d}/{i}.{extension}"
                    Path(image_path).write_bytes(base64.b64decode(c["image_url"].split(",", 1)[1]))
                    cmd += ["--image", image_path]
            try:
                p = subprocess.run(cmd + ["-"], input="\n\n".join(texts), text=True, capture_output=True, timeout=CODEX_TIMEOUT, env=env)
                u = {}
                for line in p.stdout.splitlines():
                    if line.startswith("{") and '"turn.completed"' in line:
                        u = json.loads(line).get("usage") or {}
                if p.returncode:
                    failures=[]
                    for line in p.stdout.splitlines():
                        try:
                            event=json.loads(line)
                        except ValueError:
                            continue
                        if event.get('type') in ('error','turn.failed'):
                            failures.append(json.dumps(event,ensure_ascii=False)[-1000:])
                    detail='; '.join(failures[-2:]) or (p.stderr or p.stdout)[-500:]
                    raise RuntimeError(f"codex exit {p.returncode}: {detail}")
                return json.loads(Path(d, 'out.txt').read_text()), {
                    "input": u.get("input_tokens", 0), "cached": u.get("cached_input_tokens", 0),
                    "output": u.get("output_tokens", 0), "reasoning": u.get("reasoning_output_tokens", 0)}
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
                if attempt == CODEX_TRIES - 1:
                    raise
                print(f"[codex] {name} attempt {attempt + 1} failed, retrying: {str(e)[:200]}", flush=True)


def usage_line(u=USAGE):
    return (f"[usage {BACKEND}] {u['calls']} calls | input {u['input']} (cached {u['cached']}, {u['cached'] / max(1, u['input']):.0%}) "
            f"| output {u['output']} (reasoning {u['reasoning']}, {u['reasoning'] / max(1, u['output']):.0%}) "
            f"| per call in {u['input'] / max(1, u['calls']):.0f} out {u['output'] / max(1, u['calls']):.0f}")


atexit.register(lambda: USAGE["calls"] and print(usage_line(), flush=True))
