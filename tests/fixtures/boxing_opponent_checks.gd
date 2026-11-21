extends SceneTree
var failures: Array = []
var report: Array = []
var config: Dictionary
var combat_script = load("res://combat.gd")

func check(value: bool, message: String) -> void:
    if not value:
        failures.append(message)

func fresh(level: String = "contender", seed_value: int = 73):
    var c = combat_script.new()
    c.reset(config, level, seed_value)
    return c

func run_policy(level: String, seed_value: int, policy: String):
    var c = fresh(level, seed_value)
    for tick in range(720):
        if tick % 12 == 0:
            if policy == "spam":
                c.start_action(0,"jab")
            elif policy == "respond" and c.phase(1) == "tell":
                c.start_action(0,"dodge_high" if c.target(1) == "high" else "dodge_low")
        c.step()
        if c.match_over:
            break
    return c

func _initialize() -> void:
    config = JSON.parse_string(FileAccess.get_file_as_string("res://rules.json"))
    for level in ["rookie","contender","champion"]:
        var starts := {}
        var spam_damage := 0
        for seed_value in [73,74,75]:
            for policy in ["wait","spam","respond"]:
                var c = run_policy(level,seed_value,policy)
                var again = run_policy(level,seed_value,policy)
                check(c.event_log == again.event_log and c.actors == again.actors, "Determinism: " + level + policy)
                check(c.match_over, "Complete bout: " + level)
                var last_start := {}
                for event in c.event_log:
                    if event.kind == "start":
                        last_start[event.actor] = event
                        if event.actor == 1 and c.is_attack(event.action):starts[event.action] = true
                    elif event.kind == "strike":
                        check(event.tick - last_start[event.actor].tick >= last_start[event.actor].contact_after - 1, "Impact before tell")
                if policy == "wait":
                    check(c.actors[0].hp < 100, "Idle player must take damage: " + level)
                if policy == "spam":spam_damage += 100-int(c.actors[0].hp)
                if policy == "spam" and level == "champion":
                    check(c.outcome == "loss", "Repeated jab alone must not beat champion test fixtures")
                report.append({"level":level,"seed":seed_value,"policy":policy,"player_hp":c.actors[0].hp,"opponent_hp":c.actors[1].hp,"events":c.event_log.size()})
        check(starts.size() == 3, "All attack types: " + level)
        check(spam_damage > 0, "Spam must not permanently suppress opponent: " + level)
    for attack in ["jab","uppercut"]:
        for defense in ["dodge_high","dodge_low","block"]:
            var c = fresh()
            c.cooldown = 10000
            c.start_action(1,attack)
            for i in range(int(c.actors[1].contact)-6):c.step()
            c.start_action(0,defense)
            for i in range(6):c.step()
            var avoided: bool = defense == "block" or defense == ("dodge_high" if attack == "jab" else "dodge_low")
            check((c.actors[0].hp == 100) == avoided, "Defense type: " + attack + defense)
    var weak = fresh()
    weak.cooldown = 10000
    weak.actors[0].stamina = 8.0
    weak.start_action(1,"jab")
    for i in range(18):weak.step()
    weak.start_action(0,"block")
    for i in range(6):weak.step()
    check(weak.actors[0].hp < 100, "Low stamina weakens guard")
    var tired = fresh()
    tired.actors[0].stamina = 24.0
    tired.start_action(0,"jab")
    check(tired.actors[0].contact > 24, "Low stamina slows attack")
    var trade = fresh()
    trade.cooldown = 10000
    trade.start_action(0,"jab")
    trade.start_action(1,"jab")
    for i in range(24):trade.step()
    check(trade.actors[0].hp == trade.actors[1].hp and trade.actors[0].hp < 100, "Simultaneous attacks trade fairly")
    var retry = fresh()
    retry.cooldown = 1
    retry.actors[1].action = "hit"
    retry.actors[1].remaining = 12
    for i in range(13):retry.step()
    check(retry.is_attack(retry.actors[1].action), "Readiness survives busy state")
    print(JSON.stringify({"passed":failures.is_empty(),"failures":failures,"policies":report}))
    quit(0 if failures.is_empty() else 1)
