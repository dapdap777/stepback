from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import stepback
from fakes import FakeAPI
from stepback import config
from stepback.recorder.recorder import _reset_recorder_for_tests
from stepback.storage import Step, TraceStore


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own database, fresh settings and a fresh recorder."""
    db_path = tmp_path / ".stepback" / "traces.db"
    monkeypatch.delenv("STEPBACK_DISABLED", raising=False)
    monkeypatch.delenv("STEPBACK_DIR", raising=False)
    monkeypatch.delenv("STEPBACK_DB", raising=False)
    _reset_recorder_for_tests()
    config.settings = config.Settings()
    stepback.configure(db_path=db_path)
    yield db_path
    _reset_recorder_for_tests()
    config.settings = config.Settings()


@pytest.fixture
def db_path(isolated_settings: Path) -> Path:
    return isolated_settings


@pytest.fixture
def fake_api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def store(db_path: Path) -> Iterator[TraceStore]:
    s = TraceStore(db_path)
    yield s
    s.close()


@pytest.fixture
def recorded(db_path: Path) -> Callable[[], list[Step]]:
    """Flush the recorder and return every recorded step, oldest run first."""

    def read() -> list[Step]:
        assert stepback.flush(5)
        with TraceStore(db_path) as s:
            steps: list[Step] = []
            for summary in reversed(s.list_runs()):
                steps.extend(s.get_steps(summary.run.id))
            return steps

    return read
