from __future__ import annotations

from . import ProcessingStep
from .nougat_step import NougatStep

_REGISTRY: dict[str, type[ProcessingStep]] = {
    "nougat": NougatStep,
}


def get_available_steps() -> dict[str, type[ProcessingStep]]:
    return dict(_REGISTRY)


def get_step(name: str) -> ProcessingStep:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown processing step: {name!r}. Available: {list(_REGISTRY)}"
        )
    return cls()
