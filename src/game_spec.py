"""Game spec for the code-as-world fighting game: every character/action-specific string lives here, never in engine.py.
gen_assets.py reads a spec module chosen with SPEC=<module> (default game_spec); race_spec.py is the racing one."""

import random

ASSETS = "assets"
CHARACTERS = {"A": {"ref": "assets/A_idle.png", "desc": "a male martial artist in a red gi, black belt"},
              "B": {"ref": "assets/B_idle.png", "desc": "a male martial artist in a blue gi, black belt"}}  # B: its own H3 clips, not a recolour of A
PAIR = ("A", "A")  # (left, right) fighter. The same character twice = the right one is mirrored and its red swapped for blue (the early
                   # stand-in: H3 paints red toenails and cuffs on the skin, which the swap turns blue). Set ("A", "B") once B's clips exist.
STAGE = "assets/stage.png"  # fixed side-view background
STAGE_PROMPT = ("Side view of a deserted traditional martial arts training hall at dusk, painted 2D game-art background. A flat wooden "
                "floor runs level across the entire width in the lower fifth of the image. The hall is completely empty. Wide horizontal "
                "composition, clean shapes, soft depth, muted warm colours.")  # describe only what is there: a video model reads "no people"
                # or "a red and a blue fighter in front of it" as people to draw (stage_v1: all 4 candidates had two fighters)
ACTIONS = {  # name: (prompt fragment, frames, damage, hit range in game units, stun frames)
    "idle": ("stands in fighting stance, breathing, slight bounce", 24, 0, 0, 0),
    "walk_f": ("walks forward with natural steps in fighting stance while the camera tracks alongside, so the character stays in the same place in the frame", 24, 0, 0, 0),
    "walk_b": ("walks backward with natural steps in fighting stance while the camera tracks alongside, so the character stays in the same place in the frame", 24, 0, 0, 0),
    "punch": ("throws a fast straight punch then returns to stance", 24, 10, 120, 12),
    "kick": ("throws a high roundhouse kick then returns to stance", 30, 15, 160, 18),
    "block": ("raises both forearms in front of the face and chest to block, then lowers them", 12, 0, 0, 0),
    "hit": ("takes a punch to the chest: an impact flash on the torso, the upper body jerks back, head snaps back, arms drop, then straightens up", 24, 0, 0, 0),
    "down": ("gets knocked down to the floor and lies still", 36, 0, 0, 0),
}
HP = 100
DECIDE = 9  # frames between the Player's decisions: short enough to see a 12-15 frame startup and block it (18 made defence impossible)
BLOCK_STUN = 10  # guard recoil: an attacker whose hit is blocked is stunned this long, so blocking earns a punish window
# (without it a block only halves damage and gives nothing back, defence is strictly dominated and every player degenerates to
#  striking first; with it attack / block / wait form a cycle a skill can reason about)
IDLE, HIT, DOWN, BLOCK = "idle", "hit", "down", "block"  # roles the engine needs, so engine.py never names an action
MOVES = {"walk_f": 4, "walk_b": -4}  # game units per frame, + = toward the opponent (the engine moves x; walk sprites step in place)
ONE_WAY = {DOWN}  # actions that do not return to idle: H3 gets only the first keyframe


# Scripted opponents (engine.scripted picks one by name): name -> (policy(me, op, dist) -> action, reaction: frames between its
# decisions), written from the spec numbers only. A ladder of styles so the Player skill has to generalise instead of beating one
# bot. Reaction matters more than style: a bot deciding every 18 frames like the Player never sees an attack coming (the hit lands
# at frame 12) and a 12-frame stun always covers its next tick, so it gets stun-locked by any first strike; a 6-frame reflex can
# block the startup and hit the recovery, which turns "punch first" from unbeatable into punishable.
# hit frame = frames // 2 (engine rule); an attack "reaches" when its range >= dist.
def _attacks(dist):  # attacks that reach at this distance, fastest first
    return sorted((a for a, v in ACTIONS.items() if v[2] > 0 and v[3] >= dist), key=lambda a: ACTIONS[a][1])


def _incoming(op, dist, reflex=1.0):  # the opponent's attack has not hit yet and will reach me, and I notice it (reflex = P(react))
    _, n, dmg, reach, _ = ACTIONS[op["action"]]
    return dmg > 0 and reach >= dist and op["frame_idx"] < n // 2 and random.random() < reflex


def _recovering(op):  # the opponent's attack already hit (or missed) and is still playing out: a free window
    _, n, dmg, _, _ = ACTIONS[op["action"]]
    return dmg > 0 and op["frame_idx"] >= n // 2


def brawler(me, op, dist):  # blocks what reaches it, otherwise attacks with anything that reaches or closes in
    if _incoming(op, dist, 0.5):
        return BLOCK
    hits = _attacks(dist)
    return random.choice(hits + [FORWARD]) if hits else FORWARD


def zoner(me, op, dist):  # keeps the distance where its longest attack reaches and the opponent's shortest does not
    if _incoming(op, dist, 0.5):
        return BLOCK
    if dist <= SHORT:  # too close: back off, sometimes strike instead (the engine clamps at the wall, so retreat can fail)
        return _attacks(dist)[0] if random.random() < 0.3 else BACK
    return _attacks(dist)[-1] if dist <= LONG else FORWARD  # its longest attack from the band, else approach


def punisher(me, op, dist):  # blocks incoming attacks and hits back the moment the opponent is recovering or stunned
    if _incoming(op, dist, 0.7):
        return BLOCK
    hits = _attacks(dist)
    if hits and (_recovering(op) or op["stun"] > 0):
        return hits[0]  # fastest strike into the open window
    if hits:
        return hits[0] if random.random() < 0.25 else IDLE  # mostly waits for the opponent to commit
    return FORWARD


_ranges = sorted(v[3] for v in ACTIONS.values() if v[2] > 0)
SHORT, LONG = _ranges[0], _ranges[-1]  # the opponent's shortest reach and the zoner's longest
FORWARD, BACK = max(MOVES, key=MOVES.get), min(MOVES, key=MOVES.get)
OPPONENTS = {"brawler": (brawler, 6), "zoner": (zoner, 9), "punisher": (punisher, 9)}  # the first one is the engine default
# Rules-only simulation (agent every 9 frames): punch spam wins 1% / 0% / 27% against these, a naive mix ~50%, and
# "block what is coming, hit the recovery" 46% / 100% / 100%, i.e. the ladder is beatable but not by striking first.

# asset generation (gen_assets.py)
REF_FIT = (0.40, 0.60)  # subject in the reference: x of its centre / height, as fractions of the square -> room in front for punches and kicks
REF_PROMPT = "Full-body side view of {desc}, standing in a fighting stance and facing right, whole body from head to feet visible with margin around it, centered. Flat pure chroma-key green (#00FF00) background, no shadow, no floor, no text. Even lighting, crisp edges, 2D fighting game character art."
SCENE = "2D fighting game character animation on a green screen background, side view, full body, the camera keeps the character centred in the frame"
IDENTITY = "face, hair, outfit, colors, proportions, facing right"  # what the judge compares with the reference
JUDGE_NOTE = ""  # extra judge rules for this game
