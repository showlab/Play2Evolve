"""Engine-neutral contract for generated game assets.

The contract is an internal representation derived from the existing asset
schema.  Agents describe the material; this module owns how that material must
be delivered to the game and translated for a generation backend.
"""
import re
from pathlib import Path


GENERATION_SCOPES = {'single_subject', 'coupled_action', 'scene'}
MOTION_MODES = {'static', 'loop', 'event'}


def material_intent(asset):
    """Resolve an optional generation decision without changing legacy plans."""
    kind = asset['kind']
    scope = asset.get('generation_scope', 'scene' if kind == 'background' else 'single_subject')
    motion = asset.get('motion', ('loop' if asset.get('loop') else 'event') if kind == 'animation' else 'static')
    if scope not in GENERATION_SCOPES or motion not in MOTION_MODES:
        raise ValueError('Unknown material generation scope or motion')
    return scope, motion


_SHARED_STYLE_PRODUCTION_LANGUAGE = re.compile(
    r'\b(?:chroma(?:[- ]key)?|green[- ]?screen|pure\s+green|transparent(?:\s+background)?|'
    r'alpha(?:\s+channel|\s+background)?|isolated\s+(?:sprite|prop|subject|character|actor)|'
    r'sprite\s+sheet|first\s+frame|last\s+frame|keyframe|generous\s+margins?)\b',
    re.IGNORECASE,
)


def validate_shared_style(style):
    """Require a batch-wide style to be valid for every material role.

    Output mechanics belong to the per-asset contract/prompt.  Rejecting them
    here is deliberate: silently deleting words would make the actual H3 input
    hard to audit and would merely patch individual prompt failures.
    """
    if not isinstance(style, str) or not style.strip():
        raise ValueError('Shared visual style must be non-empty prose')
    match = _SHARED_STYLE_PRODUCTION_LANGUAGE.search(style)
    if match:
        raise ValueError(
            'Shared visual style contains an asset-production instruction '
            f'({match.group(0)!r}); keep style to art direction and put output '
            'mechanics in the individual asset definition')
    return style.strip()


def generation_contract(asset):
    """Derive one authoritative output contract from the current asset fields."""
    kind = asset['kind']
    scope, motion = material_intent(asset)
    if kind == 'background':
        return {
            'representation': 'opaque_scene' if motion == 'static' else 'animated_scene',
            'alpha_required': False,
            'motion': motion,
            'reference_policy': 'first_and_last' if asset.get('reference_asset_id') and motion == 'loop' else
                                'first' if asset.get('reference_asset_id') else 'none',
            'method': 'fl2va' if asset.get('reference_asset_id') else 't2va',
            'aspect_ratio': '16:9',
            'chroma_key': False,
            'shared_crop': False,
        }
    if kind == 'sprite':
        return {
            'representation': 'isolated_cutout',
            'alpha_required': True,
            'motion': 'static',
            'reference_policy': 'none',
            'method': 't2va',
            'aspect_ratio': '1:1',
            'chroma_key': True,
            'shared_crop': True,
        }
    if kind == 'animation':
        return {
            'representation': 'coupled_sequence' if scope == 'coupled_action' else 'isolated_sequence',
            'alpha_required': True,
            'motion': motion,
            'reference_policy': 'first_and_last' if motion == 'loop' else 'first',
            'method': 'fl2va',
            'aspect_ratio': '1:1',
            'chroma_key': True,
            'shared_crop': True,
        }
    raise ValueError(f'Unknown asset kind: {kind}')


def contract_instruction(contract):
    """Compile backend-facing prose from a validated output contract."""
    representation = contract['representation']
    if representation == 'opaque_scene':
        return ('Render a complete opaque environmental plate with fixed framing. '
                'Preserve a clear playable region for later compositing.')
    if representation == 'animated_scene':
        ending = ('Return to the opening composition for a seamless loop.'
                  if contract['motion'] == 'loop' else 'Complete one continuous event.')
        return ('Render one complete opaque scene with a locked camera and stable playable region. '
                'Keep game UI and state-dependent controls out of the video. ' + ending)
    if representation == 'isolated_cutout':
        return ('Render one isolated static foreground subject, fully visible with generous margins, '
                'on a uniform pure green #00FF00 chroma background for transparent cutout extraction.')
    if representation == 'isolated_sequence':
        ending = ('Return precisely to the starting pose for a seamless loop.'
                  if contract['motion'] == 'loop'
                  else 'Complete one action without looping or camera movement.')
        return ('Render one isolated foreground subject, fully visible with generous margins, '
                'on a uniform pure green #00FF00 chroma background for transparent frame extraction. '
                + ending)
    if representation == 'coupled_sequence':
        ending = ('Return all subjects to the starting pose for a seamless loop.'
                  if contract['motion'] == 'loop' else 'Complete the interaction once.')
        return ('Render all interacting subjects and props as one synchronized foreground unit, '
                'fully visible with generous margins, a fixed camera, and a uniform pure green #00FF00 '
                'chroma background for transparent frame extraction. ' + ending)
    raise ValueError(f'Unknown representation: {representation}')


def reference_instruction(scope='single_subject', *, transparent=False):
    """Backend requirement for an animation identity reference image."""
    subject = ('all interacting subjects and props together' if scope == 'coupled_action'
               else 'one isolated static full subject')
    background = ('on a transparent background' if transparent else
                  'on a uniform pure green #00FF00 chroma background')
    return ('Render ' + subject + ', fully visible with generous margins, '
            + background + '. Use a fixed camera.')


def compile_generation_prompt(style, asset, instructions=()):
    """Compile auditable natural language without mixing style and delivery rules."""
    pieces = [str(style).strip(), asset['prompt'].strip(),
              'Output requirements: ' + contract_instruction(generation_contract(asset))]
    learned = [str(x).strip() for x in instructions if str(x).strip()]
    if learned:
        pieces.append('Learned generation requirements: ' + ' '.join(learned))
    return ' '.join(piece for piece in pieces if piece)


def descriptor(asset):
    foreground=asset['kind'] != 'background'
    return {
        'kind':asset['kind'],
        'frames':asset['frames'],
        'size':list(asset['size']),
        'alpha_mode':'rgba' if foreground else 'opaque',
        'anchor':asset.get('anchor','bottom_center' if foreground else 'center'),
        'crop_policy':asset.get('crop_policy','contain'),
        'identity_group':asset.get('identity_group',asset['id']),
    }


def validate_bound(root,asset,files):
    """Validate the file interface; semantic quality remains a visual judgment."""
    from PIL import Image
    spec=descriptor(asset)
    if len(files)!=spec['frames']:
        raise ValueError('Asset file count violates its declared contract')
    for name in files:
        path=Path(root)/name
        with Image.open(path) as image:
            if list(image.size)!=spec['size']:
                raise ValueError(f'Asset size violates contract: {name}')
            if spec['alpha_mode']=='rgba':
                if image.mode!='RGBA':
                    raise ValueError(f'Foreground asset must be canonical RGBA: {name}')
                lo,hi=image.getchannel('A').getextrema()
                if lo==hi:
                    raise ValueError(f'Foreground asset needs both transparent and opaque pixels: {name}')
    return spec
