"""Live runner: send each test case to a deployed agent and check the answer.

Invocation reuses PR #100's ``DiscoveryAgentClient`` (utilities/agent-evaluation/
evaluators/discovery_client.py) rather than keeping a second data-plane client.
Point ``--client-dir`` at that folder in any microsoft/discovery checkout.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .findings import log_event
from .live_checks import CaseResult, evaluate
from .plan import TestCase


class AgentClient(Protocol):
    async def create_investigation(self, project: str, investigation: str, **kw: Any) -> Any: ...
    async def invoke(self, project: str, investigation: str, agent: str, query: str, **kw: Any
                     ) -> tuple[dict, str]: ...
    async def aclose(self) -> None: ...


def load_pr100_client(client_dir: str | Path, endpoint: str) -> AgentClient:
    client_dir = Path(client_dir).resolve()
    if not (client_dir / "discovery_client.py").exists():
        raise FileNotFoundError(
            f"{client_dir} has no discovery_client.py. Point --client-dir at "
            "utilities/agent-evaluation/evaluators from microsoft/discovery (PR #100).")
    if str(client_dir) not in sys.path:
        sys.path.insert(0, str(client_dir))
    module = importlib.import_module("discovery_client")
    return module.DiscoveryAgentClient(endpoint)


def load_agent_map(path: str | Path | None) -> dict[str, str]:
    """``catalog-name = deployed-name`` lines; '#' comments allowed."""
    if not path:
        return {}
    mapping = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            mapping[k] = v
    return mapping


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


async def run_live(plan: list[TestCase], client: AgentClient, *, project: str, out_dir: Path,
                   run_id: str | None = None, agent_map: dict[str, str] | None = None,
                   concurrency: int = 2, timeout_s: int = 900) -> list[CaseResult]:
    run_id = run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    investigation = f"dft-{run_id}"
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    agent_map = agent_map or {}

    try:
        await client.create_investigation(project, investigation)
    except (Exception, SystemExit) as exc:  # PR #100 client raises SystemExit on HTTP errors
        await client.aclose()
        raise RuntimeError(f"could not create investigation {investigation} in project {project}: "
                           f"{str(exc)[:300]}") from None
    log_event("run.start", run_id=run_id, project=project, investigation=investigation, cases=len(plan))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(case: TestCase) -> CaseResult:
        deployed = agent_map.get(case.agent, case.agent)
        async with sem:
            log_event("case.start", run_id=run_id, case=case.id, agent=deployed, origin=case.origin)
            t0 = time.monotonic()
            response, conv, error = None, "", None
            try:
                response, conv = await client.invoke(project, investigation, deployed, case.prompt,
                                                     timeout=timeout_s, label=case.id)
            except (Exception, SystemExit) as exc:  # PR #100 client raises SystemExit on poll timeout
                error = exc
            latency = round(time.monotonic() - t0, 1)
            if response is not None:
                (raw_dir / f"{_safe(case.id)}.json").write_text(json.dumps(response, indent=2, default=str),
                                                                encoding="utf-8")
            result = evaluate(case, response, error=error, latency_s=latency, conversation=conv)
            log_event("case.end", run_id=run_id, case=case.id, status=result.status, latency_s=latency,
                      conversation=conv, categories=sorted({f["category"] for f in result.findings}))
            return result

    try:
        results = await asyncio.gather(*(one(c) for c in plan))
    finally:
        await client.aclose()
    log_event("run.end", run_id=run_id, passed=sum(r.status == "PASS" for r in results), total=len(results))
    return list(results)
