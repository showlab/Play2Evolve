"""Generic issue-level repair states, independent of any game or engine."""

VALID_STATES = frozenset({'gone', 'still_there', 'not_exercised'})


def state(verdict):
    """Return the canonical verifier state while accepting legacy verdicts."""
    value = verdict.get('status')
    if value in VALID_STATES:
        return value
    if not verdict.get('coverage_passed'):
        return 'not_exercised'
    return 'gone' if verdict.get('defect_fixed') else 'still_there'


def attempted_outcome(verdict, attempted=True):
    """Use RSIGame-style issue bookkeeping without treating missing evidence as failure."""
    value = state(verdict)
    if value == 'not_exercised':
        return 'unobserved'
    if value == 'gone':
        return 'fixed' if attempted else 'fixed_incidentally'
    return 'failed' if attempted else 'not_attempted'


def validate(verdict, checks):
    value = state(verdict)
    rows = verdict.get('acceptance_checks', [])
    names = [row.get('criterion') for row in rows]
    if len(names) != len(set(names)) or set(names) != set(checks):
        raise ValueError('Repair verifier must assess every acceptance criterion exactly once')
    if value == 'gone' and not verdict.get('defect_fixed'):
        raise ValueError('A gone defect must be marked fixed')
    if value == 'not_exercised' and verdict.get('defect_fixed') is True:
        raise ValueError('An unobserved defect cannot be marked fixed')
    return value
