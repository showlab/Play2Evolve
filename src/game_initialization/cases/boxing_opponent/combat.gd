extends RefCounted
# Deterministic combat: opponents observe committed actions, never future inputs.
var rules: Dictionary
var profile: Dictionary
var actors: Array = []
var rng := RandomNumberGenerator.new()
var ticks := 0
var match_over := false
var outcome := "playing"
var event_log: Array = []
var cooldown := 0
var pattern: Array = []
var pattern_index := 0
var needs_guard := false
var next_counter := false
var reaction_ticks := 0

func reset(config: Dictionary, difficulty: String, seed_value: int) -> void:
    rules = config
    profile = rules.opponents[difficulty]
    rng.seed = seed_value
    ticks = 0
    match_over = false
    outcome = "playing"
    event_log = []
    actors = []
    for i in range(2):
        actors.append({"hp": 100, "stamina": 100.0, "action": "idle", "age": 0,
            "remaining": 0, "contact": 0, "duration": 0, "landed": false,
            "combo": 0, "last_hit": -1000, "hits_taken": 0})
    cooldown = int(profile.opening_ticks) + rng.randi_range(0, 12)
    pattern = []
    pattern_index = 0
    needs_guard = false
    next_counter = false
    reaction_ticks = 0

func is_attack(name: String) -> bool:
    return rules.attacks.has(name)

func phase(index: int) -> String:
    var a: Dictionary = actors[index]
    if is_attack(a.action):
        return "tell" if a.age < a.contact else "recovery"
    return a.action

func target(index: int) -> String:
    var a: Dictionary = actors[index]
    return str(rules.attacks[a.action].target) if is_attack(a.action) and phase(index) == "tell" else "none"

func start_action(index: int, name: String) -> bool:
    var a: Dictionary = actors[index]
    if match_over or a.remaining > 0 or not rules.costs.has(name):
        return false
    var cost := float(rules.costs[name])
    if a.stamina < cost:
        return false
    var tired: bool = a.stamina < float(rules.fatigue_threshold)
    a.stamina -= cost
    a.action = name
    a.age = 0
    a.landed = false
    a.contact = 0
    if is_attack(name):
        var move: Dictionary = rules.attacks[name]
        var speed := float(profile.tell_scale) if index == 1 else 1.0
        var fatigue := float(rules.fatigue_slowdown) if tired else 1.0
        a.contact = maxi(1, int(ceil(float(move.tell_ticks) * speed * fatigue)))
        a.duration = a.contact + int(ceil(float(move.recovery_ticks) * fatigue))
    else:
        a.duration = int(rules.durations[name])
        if index == 1 and name == "block":
            a.duration = int(profile.guard_ticks)
    a.remaining = a.duration
    event_log.append({"tick": ticks, "kind": "start", "actor": index,
        "action": name, "contact_after": a.contact, "duration": a.duration})
    return true

func opponent_step() -> void:
    # Readiness is retained while busy; an interrupted turn is retried on recovery.
    cooldown = maxi(0, cooldown - 1)
    var enemy: Dictionary = actors[1]
    var player: Dictionary = actors[0]
    if phase(0) == "tell":
        reaction_ticks += 1
    else:
        reaction_ticks = 0
    if enemy.remaining > 0:
        return
    if needs_guard:
        if start_action(1, "block"):
            needs_guard = false
            next_counter = true
            cooldown = 0
            return
        # When exhausted the guard cannot be raised: remain vulnerable and recover.
        return
    if next_counter:
        if start_action(1, "jab"):
            next_counter = false
            cooldown = int(profile.combo_gap_ticks)
        return
    # React only after observing a committed tell for the configured latency.
    if reaction_ticks >= int(profile.reaction_ticks) and phase(0) == "tell":
        if start_action(1, "block"):
            next_counter = true
            cooldown = 0
        return
    if cooldown > 0:
        return
    if enemy.stamina < float(profile.rest_below):
        cooldown = int(profile.rest_ticks)
        return
    if pattern_index >= pattern.size():
        pattern = profile.patterns[rng.randi_range(0, profile.patterns.size()-1)].duplicate()
        pattern_index = 0
    var attack := str(pattern[pattern_index])
    if start_action(1, attack):
        pattern_index += 1
        cooldown = int(actors[1].duration) + (int(profile.combo_gap_ticks) if pattern_index < pattern.size() else int(profile.pause_ticks))

func resolve_strike(attacker: int, defender_state: Dictionary) -> void:
    var a: Dictionary = actors[attacker]
    var d: Dictionary = actors[1-attacker]
    a.landed = true
    var move: Dictionary = rules.attacks[a.action]
    var evaded: bool = defender_state.remaining > 0 and defender_state.action == ("dodge_high" if move.target == "high" else "dodge_low")
    var blocked: bool = defender_state.remaining > 0 and defender_state.action == "block"
    var damage := 0
    if not evaded:
        damage = int(move.damage)
        if ticks - int(a.last_hit) <= int(rules.combo_window_ticks) and a.combo > 0:
            damage += int(rules.combo_bonus)
        if blocked:
            var strong: bool = defender_state.stamina >= float(rules.guard_threshold)
            damage = 0 if strong else int(ceil(damage * float(rules.weak_guard_damage)))
            d.stamina = maxf(0, float(d.stamina) - float(rules.guard_impact_cost))
            a.remaining += int(rules.block_recoil_ticks)
    event_log.append({"tick": ticks, "kind": "strike", "actor": attacker,
        "action": a.action, "blocked": blocked, "evaded": evaded, "damage": damage})
    if damage == 0:
        a.combo = 0
        return
    a.combo = int(a.combo) + 1 if ticks - int(a.last_hit) <= int(rules.combo_window_ticks) else 1
    a.last_hit = ticks
    d.hp = maxi(0, int(d.hp) - damage)
    d.hits_taken += 1
    d.action = "hit"
    d.age = 0
    d.remaining = int(rules.durations.hit)
    d.duration = d.remaining
    # A struck opponent prepares a visible guard, then a normal telegraphed counter.
    if attacker == 0:
        needs_guard = true
        next_counter = false
        pattern_index = pattern.size()

func step() -> void:
    if match_over:
        return
    ticks += 1
    opponent_step()
    var striking: Array = []
    for i in range(2):
        var a: Dictionary = actors[i]
        a.stamina = minf(100.0, a.stamina + float(rules.regen_idle if a.action == "idle" else rules.regen_active))
        if a.remaining > 0:
            a.age += 1
            a.remaining -= 1
            if is_attack(a.action) and not a.landed and a.age == a.contact:
                striking.append(i)
        else:
            a.age += 1
    # Snapshot both defenses; simultaneous impacts trade, with no player-first bias.
    var saved := actors.duplicate(true)
    for i in striking:
        actors[i].action = saved[i].action
        resolve_strike(i, saved[1-i])
    # Restore hit reactions after simultaneous strikes (both outgoing blows landed).
    for i in range(2):
        if actors[i].hp < saved[i].hp:
            actors[i].action = "hit"
            actors[i].age = 0
            actors[i].remaining = int(rules.durations.hit)
            actors[i].duration = actors[i].remaining
        elif actors[i].remaining == 0:
            actors[i].action = "idle"
            actors[i].age = 0
    if actors[0].hp == 0 or actors[1].hp == 0 or ticks >= int(rules.bout_ticks):
        match_over = true
        outcome = "win" if actors[0].hp > actors[1].hp else ("loss" if actors[0].hp < actors[1].hp else "draw")
