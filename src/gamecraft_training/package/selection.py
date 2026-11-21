"""Adaptive anonymous pairwise candidate selection."""


def promotion(reviews):
    """Use one clear review; an optional reversed review resolves low-confidence/tie results."""
    if not 1 <= len(reviews) <= 2:
        return False, 'pairwise selection requires one review and at most one confirmation'
    first=reviews[0]
    if not first.get('evidence_valid') or first.get('preferred_role')=='unavailable':
        return False, 'pairwise evidence was unavailable or invalid'
    if first.get('critical_regression'):
        return False, 'pairwise reviewer found a critical regression'
    if len(reviews)==1:
        if first.get('preferred_role')=='candidate' and first.get('confidence')!='low':
            return True, 'blind pairwise review preferred the candidate'
        return False, 'blind pairwise review did not clearly prefer the candidate'
    second=reviews[1]
    if not second.get('evidence_valid') or second.get('critical_regression'):
        return False, 'pairwise confirmation was invalid or found a critical regression'
    if second.get('preferred_role')=='candidate' and second.get('confidence')!='low':
        return True, 'reversed-order confirmation preferred the candidate'
    return False, 'reversed-order confirmation did not prefer the candidate'


def needs_confirmation(review):
    return (review.get('evidence_valid') is True and not review.get('critical_regression') and
            (review.get('preferred_role')=='tie' or review.get('confidence')=='low'))


def label_review(review, roles):
    """Translate an anonymous A/B choice to a stable incumbent/candidate role."""
    preferred = review.get('preferred')
    if preferred in ('A', 'B'):
        role = roles[preferred]
    elif preferred in ('tie', 'unavailable'):
        role = preferred
    else:
        raise ValueError('Invalid pairwise preference')
    return {**review, 'preferred_role': role, 'roles': dict(roles)}
