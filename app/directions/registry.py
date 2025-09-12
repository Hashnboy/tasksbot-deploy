"""Реестр направлений."""
from importlib import import_module
from typing import Dict, Iterable, Optional

from .base import Direction

_registry: Dict[str, Direction] = {}


def load_all() -> None:
    """Импортировать и зарегистрировать все встроенные направления."""
    modules = ["tobacco", "coffee", "wb", "personal"]
    for mod_name in modules:
        mod = import_module(f".{{}}".format(mod_name), package=__name__)
        direction: Direction = getattr(mod, "direction")  # each module exposes 'direction'
        _registry[direction.key] = direction


def get(key: str) -> Optional[Direction]:
    return _registry.get(key)


def all_directions() -> Iterable[Direction]:
    return _registry.values()
