extends RefCounted
func status(game: Node) -> Dictionary:
    var a: Dictionary = game.actors[0]
    var b: Dictionary = game.actors[1]
    return {"player_hp": a.hp, "opponent_hp": b.hp, "player_stamina": a.stamina, "opponent_stamina": b.stamina,
        "player_action": a.action, "opponent_action": b.action, "player_age": a.age, "opponent_age": b.age,
        "ticks": game.elapsed_ticks, "terminal": game.match_over, "outcome": game.outcome}
func terminal(value: Dictionary) -> bool:
    return value.terminal
func execute_event(game: Node, event: Dictionary) -> void:
    var key := InputEventKey.new()
    key.keycode = OS.find_keycode_from_string(str(event.keycode))
    key.pressed = true
    game._unhandled_input(key)
    key.pressed = false
    game._unhandled_input(key)
