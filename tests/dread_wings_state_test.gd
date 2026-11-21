# Run inside an exported fixture project with --headless --script <this file>.
extends SceneTree

func _initialize() -> void:
    call_deferred("check_game")

func require(ok: bool, message: String) -> void:
    if not ok:
        push_error(message)
        quit(1)
        assert(ok, message)

func tap(game: Node) -> void:
    var event := InputEventKey.new()
    event.keycode = KEY_SPACE
    event.pressed = true
    game._unhandled_input(event)

func check_game() -> void:
    var game = load("res://Main.tscn").instantiate()
    root.add_child(game)
    game.demo_mode = true
    require(game.state == "title", "starts at title")
    tap(game)
    require(game.state == "ready", "title tap enters ready")
    tap(game)
    require(game.state == "playing" and game.velocity < 0, "first flap starts flight")
    game.bird_y = 360
    game.obstacles.clear()
    game.obstacles.append({"x": 150.0, "center": 360.0, "passed": false})
    game._physics_process(0)
    require(game.score == 1, "passing a pair increments score")
    game._physics_process(0)
    require(game.score == 1 and game.speed > 210, "pair counted once; difficulty progresses")
    game.obstacles.clear()
    game.obstacles.append({"x": 260.0, "center": 360.0, "passed": false})
    game.bird_y = 80
    game._physics_process(0)
    require(game.state == "dead", "hazard contact ends run")
    var x = game.obstacles[0].x
    game._physics_process(0.1)
    require(game.obstacles[0].x == x, "death freezes scrolling")
    tap(game)
    require(game.state == "ready" and game.score == 0 and game.obstacles.size() == 5, "retry resets without relaunch")
    print("DREAD_WINGS_STATE_TEST_OK")
    quit(0)
