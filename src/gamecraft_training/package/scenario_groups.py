"""Validate initialization groups without changing the declared scenario scripts."""


def build(scenarios, groups):
    if not isinstance(groups, list) or not groups:
        raise ValueError('Initialization groups must be a nonempty list')
    by_id={case['id']:case for case in scenarios}
    if len(by_id)!=len(scenarios):
        raise ValueError('Initialization scenario IDs must be unique')
    units=[];seen=set();names=set()
    for group in groups:
        if not isinstance(group, dict) or set(group)!={'id','scenarios'}:
            raise ValueError('Each initialization group needs id and scenarios')
        name=group['id'];ids=group['scenarios']
        if not isinstance(name,str) or not name or name in names or name in by_id:
            raise ValueError('Initialization group IDs must be unique and distinct from scenario IDs')
        if not isinstance(ids,list) or not ids or any(not isinstance(x,str) for x in ids):
            raise ValueError('Initialization group scenarios must be a nonempty list of IDs')
        if any(x not in by_id or x in seen for x in ids):
            raise ValueError('Initialization groups must contain each declared scenario once')
        seen.update(ids);names.add(name)
        units.append({'id':name,'scenario_ids':ids,'members':[by_id[x] for x in ids]})
    if seen!=set(by_id):
        raise ValueError('Initialization groups must cover every declared initialization scenario')
    return units
