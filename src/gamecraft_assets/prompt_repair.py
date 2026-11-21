"""Rewrite one H3 material prompt from task evidence, without inheriting bad prose."""
import json
import os


def rewrite_prompt(asset, *, style, problem, history=(), task_brief=''):
    from llm import call

    model = os.environ.get('GAMECRAFT_ASSET_PROMPT_MODEL') or os.environ.get('GAMECRAFT_VIDEO_SKILL_MODEL', 'gpt-5.5')
    schema = {
        'type': 'object', 'additionalProperties': False,
        'required': ['repairable', 'prompt', 'reason'],
        'properties': {
            'repairable': {'type': 'boolean'},
            'prompt': {'type': 'string'},
            'reason': {'type': 'string'},
        },
    }
    context = {
        'task_brief': task_brief[:20000],
        'shared_visual_style': style,
        'asset': {key: value for key, value in asset.items()
                  if key not in {'revision'}},
        'observed_problem': problem,
        'recent_problems': list(history)[-3:],
    }
    result = call(
        model,
        'You are the prompt author for ONE H3-generated game material. Write a COMPLETE replacement '
        'for asset.prompt, not a suffix, correction note, or patch. The existing prompt, description, '
        'and requirements may contain mistakes; treat them as hypotheses. Use the public task brief '
        'and observed failure to resolve contradictions. Preserve only supported subject identity and '
        'material purpose. Describe positive, observable appearance and motion inside this material. '
        'Do not put game rules, UI state, collision, placement of separately rendered assets, or output '
        'mechanics in the prompt; the generation contract handles delivery. If the defect cannot be '
        'fixed by changing this material prompt, or the task evidence cannot identify the intended '
        'material, return repairable=false with an empty prompt and explain why.',
        [{'type': 'input_text', 'text': json.dumps(context, ensure_ascii=False)}],
        'asset_prompt_rewrite', schema, backend='codex', max_tokens=1000,
    )
    if not result['repairable']:
        return {'repairable': False, 'prompt': '', 'reason': result['reason'], 'model': model}
    prompt = result['prompt'].strip()
    if not prompt or len(prompt) > 4000:
        raise ValueError('Prompt agent returned an empty or oversized replacement')
    return {'repairable': True, 'prompt': prompt, 'reason': result['reason'], 'model': model}
