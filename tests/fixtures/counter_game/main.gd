extends Node2D
# Deterministic interface fixture, not a boxing game or benchmark implementation.
var points := 0
var ticks := 0
var finished := false

func _unhandled_input(event: InputEvent) -> void:
    if finished or not event is InputEventKey or not event.pressed:
        return
    if event.keycode == KEY_J:
        points += 2
    elif event.keycode == KEY_L:
        points -= 1
    finished = points >= 3
    queue_redraw()

func _physics_process(_delta: float) -> void:
    if not finished:
        ticks += 1

func _draw() -> void:
    draw_rect(Rect2(100, 100, 100 + points * 20, 80), Color.CORNFLOWER_BLUE)
