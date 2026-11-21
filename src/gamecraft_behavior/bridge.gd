extends Node
# Development adapter only. Added to a project COPY, never the submitted game.
var control_dir: String
var observe_only := false
var remaining := 0
var last_id := -1
var pending_id := -1
var busy := true
var game: Node
var replay_mode := false
var replay_tick := 0
var replay_limit := 0
var replay_events: Dictionary = {}
var adapter = preload("res://behavior_game_adapter.gd").new()
var replay_done := false
var stop_on_terminal := true
var manual_game_process := false
var native_capture := false
var capture_started := false
var held_mouse := false
var mouse_position := Vector2.ZERO

class ReplayInput extends Node:
    var bridge: Node
    func _physics_process(_delta: float) -> void:
        bridge.replay_input()

func _ready() -> void:
    process_mode = Node.PROCESS_MODE_ALWAYS
    process_physics_priority = 1000
    control_dir = OS.get_environment("BEHAVIOR_CONTROL_DIR")
    observe_only = OS.get_environment("BEHAVIOR_OBSERVE_ONLY") == "1"
    replay_mode = not OS.get_environment("BEHAVIOR_FRAME_TRACE").is_empty()
    native_capture = observe_only and OS.get_environment("BEHAVIOR_NATIVE_CAPTURE") == "1"
    call_deferred("initialize")

func initialize() -> void:
    game = get_tree().current_scene
    if game == null:
        push_error("Behavior bridge: no scene")
        get_tree().quit(2)
        return
    # Keep gameplay logic on the same clock in interactive rollout and replay.
    # Render FPS varies with software rendering and movie capture; the bridge's
    # physics clock is fixed, so advance the root game's _process from there.
    # Observe-only capture must leave the real-time game loop untouched.
    # Frame-stepped training owns _process explicitly, but OS-input recording
    # and replay should run the clean game's normal clock.
    if game.has_method("_process") and not observe_only:
        game.set_process(false)
        manual_game_process = true
    # The launcher supplies the task scenario; the adapter supplies engine semantics.
    if replay_mode:
        var data = JSON.parse_string(FileAccess.get_file_as_string(OS.get_environment("BEHAVIOR_FRAME_TRACE")))
        replay_limit = int(data.duration_frames) * 2
        stop_on_terminal = bool(data.get("stop_on_terminal", true))
        if replay_limit == 0:
            replay_done = true
            var initial := status()
            initial["physics_ticks"] = 0
            initial["stop_reason"] = "trace_end"
            write_status("replay-status.json", initial)
            get_tree().paused = true
            call_deferred("finish_replay")
            return
        for event in data.events:
            replay_events[int(event.frame) * 2] = event
        var driver := ReplayInput.new()
        driver.bridge = self
        driver.process_physics_priority = -1000
        add_child(driver)
    elif native_capture:
        # Hold the first game frame until the external recorder is capturing it.
        # A status update alone can precede the first visible X11 game frame.
        get_tree().paused = true
        await RenderingServer.frame_post_draw
        var image_path := control_dir.path_join("initial-frame.png")
        var save_error := get_viewport().get_texture().get_image().save_png(image_path)
        if save_error != OK:
            push_error("Behavior bridge: could not save first rendered frame")
            get_tree().quit(2)
            return
        write_status("render-ready.json", {"ready": true, "image": image_path})
    elif not observe_only:
        get_tree().paused = true
        await RenderingServer.frame_post_draw
        capture(-1)
    busy = false

func status() -> Dictionary:
    return adapter.status(game)

func replay_input() -> void:
    if busy or replay_done:
        return
    if replay_events.has(replay_tick):
        apply_event(replay_events[replay_tick])

func mouse_button(position: Vector2, pressed: bool) -> void:
    var event := InputEventMouseButton.new()
    event.position = position
    event.global_position = position
    event.button_index = MOUSE_BUTTON_LEFT
    event.pressed = pressed
    get_viewport().push_input(event, true)
    mouse_position = position

func mouse_motion(position: Vector2, pressed: bool) -> void:
    var motion := InputEventMouseMotion.new()
    motion.position = position
    motion.global_position = position
    motion.relative = position - mouse_position
    motion.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
    get_viewport().push_input(motion, true)
    mouse_position = position

func apply_event(event: Dictionary) -> void:
    var kind := str(event.get("type", ""))
    if kind == "key_down" or kind == "key_up":
        var key := InputEventKey.new()
        key.keycode = OS.find_keycode_from_string(str(event.keycode))
        key.pressed = kind == "key_down"
        Input.parse_input_event(key)
        Input.flush_buffered_events()
        return
    if kind != "mouse_click" and kind != "mouse_drag" and kind != "mouse_move" and kind != "mouse_down" and kind != "mouse_up":
        adapter.execute_event(game, event)
        return
    var start := Vector2(float(event.x), float(event.y))
    if kind == "mouse_down":
        mouse_button(start, true)
        held_mouse = true
        return
    if kind == "mouse_up":
        mouse_motion(start, held_mouse)
        mouse_button(start, false)
        held_mouse = false
        return
    var finish := start
    if kind == "mouse_drag":
        finish = Vector2(float(event.to_x), float(event.to_y))
    if kind == "mouse_click" or kind == "mouse_drag":
        mouse_button(start, true)
    if kind == "mouse_drag" or kind == "mouse_move":
        mouse_motion(finish, kind == "mouse_drag" or held_mouse)
    if kind == "mouse_click" or kind == "mouse_drag":
        mouse_button(finish, false)

func write_status(name: String, value: Dictionary) -> void:
    var tmp := control_dir.path_join(name + ".tmp")
    var file := FileAccess.open(tmp, FileAccess.WRITE)
    file.store_string(JSON.stringify(value))
    file.close()
    DirAccess.rename_absolute(tmp, control_dir.path_join(name))

func capture(id: int) -> void:
    var path := control_dir.path_join("frame_%04d.png" % (id + 1))
    get_viewport().get_texture().get_image().save_png(path)
    var value := status()
    value["id"] = id
    value["image"] = path
    write_status("observation.json", value)

func _process(_delta: float) -> void:
    if game == null or busy:
        return
    if replay_mode:
        return
    if observe_only:
        if native_capture and not capture_started:
            if not FileAccess.file_exists(control_dir.path_join("capture-start.json")):
                return
            capture_started = true
            get_tree().paused = false
            write_status("capture-running.json", {"ready": true})
        write_status("replay-status.json", status())
        return
    var path := control_dir.path_join("command.json")
    if not FileAccess.file_exists(path):
        return
    var command = JSON.parse_string(FileAccess.get_file_as_string(path))
    if not command is Dictionary or int(command.get("id", -1)) <= last_id:
        return
    last_id = int(command.id)
    pending_id = last_id
    remaining = int(command.frames) * 2 # 30 fps trace -> 60 Hz game physics.
    busy = true
    get_tree().paused = false
    for event in command.events:
        apply_event(event)

func _physics_process(_delta: float) -> void:
    if replay_mode:
        if busy or replay_done:
            return
        replay_tick += 1
        if manual_game_process:
            game.call("_process", 1.0 / 60.0)
        if (stop_on_terminal and adapter.terminal(status())) or replay_tick >= replay_limit:
            replay_done = true
            var value := status()
            value["physics_ticks"] = replay_tick
            value["stop_reason"] = ("death" if adapter.terminal(status()) else "budget") if stop_on_terminal else "trace_end"
            write_status("replay-status.json", value)
            get_tree().paused = true
            call_deferred("finish_replay")
        return
    if observe_only or not busy or remaining <= 0:
        return
    # Run after the scene's physics callback, then freeze before the next tick.
    if manual_game_process:
        game.call("_process", 1.0 / 60.0)
    remaining -= 1
    if remaining == 0:
        get_tree().paused = true
        call_deferred("finish_step")

func finish_replay() -> void:
    await RenderingServer.frame_post_draw
    get_tree().quit()

func finish_step() -> void:
    get_tree().paused = true
    await RenderingServer.frame_post_draw
    capture(pending_id)
    busy = false
