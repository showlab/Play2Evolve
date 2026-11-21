"""Public-brief acceptance gates, separate from the construction context.

These are experiment integrity checks, not security signatures or benchmark scores.
Semantic decisions remain model reviews; missing evidence fails closed.
"""
import re

from game_initialization.batch.__main__ import codex, inside, read, sha, write
from game_initialization.package import inventory


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}
SPEC_SCHEMA = object_schema({'requirements': {'type': 'array', 'items': object_schema({
    'id': STRING, 'quote': STRING, 'description': STRING, 'asset_ids': STRINGS,
    'verification': STRING, 'dynamic': {'type': 'boolean'}})}})
REVIEW_SCHEMA = object_schema({
    'passed': {'type': 'boolean'}, 'missing_requirements': STRINGS, 'limitations': STRINGS,
    'requirements': {'type': 'array', 'items': object_schema({
        'id': STRING, 'passed': {'type': 'boolean'}, 'code': STRINGS,
        'runtime_checks': STRINGS, 'reason': STRING})}})
QUOTE_REPAIR_SCHEMA = object_schema({'corrections': {'type': 'array', 'items': object_schema({
    'id': STRING, 'quote': STRING})}})


def align_quotes(brief, spec):
    """Resolve harmless whitespace drift without relaxing the exact-source check."""
    for row in spec['requirements']:
        quote = row['quote']
        if quote in brief or not quote.strip():
            continue
        pattern = re.compile(r'\s+'.join(re.escape(part) for part in quote.split()))
        matches = list(pattern.finditer(brief))
        if len(matches) == 1:
            row['quote'] = matches[0].group()
    return spec


def repair_invalid_quotes(root, cfg, spec):
    """The extractor occasionally quotes its own instructions rather than the brief."""
    brief = (root/'instruction.md').read_text()
    align_quotes(brief, spec)
    invalid = [r for r in spec['requirements'] if not r['quote'].strip() or r['quote'] not in brief]
    if not invalid:
        return spec
    prompt = ('Read ONLY instruction.md. The following requirement rows have quotes that are '
              'not exact contiguous substrings of that file. For each ID return either an '
              'exact quote copied from instruction.md supporting that requirement, or an '
              'empty quote if the requirement came from extraction instructions rather than '
              'the public brief. Do not edit files. Rows:\n'
              + '\n'.join(f"{r['id']}: {r['description']} | invalid quote: {r['quote']}" for r in invalid))
    for attempt in range(2):
        if not invalid:
            break
        reminder = ('' if attempt == 0 else
                    '\nThe prior correction still was not an exact substring. '
                    'Copy directly from instruction.md, preserving every character, '
                    'including Markdown markers and punctuation. An empty quote '
                    'means the row is unsupported and will be removed.')
        fixed = codex(root, cfg, f'public-requirements-quote-repair-{attempt+1}',
                      prompt + reminder, schema=QUOTE_REPAIR_SCHEMA)['corrections']
        replacements = {r['id']: r['quote'] for r in fixed}
        expected = {r['id'] for r in invalid}
        if set(replacements) != expected or len(fixed) != len(expected):
            raise ValueError('Quote repair did not address each invalid requirement exactly once')
        for row in invalid:
            row['quote'] = replacements[row['id']]
        align_quotes(brief, spec)
        invalid = [r for r in spec['requirements']
                   if r['quote'].strip() and r['quote'] not in brief]
        if invalid:
            prompt = ('Read ONLY instruction.md. These corrected quotes are STILL '
                      'not exact contiguous substrings. Return a literal substring '
                      'for each ID or an empty quote if unsupported. Rows:\n'
                      + '\n'.join(f"{r['id']}: {r['description']} | invalid quote: {r['quote']}"
                                  for r in invalid))
    spec['requirements'] = [r for r in spec['requirements'] if r['quote'].strip()]
    return spec


def validate_spec(root, spec):
    brief = (root/'instruction.md').read_text()
    rows = spec['requirements']
    ids = [r['id'] for r in rows]
    if not rows or len(set(ids)) != len(ids) or any(not i.strip() for i in ids):
        raise ValueError('Empty or duplicate public requirements')
    for r in rows:
        if (not r['quote'].strip() or r['quote'] not in brief
                or not r['description'].strip() or not r['verification'].strip()
                or type(r['dynamic']) is not bool):
            raise ValueError('Public requirement '+r['id']+
                             ' needs exact brief quote and acceptance condition')


def prepare_requirements(root, cfg):
    """Extract before building; the builder never writes its own acceptance scope."""
    path = root/'requirements.json'
    if not path.exists():
        prompt = '''Read ONLY instruction.md, not draft, previous games or hidden rubrics.
Extract ALL requirements of the complete public brief before implementation.
Return atomic requirements with exact contiguous source quotes, full descriptions
and concrete acceptance conditions. Preserve every feature, screen, action,
progression, victory/failure condition and technical constraint. Do not reduce
scope to a prototype or vertical slice. A compound sentence can need multiple
requirements. dynamic=true for behavior that needs execution to establish;
false for static technical/content constraints. asset_ids=[] (bindings are chosen
later). Do not invent counts, difficulties or benchmark requirements absent from
the brief. H3 imagery replaces supplied asset libraries, and /workspace/game maps
to draft/game; these substitutions do not reduce functionality. Never edit files.'''
        spec = codex(root, cfg, 'public-requirements', prompt, schema=SPEC_SCHEMA)
        spec = repair_invalid_quotes(root, cfg, spec)
        validate_spec(root, spec)
        write(path, spec)
        write(root/'requirements_origin.json', {'instruction_sha256': sha(root/'instruction.md'),
              'requirements_sha256': sha(path)})
    spec = read(path)
    validate_spec(root, spec)
    origin = read(root/'requirements_origin.json')
    if origin != {'instruction_sha256': sha(root/'instruction.md'), 'requirements_sha256': sha(path)}:
        raise ValueError('Public acceptance scope changed; prepare a new workspace')
    return origin


def assert_scope(root, origin):
    if sha(root/'instruction.md') != origin['instruction_sha256'] or sha(root/'requirements.json') != origin['requirements_sha256']:
        raise ValueError('Builder changed the original public acceptance scope')


def needs_default_launch(requirement):
    """Return whether the public requirement explicitly concerns the entry point."""
    text=' '.join(str(requirement.get(k,'')) for k in ('quote','description','verification')).lower()
    explicit=('normal launch','default launch','without a scenario','without --scenario',
              'omit for a normal game launch','omitting it must mean normal launch')
    title_entry=(('start from the title' in text or 'starts from the title' in text or
                  'first screen' in text) and 'title' in text)
    return any(phrase in text for phrase in explicit) or title_entry


def validate_acceptance_plan(root):
    rows = read(root/'acceptance_plan.json')['requirements']
    expected = {r['id']: r for r in read(root/'requirements.json')['requirements']}
    declared = [c for c in read(root/'draft/scenarios.json') if c['purpose'] == 'initialization']
    cases = {c['id'] for c in declared}
    default_cases = {c['id'] for c in declared if c.get('launch','scenario') == 'default'}
    if len(rows) != len(expected) or {r['id'] for r in rows} != set(expected):
        raise ValueError('Acceptance plan must cover the original requirements')
    for row in rows:
        if (not row['implementation'].strip()
                or (expected[row['id']]['dynamic'] and not row['scenarios'])
                or not set(row['scenarios']).issubset(cases)):
            raise ValueError('Acceptance plan needs executable initialization coverage: '+row['id'])
        if (expected[row['id']]['dynamic'] and needs_default_launch(expected[row['id']])
                and not set(row['scenarios']) & default_cases):
            raise ValueError('Acceptance plan needs default-launch coverage: '+row['id'])


def validate_review(root, report, runtime):
    expected = {r['id']: r for r in read(root/'requirements.json')['requirements']}
    rows = report['requirements']
    if (report['passed'] is not True or report['missing_requirements']
            or len(rows) != len(expected) or {r['id'] for r in rows} != set(expected)):
        raise ValueError('Original brief not satisfied: '+str(report))
    case_root = inside(root, runtime['package'])/'checks'
    for row in rows:
        if row['passed'] is not True or not row['reason'].strip() or not row['code']:
            raise ValueError('Unverified public requirement: '+row['id'])
        for name in row['code']:
            p = inside(root, name)
            if root/'draft' not in p.parents or not p.is_file():
                raise ValueError('Review must cite actual draft code: '+name)
        if expected[row['id']]['dynamic'] and not row['runtime_checks']:
            raise ValueError('Missing execution evidence: '+row['id'])
        for name in row['runtime_checks']:
            p = inside(root, name)
            # Only parent-run current scenario results, not author-written evidence.
            if case_root not in p.parents or p.name != 'result.json' or not p.is_file():
                raise ValueError('Runtime evidence must come from current parent checks')
            result = read(p)
            if result.get('replay_verified') is not True or not result.get('decisions') or 'stepped' not in result:
                raise ValueError('Incomplete runtime observation: '+name)


def review_requirements(root, cfg, stage, runtime, origin):
    assert_scope(root, origin)
    before = inventory(root/'draft')
    evidence_before = inventory(inside(root, runtime['package']))
    prompt = '''Independently accept or reject this initialization. Never edit files.
Read the WHOLE instruction.md first, then requirements.json, actual draft code,
draft/verification.json and the CURRENT parent-run package/checks below. The
original brief is authoritative; find omissions or weakened conditions even in
the extracted checklist and put them in missing_requirements. Do not read hidden
benchmark rubrics or other games. No visual beauty score and no player training.
Do not trust author completion claims, file existence, rank labels, counters or
startup/replay success as proof of actual gameplay. A vertical slice, deferred
feature, stub or unsupported claim fails. Inspect actual state transitions in
decisions.json/result.json, trace inputs and replay frames as needed. For each
dynamic requirement cite current case result.json files in runtime_checks; explain
expected versus observed behavior in reason, including concrete state values.
Check that normal user inputs can reach the feature, not merely a bridge that
initializes an already-winning state. Progression must change gameplay as the
brief requests, and demonstrations must reach required success/failure/end states.
For static requirements cite actual implementing code. code paths are relative
to this workspace under draft/. Missing executable coverage means passed=false;
give precise repair feedback. Cosmetic imperfections may be recorded as limitations.
Do not invent opponent counts or other conditions not in the public brief.
H3 imagery and draft/game path are authorized substitutions only.
Current runtime report:\n'''+str(runtime)
    report = codex(root, cfg, stage, prompt, schema=REVIEW_SCHEMA)
    assert_scope(root, origin)
    if inventory(root/'draft') != before or inventory(inside(root, runtime['package'])) != evidence_before:
        raise ValueError('Read-only acceptance review changed the candidate')
    # Save rejection too, so repair feedback and every review are inspectable.
    write(root/'latest_requirements_review.json', {'review': report, 'origin': origin,
          'draft_sha256': before, 'evidence_sha256': evidence_before,
          'runtime_sha256': sha(root/'latest_check.json')})
    validate_review(root, report, runtime)


def require_current_review(root, runtime):
    if not (root/'latest_requirements_review.json').is_file():
        raise ValueError('Independent public requirements review is required before publication')
    attestation = read(root/'latest_requirements_review.json')
    assert_scope(root, attestation['origin'])
    if (attestation['draft_sha256'] != inventory(root/'draft')
            or attestation['evidence_sha256'] != inventory(inside(root, runtime['package']))
            or attestation['runtime_sha256'] != sha(root/'latest_check.json')):
        raise ValueError('No current independent requirements review')
    validate_review(root, attestation['review'], runtime)
