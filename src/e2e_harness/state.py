"""実行状態の永続化。

オーケストレータは中断・再開できる必要がある。工程ごとの状態を
artifacts/<run-id>/state.json に落とし、再開時はそこから読み直す。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # 人のレビュー待ちで止まっている状態。
    AWAITING_REVIEW = "awaiting_review"
    # 手動モードで、人がプロンプトを実行するのを待っている状態。
    # 正常な進行であり、失敗ではない。
    AWAITING_MANUAL = "awaiting_manual"


@dataclass
class StageState:
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    output: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageState:
        return cls(
            status=StageStatus(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            output=data.get("output"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
        )


@dataclass
class RunState:
    run_id: str
    spec_document: str
    platform: str
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    stages: dict[str, StageState] = field(default_factory=dict)

    @property
    def directory_name(self) -> str:
        return self.run_id

    def stage(self, name: str) -> StageState:
        return self.stages.setdefault(name, StageState())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "spec_document": self.spec_document,
            "platform": self.platform,
            "created_at": self.created_at,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        return cls(
            run_id=str(data["run_id"]),
            spec_document=str(data["spec_document"]),
            platform=str(data.get("platform", "android")),
            created_at=str(data.get("created_at", "")),
            stages={
                str(k): StageState.from_dict(v)
                for k, v in data.get("stages", {}).items()
            },
        )


def run_dir(artifacts_dir: Path, run_id: str) -> Path:
    return artifacts_dir / run_id


def save(artifacts_dir: Path, state: RunState) -> Path:
    directory = run_dir(artifacts_dir, state.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILENAME
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load(artifacts_dir: Path, run_id: str) -> RunState:
    path = run_dir(artifacts_dir, run_id) / STATE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"実行状態が見つかりません: {path}")
    return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_runs(artifacts_dir: Path) -> list[str]:
    if not artifacts_dir.is_dir():
        return []
    runs = [
        d.name
        for d in artifacts_dir.iterdir()
        if d.is_dir() and (d / STATE_FILENAME).is_file()
    ]
    return sorted(runs, reverse=True)


def new_run_id(feature: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in feature)
    return f"{stamp}-{safe}"


def now() -> str:
    return datetime.now(UTC).isoformat()
