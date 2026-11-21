"""Explicit adapter selection; unsupported tasks never silently use bird rules."""
import json
from pathlib import Path
from .base import GameAdapter
from .dread_wings import DreadWingsAdapter


def resolve(project=None, adapter=None):
    if adapter is None:
        task = (json.loads((Path(project) / 'game_plan.json').read_text())['task_id']
                if project is not None else 'platformer-dread-wings')
        if task != 'platformer-dread-wings':
            raise ValueError(f'No built-in Agent adapter for {task}; supply adapter= explicitly')
        adapter = DreadWingsAdapter()
    if not isinstance(adapter, GameAdapter):
        raise TypeError('adapter must implement the GameAdapter contract')
    if project is not None:
        task = json.loads((Path(project) / 'game_plan.json').read_text())['task_id']
        if task != adapter.task_id:
            raise ValueError(f'Project task {task} does not match adapter {adapter.task_id}')
    return adapter


def load_factory(name):
    """Load an explicitly selected, trusted Python adapter factory for the CLI."""
    import importlib
    module, sep, factory = name.partition(':')
    if not sep or not module or not factory:
        raise ValueError('Expected --adapter package.module:factory')
    return resolve(adapter=getattr(importlib.import_module(module), factory)())
