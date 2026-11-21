extends RefCounted
func status(game: Node) -> Dictionary:
    return {"points": game.points, "ticks": game.ticks, "terminal": game.finished, "private_opponent_plan": "hidden"}
func terminal(value: Dictionary) -> bool:
    return value.terminal
func execute_event(game: Node, event: Dictionary) -> void:
    var key := InputEventKey.new()
    key.keycode = OS.find_keycode_from_string(str(event.keycode))
    key.pressed = true
    game._unhandled_input(key)
    key.pressed = false
    game._unhandled_input(key)
