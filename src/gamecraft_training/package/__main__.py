import argparse
import os
import shutil
import json
from pathlib import Path
from codex_provider import add_arguments as add_codex_provider_arguments
from codex_provider import environment_values as codex_environment_values
from codex_provider import from_args as codex_provider_from_args
from .loop import run
from .codex_edit import DEFAULT_CODEX_MODEL


def main():
    p=argparse.ArgumentParser(description='One three-side training loop for any initialized game package')
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--game-package')
    source.add_argument('--continue-from-rsi',help='Completed RSI upgrade run (or cycle queue) to continue; inherit its selected game and skills')
    p.add_argument('--rsi-cycles',type=int,default=1,
                   help='Number of successive RSI upgrade cycles; --rounds still controls rounds within each cycle')
    p.add_argument('--output',required=True)
    p.add_argument('--rounds',type=int,default=None,
                   help='RSI: whole-game rounds; initialization: maximum optimization rounds per scene')
    p.add_argument('--profile',choices=['rsi','initialization'],default='rsi')
    p.add_argument('--initialization-max-frames',type=int,default=64,
                   help='Maximum chronological evidence frames for each initialization scene judge')
    p.add_argument('--initialization-scene',help='Run only this declared initialization scene; do not freeze a full game package')
    p.add_argument('--initialization-mode',choices=['per_scene','grouped','whole_game'],default='per_scene',
                   help='Per-scene rounds (default), grouped rounds, or one whole-game judge and repair per round')
    p.add_argument('--initialization-groups',help='JSON list of scenario groups for grouped initialization')
    p.add_argument('--initialization-playtest',choices=['fixed','program','agent','qwen27'],default='fixed',
                   help='Initialization playtest: original fixed trajectories, reactive program, '
                        'interactive configured agent, or interactive local Qwen3.8-27B')
    p.add_argument('--initialization-group-count',type=int,default=3,
                   help='Initialization program mode: Player plans this many profiles once (default: 3)')
    p.add_argument('--no-final-showcase',action='store_true',
                   help='Skip the separate final-game showcase; program mode enables it by default')
    p.add_argument('--initialization-behavior-skill',action='store_true',
                   help='In initialization program mode, reauthor the Player every round and evolve a game-local Behavior Skill')
    p.add_argument('--rsi-goal-mode', choices=['planned','legacy'], default='planned',
                   help='Plan one shared goal and continue between stages (default), or use legacy train fixtures/terminal stops')
    p.add_argument('--rsi-mode', choices=['upgrade','repair'], default='upgrade',
                   help='RSI: pursue one three-round game upgrade (default), or use the older per-round repair loop')
    p.add_argument('--rsi-upgrade-planning', choices=['legacy','evidence'], default='legacy',
                   help='Opt in to baseline audit, novelty review, and retryable RSI upgrade milestones')
    p.add_argument('--rsi-upgrade-execution', choices=['incremental','build_repair'],
                   default='incremental',
                   help='RSI upgrade: build the complete goal once, then playtest and repair it each round')
    p.add_argument('--max-decisions',type=int,default=40)
    p.add_argument('--step-frames',type=int,default=6)
    p.add_argument('--player-state',choices=['pixels','engine'],default='engine',
                   help='RSI candidate acceptance policies require engine; initialization also supports pixels')
    p.add_argument('--player-mode',choices=['interactive','program'],
                   default=os.getenv('GAMECRAFT_PLAYER_MODE','interactive'),
                   help='RSI Player: model decides each action, or authors one reactive program per round')
    p.add_argument('--player-backend',choices=['local','codex','api'],
                   default=os.getenv('GAMECRAFT_PLAYER_BACKEND','local'),
                   help='RSI acting Player backend (default: local)')
    p.add_argument('--player-model',default=os.getenv('GAMECRAFT_PLAYER_MODEL','local'),
                   help='RSI acting Player model; set explicitly for codex/api')
    p.add_argument('--godot',default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot'))
    p.add_argument('--asset-python',required=True)
    p.add_argument('--h3-backend', choices=['local','minimax','wan'],
                   default=os.getenv('H3_BACKEND','local'), help='Video generator backend')
    p.add_argument('--asset-parallelism',type=int,default=int(os.getenv('GAMECRAFT_ASSET_PARALLELISM','1')),
                   help='Maximum simultaneous H3 asset jobs; each job generates its candidates serially')
    p.add_argument('--asset-candidates',type=int,choices=range(1,5),default=4,
                   help='H3 candidates per repaired asset (default: 4)')
    p.add_argument('--video-skill',action='store_true',help='Use and evolve the optional video-generation skill; default keeps legacy prompt repair')
    p.add_argument('--video-skill-model',default='gpt-6-sol',help='Codex model that proposes H3-only Video Skill rules')
    p.add_argument('--global-video-skill',help='Optional global.json or video_skill directory used as the starting global layer')
    p.add_argument('--local-video-skill',help='Optional local.json or video_skill directory used as the starting game-local layer')
    p.add_argument('--global-video-skill-store',help='Initialization-only cross-game candidate store')
    p.add_argument('--initial-behavior-skill',help='Prior game-specific Behavior Skill file; interactive initialization Players use it as read-only guidance')
    p.add_argument('--composition-mode',choices=['parameters','codex'],default=None,
                   help='Default: Codex for RSI upgrade, parameters for legacy repair/initialization')
    p.add_argument('--composition-policy',help='JSON with protected project-relative paths; overrides package code_edit policy')
    p.add_argument('--codex-bin',default=os.getenv('CODEX_BIN','codex'))
    add_codex_provider_arguments(p)
    p.add_argument('--codex-model',default=DEFAULT_CODEX_MODEL)
    p.add_argument('--codex-timeout',type=int,default=None)
    p.add_argument('--codex-queue',help='Shared job directory; run codex_edit worker on the developer machine')
    p.add_argument('--judge-backend',choices=['local','codex','api'],
                   default=os.getenv('GAMECRAFT_JUDGE_BACKEND','local'))
    p.add_argument('--judge-model',default=os.getenv('GAMECRAFT_JUDGE_MODEL','local'))
    p.add_argument('--history-judge-rounds',type=int,default=8,
                   help='Prior full rounds supplied to the repair diagnosis Judge (default: 8; 0 disables)')
    p.add_argument('--history-legacy-planner-rounds',type=int,default=5,
                   help='Prior full rounds supplied to the legacy repair planner (default: 5)')
    p.add_argument('--history-planner-rounds',type=int,default=3,
                   help='Prior full rounds supplied to the unified boundary planner (default: 3)')
    p.add_argument('--history-editor-rounds',type=int,default=5,
                   help='Prior full rounds supplied to the Codex editor (default: 5)')
    p.add_argument('--history-skill-rounds',type=int,default=8,
                   help='Prior behavior summaries supplied to Skill generation (default: 8)')
    a=p.parse_args()
    history_windows={
        'judge':a.history_judge_rounds,
        'legacy_planner':a.history_legacy_planner_rounds,
        'planner':a.history_planner_rounds,
        'editor':a.history_editor_rounds,
        'skill':a.history_skill_rounds,
    }
    for name,value in history_windows.items():
        if value<0:p.error(f'--history-{name.replace("_","-")}-rounds must be nonnegative')
    if a.rsi_cycles<1:
        p.error('--rsi-cycles must be positive')
    if (a.rsi_cycles!=1 or a.continue_from_rsi) and (a.profile!='rsi' or a.rsi_mode!='upgrade'):
        p.error('RSI continuation/cycles require --profile rsi --rsi-mode upgrade')
    if a.continue_from_rsi and any((a.initial_behavior_skill,a.global_video_skill,a.local_video_skill)):
        p.error('--continue-from-rsi inherits skills; do not override starting skills')
    if a.rounds is None:
        a.rounds=3 if a.profile=='rsi' and a.rsi_mode=='upgrade' else 1
    if a.rounds < 1:
        p.error('--rounds must be positive')
    if a.codex_timeout is None:
        a.codex_timeout=3600 if a.profile=='rsi' and a.rsi_mode=='upgrade' else 900
    if a.composition_mode is None:
        a.composition_mode=('codex' if a.profile=='rsi' and a.rsi_mode=='upgrade'
                            else 'parameters')
    os.environ['H3_BACKEND']=a.h3_backend
    if not a.godot:p.error('Godot required')
    os.environ['LLM_BACKEND']=os.environ['OPTIMIZER_BACKEND']='local'
    if a.codex_timeout<1:p.error('Codex timeout must be positive')
    if a.composition_mode=='codex' and not a.codex_queue and not shutil.which(a.codex_bin):
        p.error('Codex CLI not found; configure --codex-bin or use --codex-queue with a developer-machine worker')
    if a.composition_mode!='codex' and (a.composition_policy or a.codex_queue):
        p.error('Composition policy/queue require --composition-mode codex')
    if a.judge_backend!='local' and a.judge_model=='local':
        p.error('Set an explicit --judge-model for api/codex Judge')
    if a.player_backend!='local' and a.player_model=='local':
        p.error('Set an explicit --player-model for api/codex Player')
    if (a.profile=='rsi' or a.initialization_playtest in ('program','agent')) and a.player_backend=='codex' and not shutil.which(a.codex_bin):
        p.error('Codex Player requires a local Codex CLI; configure --codex-bin')
    if a.initialization_behavior_skill and (a.profile!='initialization' or a.initialization_playtest!='program'):
        p.error('--initialization-behavior-skill requires --profile initialization --initialization-playtest program')
    if (a.profile=='initialization' and a.initial_behavior_skill and not a.initialization_behavior_skill
            and a.initialization_playtest not in ('agent','qwen27')):
        p.error('--initial-behavior-skill requires interactive initialization or --initialization-behavior-skill')
    if a.global_video_skill_store and (a.profile!='initialization' or not a.video_skill):
        p.error('--global-video-skill-store requires --profile initialization --video-skill')
    if a.initialization_playtest in ('program','agent','qwen27'):
        if a.profile!='initialization' or a.player_state!='engine':
            p.error('Interactive/program initialization playtests require --profile initialization --player-state engine')
        if a.initialization_playtest=='program' and (a.initialization_groups or a.initialization_scene or a.initialization_mode=='whole_game'):
            p.error('Program mode plans its own groups; do not specify groups, single scene or whole_game')
        if a.initialization_playtest in ('agent','qwen27') and a.initialization_mode=='whole_game':
            p.error('Interactive initialization playtests currently require per_scene or grouped mode')
        if a.initialization_playtest=='qwen27' and (a.player_backend!='local' or a.player_model!='local'):
            p.error('qwen27 mode requires --player-backend local --player-model local (the local service alias)')
    elif (a.initialization_mode=='grouped') != bool(a.initialization_groups):
        p.error('--initialization-mode grouped requires --initialization-groups, and vice versa')
    os.environ['CODEX_BIN']=a.codex_bin
    provider=codex_provider_from_args(a)
    os.environ.update(codex_environment_values(provider))
    os.environ['GAMECRAFT_VIDEO_SKILL_MODEL']=a.video_skill_model
    os.environ['GAMECRAFT_JUDGE_BACKEND']=a.judge_backend
    os.environ['GAMECRAFT_JUDGE_MODEL']=a.judge_model
    from .codex_edit import CodexEditor
    editor=CodexEditor(binary=a.codex_bin,model=a.codex_model,timeout=a.codex_timeout,
                       queue=a.codex_queue,provider=provider,godot=a.godot,
                       history_window=history_windows['editor']) if a.composition_mode=='codex' else None
    policy=json.loads(Path(a.composition_policy).read_text()) if a.composition_policy else None
    execute=run
    if a.rsi_cycles!=1 or a.continue_from_rsi:
        from functools import partial
        from .continuation import run_cycles
        execute=partial(run_cycles,cycles=a.rsi_cycles,continue_from=a.continue_from_rsi)
    r=execute(a.game_package,a.output,godot=a.godot,asset_python=a.asset_python,rounds=a.rounds,
          max_decisions=a.max_decisions,step_frames=a.step_frames,player_state=a.player_state,
          player_mode=a.player_mode,player_backend=a.player_backend,player_model=a.player_model,
          composition_mode=a.composition_mode,composition_policy=policy,code_editor=editor,
          video_skill=a.video_skill,judge_backend=a.judge_backend,judge_model=a.judge_model,
          profile=a.profile,initial_behavior_skill=a.initial_behavior_skill,rsi_goal_mode=a.rsi_goal_mode,
          rsi_mode=a.rsi_mode,
          rsi_upgrade_planning=a.rsi_upgrade_planning,
          rsi_upgrade_execution=a.rsi_upgrade_execution,
          global_video_skill=a.global_video_skill,local_video_skill=a.local_video_skill,
          global_video_skill_store=a.global_video_skill_store,asset_parallelism=a.asset_parallelism,
          asset_candidates=a.asset_candidates,
          initialization_max_frames=a.initialization_max_frames,initialization_scene=a.initialization_scene,
          initialization_mode=a.initialization_mode,
          initialization_playtest=a.initialization_playtest,
          initialization_behavior_skill=a.initialization_behavior_skill,
          history_windows=history_windows,
          final_showcase=not a.no_final_showcase,
          initialization_group_count=a.initialization_group_count,
          initialization_groups=(json.loads(Path(a.initialization_groups).read_text())
                                 if a.initialization_groups else None))
    if a.profile=='rsi' and r['errors']:raise SystemExit(2)


if __name__=='__main__':main()
