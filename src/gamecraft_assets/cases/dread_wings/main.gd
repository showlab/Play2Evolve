extends Node2D

var plan: Dictionary
var cfg: Dictionary
var state := "title"
var bird_y := 360.0
var velocity := 0.0
var score := 0
var best := 0
var speed := 210.0
var elapsed := 0.0
var distance := 0.0
var obstacles: Array[Dictionary] = []
var rng := RandomNumberGenerator.new()
var demo_mode := false
var bird: AnimatedSprite2D
var sky: Texture2D
var pipe: Texture2D
var title: Label
var info: Label
var points: Label
var high_score: Label

func _ready() -> void:
    plan = JSON.parse_string(FileAccess.get_file_as_string("res://game_plan.json"))
    cfg = plan.mechanics
    sky = load("res://assets/skyline/frame0000.png")
    pipe = load("res://assets/pipe/frame0000.png")
    bird = AnimatedSprite2D.new()
    bird.sprite_frames = load("res://assets/bird/animations.tres")
    bird.scale = Vector2(float(cfg.player_size[0]) / 192.0, float(cfg.player_size[1]) / 192.0)
    bird.play("fly")
    add_child(bird)
    title = make_label(46, Vector2(0, 160), Vector2(1280, 80))
    info = make_label(23, Vector2(0, 470), Vector2(1280, 100))
    points = make_label(34, Vector2(0, 30), Vector2(1280, 55))
    high_score = make_label(19, Vector2(930, 30), Vector2(320, 40))
    var args := OS.get_cmdline_user_args()
    for i in range(args.size()):
        if args[i] == "--scenario" and i + 1 < args.size():
            demo_mode = true
            if args[i + 1] == "ready":
                state = "ready"
    if not demo_mode and FileAccess.file_exists("user://dread_wings_best.save"):
        best = int(FileAccess.get_file_as_string("user://dread_wings_best.save"))
    reset_world()
    update_ui()

func make_label(font_size: int, pos: Vector2, extent: Vector2) -> Label:
    var label := Label.new()
    label.position = pos
    label.size = extent
    label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
    label.add_theme_font_size_override("font_size", font_size)
    label.add_theme_color_override("font_color", Color(0.95, 0.92, 0.9))
    label.add_theme_color_override("font_outline_color", Color(0.05, 0.02, 0.04))
    label.add_theme_constant_override("outline_size", 5)
    add_child(label)
    return label

func reset_world() -> void:
    rng.seed = int(plan.seed) if demo_mode else int(Time.get_ticks_usec())
    bird_y = float(cfg.player_position[1])
    velocity = 0.0
    score = 0
    elapsed = 0.0
    distance = 0.0
    speed = float(cfg.initial_scroll_speed)
    obstacles.clear()
    for i in range(5):
        add_obstacle(1000.0 + i * float(cfg.obstacle_spacing))
    bird.play("fly")

func add_obstacle(x: float) -> void:
    obstacles.append({"x": x, "center": rng.randf_range(235.0, 485.0), "passed": false})

func _unhandled_input(event: InputEvent) -> void:
    var tap: bool = event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_SPACE
    tap = tap or (event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT)
    if not tap:
        return
    if state == "title" or state == "dead":
        reset_world()
        state = "ready"
    else:
        state = "playing"
        velocity = float(cfg.flap_velocity)
    update_ui()

func _physics_process(delta: float) -> void:
    if state == "playing":
        elapsed += delta
        speed = minf(float(cfg.maximum_scroll_speed), float(cfg.initial_scroll_speed) + score * float(cfg.speed_gain_per_point))
        distance += speed * delta
        velocity += float(cfg.gravity) * delta
        bird_y += velocity * delta
        for o in obstacles:
            o.x -= speed * delta
        var body := Rect2(float(cfg.player_position[0]) - 23, bird_y - 18, 46, 36)
        if body.position.y <= 0 or body.end.y >= 680:
            die()
        for o in obstacles:
            var half_gap := float(cfg.gap_size) * 0.5
            var top := Rect2(float(o.x), 0, float(cfg.obstacle_width), float(o.center) - half_gap)
            var bottom := Rect2(float(o.x), float(o.center) + half_gap, float(cfg.obstacle_width), 720.0 - float(o.center) - half_gap)
            if state == "playing" and (body.intersects(top) or body.intersects(bottom)):
                die()
            if state == "playing" and not o.passed and float(o.x) + float(cfg.obstacle_width) < body.position.x:
                o.passed = true
                score += 1
        if obstacles[0].x < -float(cfg.obstacle_width):
            obstacles.pop_front()
            add_obstacle(float(obstacles[-1].x) + float(cfg.obstacle_spacing))
        update_ui()
    bird.position = Vector2(float(cfg.player_position[0]), bird_y)
    bird.rotation = clampf(velocity / 1100.0, -0.35, 0.6) if state == "playing" else 0.0
    queue_redraw()

func die() -> void:
    state = "dead"
    bird.pause()
    if score > best:
        best = score
        if not demo_mode:
            var save := FileAccess.open("user://dread_wings_best.save", FileAccess.WRITE)
            if save:
                save.store_string(str(best))

func update_ui() -> void:
    points.text = str(score) if state == "playing" else ""
    high_score.text = "BEST  %d" % best
    title.visible = state != "playing"
    info.visible = state != "playing"
    if state == "title":
        title.text = "DREAD WINGS"
        info.text = "A fragile bird. An unforgiving sky.\nSPACE / CLICK TO BEGIN"
    elif state == "ready":
        title.text = "READY"
        info.text = "TAP TO FLAP\nAvoid the columns. Keep flying."
    elif state == "dead":
        title.text = "SIGNAL LOST"
        info.text = "SCORE  %d    /    BEST  %d\nSPACE / CLICK TO RETRY" % [score, best]

func _draw() -> void:
    if sky == null:
        return
    draw_texture_rect(sky, Rect2(0, 0, 1280, 720), false)
    # Procedural distant silhouettes scroll independently over the generated sky.
    for i in range(18):
        var x := fposmod(i * 90.0 - distance * 0.12, 1620.0) - 170.0
        draw_rect(Rect2(x, 580 + (i % 4) * 14, 62, 160), Color(0.025, 0.035, 0.055, 0.65))
    if state != "title":
        for o in obstacles:
            var gap := float(cfg.gap_size) * 0.5
            var top_height := float(o.center) - gap
            var bottom_y := float(o.center) + gap
            draw_texture_rect(pipe, Rect2(float(o.x), 0, float(cfg.obstacle_width), top_height), false)
            draw_texture_rect(pipe, Rect2(float(o.x), bottom_y, float(cfg.obstacle_width), 720 - bottom_y), false)
            draw_rect(Rect2(float(o.x), top_height - 6, float(cfg.obstacle_width), 6), Color(0.05, 0.8, 0.85))
            draw_rect(Rect2(float(o.x), bottom_y, float(cfg.obstacle_width), 6), Color(0.05, 0.8, 0.85))
    draw_rect(Rect2(0, 680, 1280, 40), Color(0.055, 0.045, 0.07))
    draw_line(Vector2(0, 680), Vector2(1280, 680), Color(0.7, 0.13, 0.19), 3)
