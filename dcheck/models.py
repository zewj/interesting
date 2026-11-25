"""Shared data types used by every dcheck module."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "available"
    TAKEN = "taken"
    INVALID = "invalid"  # fails Discord's username rules (checked locally or rejected by API)
    ERROR = "error"      # network failure, unexpected response, retries exhausted


@dataclass(frozen=True)
class Result:
    username: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d
