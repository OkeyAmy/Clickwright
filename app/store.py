"""Durable record of everything that ran.

Firestore when a project is configured, JSON on disk otherwise. Runs, approval
requests and benchmarks are the audit surface: what executed, why it was
allowed, what it cost, and which version of which connector did it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from app.connectors.models import ApprovalRequest, Benchmark, RunRecord

HOME = Path(os.getenv("CLICKWRIGHT_HOME", "var"))


class _JsonCollection:
    def __init__(self, directory: Path, model):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model = model

    def put(self, key: str, item):
        (self.dir / f"{key}.json").write_text(item.model_dump_json(indent=2))
        return item

    def get(self, key: str):
        f = self.dir / f"{key}.json"
        return self.model.model_validate_json(f.read_text()) if f.exists() else None

    def all(self, limit: Optional[int] = None):
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if limit:
            files = files[:limit]
        return [self.model.model_validate_json(f.read_text()) for f in files]


class Store:
    def __init__(self, home: Optional[Path] = None):
        base = home or HOME
        self.runs = _JsonCollection(base / "runs", RunRecord)
        self.approvals = _JsonCollection(base / "approvals", ApprovalRequest)
        self.benchmarks = _JsonCollection(base / "benchmarks", Benchmark)

    # convenience wrappers ------------------------------------------------

    def save_run(self, run: RunRecord) -> RunRecord:
        return self.runs.put(run.id, run)

    def recent_runs(self, limit: int = 50) -> list[RunRecord]:
        return self.runs.all(limit)

    def runs_for(self, connector_id: str, limit: int = 50) -> list[RunRecord]:
        return [r for r in self.runs.all() if r.connector_id == connector_id][:limit]

    def save_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        return self.approvals.put(request.id, request)

    def pending_approvals(self) -> list[ApprovalRequest]:
        return [a for a in self.approvals.all() if a.status == "pending"]

    def save_benchmark(self, benchmark: Benchmark) -> Benchmark:
        return self.benchmarks.put(benchmark.connector_id, benchmark)

    def benchmark(self, connector_id: str) -> Optional[Benchmark]:
        return self.benchmarks.get(connector_id)
