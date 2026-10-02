"""Structural DSL validation; field-dependent checks live in the compiler."""
from typing import Any
from pydantic import BaseModel, ConfigDict, StrictInt


class SegmentValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    root: dict[str, Any]


def fail(path, message):
    raise SegmentValidationError([{"path": path, "message": message}])