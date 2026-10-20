"""Every public callable is fully annotated, and its annotations resolve."""

from __future__ import annotations

import importlib
import inspect
import typing

import pytest

from tests.test_api_surface import _public_modules


def _callables() -> typing.List[typing.Tuple[str, typing.Callable[..., object]]]:
    """Each ``__all__`` function and public method of each ``__all__`` class
    that pkgforge defines (aliases and constants are skipped)."""
    found = []
    seen = set()
    for name in _public_modules():
        module = importlib.import_module(name)
        for attr in getattr(module, "__all__", ()):
            obj = getattr(module, attr)
            if not str(getattr(obj, "__module__", "")).startswith("pkgforge"):
                continue
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if inspect.isfunction(obj):
                found.append((f"{name}.{attr}", obj))
            elif inspect.isclass(obj) and not hasattr(obj, "__required_keys__"):
                for member, value in vars(obj).items():
                    if member.startswith("_") and member != "__init__":
                        continue
                    func = getattr(value, "__func__", value)
                    if isinstance(value, property):
                        func = value.fget
                    if inspect.isfunction(func) and func.__module__.startswith(
                        "pkgforge"
                    ):
                        found.append((f"{name}.{attr}.{member}", func))
    return found


@pytest.mark.parametrize(
    "func", [func for _, func in _callables()], ids=[name for name, _ in _callables()]
)
def test_public_callable_is_fully_annotated(func):
    hints = typing.get_type_hints(func)
    params = [
        name
        for name in inspect.signature(func).parameters
        if name not in ("self", "cls")
    ]
    missing = [name for name in params + ["return"] if name not in hints]
    assert not missing, f"{func.__qualname__} lacks annotations for {missing}"


def test_the_walk_finds_the_public_surface():
    names = {name for name, _ in _callables()}
    assert "pkgforge.Project.install" in names
    assert "pkgforge.open_db" in names
