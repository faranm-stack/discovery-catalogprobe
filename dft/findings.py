"""Result records shared by static and live checks."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ("error", "warning", "info")

# Failure taxonomy from the working plan: separates "the agent is broken"
# from "the platform is broken".
CATEGORIES = (
    "definition",     # the agent/tool definition itself is wrong
    "deploy",         # deployment failed / agent not found
    "platform",       # infra, auth, permissions, storage
    "capability",     # agent says it can't do the task, or empty answer
    "tool-not-used",  # answered without running its tool
    "tool-error",     # tool ran but returned an error
    "wrong-output",   # ran, but output misses the expected content
    "timeout",        # no terminal answer in time
)


@dataclass
class Finding:
    check: str
    severity: str
    category: str
    subject: str  # e.g. "agents/cantera" or "starter-kits/physics-cfd-sim#sp-1"
    message: str
    path: str = ""
    line: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"bad severity {self.severity!r}")
        if self.category not in CATEGORIES:
            raise ValueError(f"bad category {self.category!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def log_event(event: str, **fields: Any) -> None:
    """One structured JSON line per event on stderr. Never pass secrets here."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)
