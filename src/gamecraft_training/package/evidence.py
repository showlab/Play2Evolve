"""Sample actual-policy episodes across time; labels keep evidence attributable."""
import json
from pathlib import Path

REPLAY_IMAGE_WIDTH = 854


def sample(results, limit=24, *, player_visible=False, scene_focus=False):
    """Sample labeled frames, optionally limiting state to what the Player saw.

    ``evaluation_state`` is private replay/verification telemetry. Behavior Skill
    optimization and verification must use ``player_state`` instead so they cannot
    create or enforce rules from information that was unavailable at decision time.
    """
    if not results:return []
    buckets=[]
    for episode_index,r in enumerate(results[:limit]):
        ds=r['decisions']
        if not ds:continue
        count=min(len(ds),limit) if scene_focus else min(4,len(ds))
        indexes=sorted({round(i*(len(ds)-1)/(count-1)) for i in range(count)}) if count>1 else [0]
        label=f'episode-{episode_index:03d}' if player_visible else r['scenario']
        buckets.append([{'scenario':label,'frame':ds[i]['frame'],'image':ds[i]['image'],
                         'action':ds[i]['action'],'reason':ds[i]['reason'],
                         'state':(ds[i].get('player_state',{}) if player_visible else
                                  ds[i].get('evaluation_state',ds[i].get('player_state',{})))} for i in indexes])
    chosen=[]
    for i in range(max((len(bucket) for bucket in buckets),default=0)):
        for bucket in buckets:
            if i<len(bucket) and len(chosen)<limit:chosen.append(bucket[i])
    return chosen


def content(results, limit=24, *, include_reasons=False, player_visible=False, scene_focus=False):
    out=[]
    for item in sample(results,limit,player_visible=player_visible,scene_focus=scene_focus):
        import llm
        out.append({'type':'input_text','text':json.dumps({k:v for k,v in item.items() if k!='image' and (include_reasons or k!='reason')})})
        # Resize model-input copies (1280x720 -> 854x480); retain captured frames
        # and sampling times for detailed inspection.
        out.append(llm.img(item['image'], width=REPLAY_IMAGE_WIDTH, jpeg_quality=85))
    return out


def outcome_content(results):
    """Give world judges the post-action state and screenshot omitted by decisions.

    Player Skill calls must continue to use only decision-time public observations.
    """
    out=[]
    for result in results:
        decisions=result.get('decisions') or []
        if not decisions and not result.get('final_image'):
            continue
        last=decisions[-1] if decisions else {}
        frame=result.get('frames',last.get('frame',0)+result.get('decision_frames',0))
        image=(Path(result['final_image']) if result.get('final_image') else
               Path(last['image']).with_name(f'frame_{len(decisions):04d}.png')
               if last.get('image') else None)
        out.append({'type':'input_text','text':json.dumps({
            'scenario':result['scenario'],'frame':frame,'observation':'post-action final observation',
            'replay_verified':result.get('replay_verified'),
            'final_state':result.get('replay',{}),
            'run_objective':result.get('run_objective'), 'completion':result.get('completion'),
            'completion_note':'Player success/failure is a claim: verify against evidence. Budget truncation is not success or defeat.',
            'final_screenshot_available':bool(image and image.is_file())})})
        if image and image.is_file():
            import llm
            out.append(llm.img(str(image), width=REPLAY_IMAGE_WIDTH, jpeg_quality=85))
    return out
