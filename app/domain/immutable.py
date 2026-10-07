"""Recursively detach and freeze JSON-like compiled configuration values."""
from dataclasses import fields, is_dataclass, replace
from collections.abc import Mapping


class FrozenDict(dict):
    """Read-only dict retaining JSON and existing mapping consumer support."""

    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        instance = dict.__new__(cls)
        dict.update(instance, {
            key: freeze_configuration(value)
            for key, value in dict(*args, **kwargs).items()
        })
        return instance

    def __init__(self, *args, **kwargs):
        # Initialization takes place once in __new__; an explicit later
        # __init__ call must not reopen the mutation path.
        pass

    def _readonly(self, *args, **kwargs):
        raise TypeError("Compiled configuration is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (FrozenDict, (dict(self),))


def freeze_configuration(value):
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (tuple, list)):
        return tuple(freeze_configuration(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_configuration(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{
            item.name: freeze_configuration(getattr(value, item.name))
            for item in fields(value) if item.init
        })
    return value
