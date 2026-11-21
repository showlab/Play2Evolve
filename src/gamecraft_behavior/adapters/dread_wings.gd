extends RefCounted

func status(game: Node) -> Dictionary:
    if game.has_method("status_snapshot"):
        return game.status_snapshot()
    return {"state": str(game.state), "score": int(game.score), "survival_seconds": float(game.elapsed), "bird_y": float(game.bird_y), "velocity": float(game.velocity)}

func terminal(value: Dictionary) -> bool:
    return value.state == "dead"

func execute_event(game: Node, event: Dictionary) -> void:
    # This game's handler processes events synchronously on the physics clock.
    var down := InputEventKey.new()
    down.keycode = OS.find_keycode_from_string(str(event.keycode))
    down.pressed = true
    game._unhandled_input(down)
    var up := InputEventKey.new()
    up.keycode = down.keycode
    up.pressed = false
    game._unhandled_input(up)
