extends SceneTree

func _initialize() -> void:
    call_deferred("check")

func check() -> void:
    var scene = load("res://Main.tscn").instantiate()
    root.add_child(scene)
    current_scene = scene
    var centers: Array = []
    for obstacle in scene.obstacles:
        centers.append(obstacle.center)
    print("SEED_CHECK=" + JSON.stringify({"seed": scene.plan.seed, "state": scene.state, "centers": centers}))
    quit(0)
