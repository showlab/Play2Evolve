"""Named seeded scenarios in the run's private project copy only."""
from pathlib import Path

OLD = '''            if args[i + 1] == "ready":
                state = "ready"'''
NEW = '''            if args[i + 1] == "ready" or args[i + 1].begins_with("ready_seed_"):
                state = "ready"
                if args[i + 1].begins_with("ready_seed_"):
                    plan.seed = int(args[i + 1].trim_prefix("ready_seed_"))'''


def install(project):
    path = Path(project) / 'main.gd'
    source = path.read_text()
    if NEW in source:
        return
    if source.count(OLD) != 1:
        raise ValueError('Cannot safely add seeded scenarios to this template')
    path.write_text(source.replace(OLD, NEW, 1))


def splits(train=8, valid=12, confirm=12, test=24):
    sizes = {'train': train, 'valid': valid, 'confirm': confirm, 'test': test}
    if any(type(n) is not int or not 1 <= n <= 100 for n in sizes.values()):
        raise ValueError('Each split needs 1–100 episodes')
    return {name: [(name, start + i) for i in range(sizes[name])]
            for name, start in [('train', 100), ('valid', 1000), ('confirm', 2000), ('test', 3000)]}
