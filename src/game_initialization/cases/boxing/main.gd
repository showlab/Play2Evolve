extends Node2D
# Initial research environment: fixed combat rules, replaceable video clips.
# Not a full Sports Boxing Gym benchmark implementation.
var rules: Dictionary
var composition: Dictionary
var clips := {}
var actors := []
var elapsed_ticks := 0
var match_over := false
var outcome := "playing"
var scenario_seed := 100
var event_log := []
var label_font: Font

func _ready() -> void:
    rules = JSON.parse_string(FileAccess.get_file_as_string("res://rules.json"))
    composition = JSON.parse_string(FileAccess.get_file_as_string("res://composition.json"))
    var args := OS.get_cmdline_user_args()
    for i in range(args.size() - 1):
        if args[i] == "--scenario" and str(args[i+1]).begins_with("bout_seed_"):
            scenario_seed = int(str(args[i+1]).trim_prefix("bout_seed_"))
    var manifest = JSON.parse_string(FileAccess.get_file_as_string("res://asset_manifest.json"))
    for clip in manifest.clips:
        var textures := []
        for file in manifest.clips[clip].files:
            textures.append(load("res://" + str(file)))
        clips[clip] = textures
    for _i in range(2):
        actors.append({"hp": 100, "stamina": 100.0, "action": "idle", "age": 0, "remaining": 0, "landed": false})
    label_font = ThemeDB.fallback_font
    queue_redraw()

func start_action(index: int, name: String) -> void:
    var a: Dictionary = actors[index]
    if match_over or a.remaining > 0:
        return
    var cost := float(rules.costs.get(name, 0))
    if a.stamina < cost:
        return
    a.stamina -= cost
    a.action = name
    a.age = 0
    a.remaining = int(rules.durations[name])
    a.landed = false

func _unhandled_input(event: InputEvent) -> void:
    if event is InputEventKey and event.pressed and not event.echo:
        if event.keycode == KEY_J:
            start_action(0, "jab")
        elif event.keycode == KEY_K:
            start_action(0, "block")

func hit(attacker: int) -> void:
    var defender := 1 - attacker
    var a: Dictionary = actors[attacker]
    var d: Dictionary = actors[defender]
    if a.landed:
        return
    a.landed = true
    var blocked: bool = d.action == "block" and d.remaining > 0
    event_log.append({"tick": elapsed_ticks, "attacker": attacker, "blocked": blocked})
    if blocked:
        a.remaining += int(rules.block_recoil_ticks)
    else:
        d.hp = maxi(0, int(d.hp) - int(rules.damage))
        d.action = "hit"
        d.age = 0
        d.remaining = int(rules.durations.hit)
        if d.hp == 0:
            match_over = true
            outcome = "win" if attacker == 0 else "loss"

func _physics_process(_delta: float) -> void:
    if match_over:
        return
    elapsed_ticks += 1
    # A fixed, seed-dependent opponent schedule. Future schedule is private.
    if (elapsed_ticks + scenario_seed % 31) % int(rules.opponent_interval_ticks) == 0:
        start_action(1, "jab")
    for i in range(2):
        var a: Dictionary = actors[i]
        a.stamina = minf(100.0, a.stamina + float(rules.regen_per_tick))
        if a.remaining > 0:
            a.age += 1
            a.remaining -= 1
            if a.action == "jab" and a.age == int(rules.contact_tick):
                hit(i)
            if a.remaining == 0:
                a.action = "idle"
                a.age = 0
        else:
            a.age += 1
    if not match_over and elapsed_ticks >= int(rules.bout_ticks):
        match_over = true
        outcome = "win" if actors[0].hp > actors[1].hp else ("loss" if actors[0].hp < actors[1].hp else "draw")
    queue_redraw()

func draw_actor(index: int) -> void:
    var a: Dictionary = actors[index]
    var frames: Array = clips[a.action]
    var visual: Dictionary = composition.clips.get(a.action, {})
    var hold := int(visual.get("hold_ticks", 0))
    var start := int(visual.get("start_frame", 0))
    var frame: int
    if a.action == "idle":
        frame = (int(a.age) / 2) % frames.size()
    else:
        frame = clampi(start + int(maxi(0, int(a.age) - hold) * frames.size() / maxi(1, int(rules.durations[a.action]) - hold)), 0, frames.size()-1)
    var texture: Texture2D = frames[frame]
    var size := Vector2(texture.get_width(), texture.get_height()) * float(composition.scale)
    var anchor: Array = composition.anchor
    var x := float(composition.actor_x[index])
    var y := float(composition.ground_y) + float(visual.get("offset_y", 0))
    if index == 0:
        draw_texture_rect(texture, Rect2(x-float(anchor[0])*size.x, y-float(anchor[1])*size.y, size.x, size.y), false)
    else:
        draw_set_transform(Vector2(x, y), 0.0, Vector2(-1, 1))
        draw_texture_rect(texture, Rect2(-float(anchor[0])*size.x, -float(anchor[1])*size.y, size.x, size.y), false)
        draw_set_transform(Vector2.ZERO)
    draw_string(label_font, Vector2(x-65, 230), str(a.action).to_upper(), HORIZONTAL_ALIGNMENT_LEFT, -1, 22, Color.WHITE)

func _draw() -> void:
    if actors.is_empty():
        return
    draw_rect(Rect2(0,0,1280,720), Color("111b2b"))
    draw_rect(Rect2(80,600,1120,90), Color("3b485c"))
    for y in [350,430,510]:
        draw_line(Vector2(80,y),Vector2(1200,y),Color("5c6b80"),4)
    for x in [80,1200]:
        draw_rect(Rect2(x-8,270,16,350),Color("72849a"))
    for i in range(2):
        var x := 80 if i == 0 else 740
        var color := Color("e95b5b") if i == 0 else Color("57b6e8")
        draw_string(label_font, Vector2(x,58), "PLAYER" if i == 0 else "OPPONENT", HORIZONTAL_ALIGNMENT_LEFT,-1,26,color)
        draw_rect(Rect2(x,76,460,24), Color("303b4c"))
        draw_rect(Rect2(x,76,float(actors[i].hp)*4.6,24),color)
        draw_rect(Rect2(x,112,float(actors[i].stamina)*4.6,10),Color("e7bf67"))
        draw_string(label_font,Vector2(x,151),"HP %d   STAMINA %d" % [actors[i].hp,actors[i].stamina],HORIZONTAL_ALIGNMENT_LEFT,-1,20,Color.WHITE)
        draw_actor(i)
    draw_string(label_font,Vector2(390,680),"J: JAB    K: BLOCK    WAIT: RECOVER",HORIZONTAL_ALIGNMENT_LEFT,-1,22,Color.WHITE)
    draw_string(label_font,Vector2(565,190),"%.1f s" % (elapsed_ticks/60.0),HORIZONTAL_ALIGNMENT_LEFT,-1,24,Color.WHITE)
    if match_over:
        draw_rect(Rect2(390,280,500,110),Color(0.04,0.06,0.1,0.95))
        draw_string(label_font,Vector2(540,350),outcome.to_upper(),HORIZONTAL_ALIGNMENT_LEFT,-1,48,Color.WHITE)
