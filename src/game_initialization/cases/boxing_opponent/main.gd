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
var combat = preload("res://combat.gd").new()
var difficulty := "contender"

func _ready() -> void:
    rules = JSON.parse_string(FileAccess.get_file_as_string("res://rules.json"))
    composition = JSON.parse_string(FileAccess.get_file_as_string("res://composition.json"))
    var args := OS.get_cmdline_user_args()
    for i in range(args.size() - 1):
        if args[i] == "--scenario" and str(args[i+1]).begins_with("bout_seed_"):
            scenario_seed = int(str(args[i+1]).trim_prefix("bout_seed_"))
        elif args[i] == "--scenario":
            var parts = str(args[i+1]).split("_")
            if parts.size() == 4 and parts[0] == "bout":
                difficulty = parts[1]
                scenario_seed = int(parts[3])
    var manifest = JSON.parse_string(FileAccess.get_file_as_string("res://asset_manifest.json"))
    for clip in manifest.clips:
        var textures := []
        for file in manifest.clips[clip].files:
            textures.append(load("res://" + str(file)))
        clips[clip] = textures
    combat.reset(rules, difficulty, scenario_seed)
    actors = combat.actors
    label_font = ThemeDB.fallback_font
    queue_redraw()

func start_action(index: int, name: String) -> void:
    combat.start_action(index, name)

func _unhandled_input(event: InputEvent) -> void:
    if event is InputEventKey and event.pressed and not event.echo:
        var actions = {KEY_J: "jab", KEY_H: "hook", KEY_U: "uppercut", KEY_K: "block", KEY_D: "dodge_high", KEY_S: "dodge_low"}
        if actions.has(event.keycode):
            start_action(0, actions[event.keycode])

func _physics_process(_delta: float) -> void:
    combat.step()
    elapsed_ticks = combat.ticks
    match_over = combat.match_over
    outcome = combat.outcome
    event_log = combat.event_log
    queue_redraw()

func draw_actor(index: int) -> void:
    var a: Dictionary = actors[index]
    # Hook/uppercut currently reuse the H3 jab clip with explicit procedural tells.
    # Provenance records this; dedicated generated clips can replace the bindings.
    var clip := str(a.action)
    if clip in ["hook", "uppercut"]:
        clip = "jab"
    elif clip in ["dodge_high", "dodge_low"]:
        clip = "block"
    var frames: Array = clips[clip]
    var visual: Dictionary = composition.clips.get(clip, {})
    var hold := int(visual.get("hold_ticks", 0))
    var start := int(visual.get("start_frame", 0))
    var frame: int
    if a.action == "idle":
        frame = (int(a.age) / 2) % frames.size()
    else:
        frame = clampi(start + int(maxi(0, int(a.age) - hold) * frames.size() / maxi(1, int(a.duration) - hold)), 0, frames.size()-1)
    var texture: Texture2D = frames[frame]
    var size := Vector2(texture.get_width(), texture.get_height()) * float(composition.scale)
    var anchor: Array = composition.anchor
    var x := float(composition.actor_x[index])
    var y := float(composition.ground_y) + float(visual.get("offset_y", 0))
    var tell: bool = combat.phase(index) == "tell"
    var facing := 1.0 if index == 0 else -1.0
    var turn := 0.0
    var lean := 0.0
    var dip := 0.0
    if tell:
        var progress := clampf(float(a.age) / maxi(1, int(a.contact)), 0.0, 1.0)
        if a.action == "jab": lean = -12.0 * progress
        elif a.action == "hook": turn = -0.10 * progress
        elif a.action == "uppercut":
            dip = 25.0 * progress
            lean = 8.0 * progress
    if a.action == "dodge_high": dip = 65.0
    if a.action == "dodge_low": lean = -60.0
    # Pivot around the feet so shoulder turns and dips are visible.
    var stretch := Vector2(1.0, 1.0 - dip / 330.0)
    draw_set_transform(Vector2(x+facing*lean, y), turn*facing, Vector2(facing, stretch.y))
    draw_texture_rect(texture, Rect2(-float(anchor[0])*size.x, -float(anchor[1])*size.y, size.x, size.y), false)
    draw_set_transform(Vector2.ZERO)
    if tell:
        var cue := str(rules.attacks[a.action].tell)
        var high: bool = combat.target(index) == "high"
        var cue_color := Color("ffd166") if high else Color("62d9ef")
        var glove := Vector2(x + facing * (40.0 - 25.0 * float(a.age) / maxi(1, int(a.contact))), 375.0 if high else 460.0)
        draw_circle(glove, 18.0, cue_color)
        draw_line(Vector2(x, 385), glove, cue_color, 8.0)
        draw_string(label_font, Vector2(x-125, 265), cue, HORIZONTAL_ALIGNMENT_LEFT, -1, 18, cue_color)
        draw_string(label_font, Vector2(x-65, 292), "HIGH - DUCK" if high else "LOW - SLIP", HORIZONTAL_ALIGNMENT_LEFT, -1, 18, cue_color)
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
    draw_string(label_font,Vector2(100,680),"J: JAB  H: HOOK  U: UPPERCUT  K: BLOCK  D: DUCK HIGH  S: SLIP LOW",HORIZONTAL_ALIGNMENT_LEFT,-1,22,Color.WHITE)
    draw_string(label_font,Vector2(490,35),difficulty.to_upper(),HORIZONTAL_ALIGNMENT_LEFT,-1,22,Color.WHITE)
    draw_string(label_font,Vector2(565,190),"%.1f s" % (elapsed_ticks/60.0),HORIZONTAL_ALIGNMENT_LEFT,-1,24,Color.WHITE)
    if match_over:
        draw_rect(Rect2(390,280,500,110),Color(0.04,0.06,0.1,0.95))
        draw_string(label_font,Vector2(540,350),outcome.to_upper(),HORIZONTAL_ALIGNMENT_LEFT,-1,48,Color.WHITE)
