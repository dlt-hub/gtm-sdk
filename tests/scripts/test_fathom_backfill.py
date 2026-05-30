# trunk-ignore-all(pyright/reportUnusedFunction): autouse pytest fixtures are invoked by name
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fathom_python import models as M

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "fathom-backfill-attio-meetings.py"
SAMPLE = REPO_ROOT / "api" / "samples" / "fathom.list_meetings.redacted.json"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "fathom_backfill_attio_meetings",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_meetings() -> list[M.Meeting]:
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return [M.Meeting.model_validate(item) for item in raw]


def _stub_iter(meetings: list[M.Meeting]):
    def _iter(**_kwargs):
        yield from meetings

    return _iter


def _full_scope(api_key: str | None = None) -> tuple[bool, set[str], str]:
    del api_key
    return (
        True,
        {"record_permission:read-write", "object_configuration:read-write"},
        "test-workspace",
    )


@pytest.fixture(autouse=True)
def _stub_attio_scope_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Attio scope preflight that --execute runs before dispatching.

    The backfill calls ``assert_attio_token_scopes`` before its execute loop;
    without a stub it would make a real GET /v2/self. Patching the
    module-internal ``fetch_token_scopes`` makes the preflight pass with no
    network (it is resolved from the preflight module globals at call time).
    """
    import libs.attio.preflight as _preflight

    _preflight.reset_scope_cache()
    monkeypatch.setenv("ATTIO_API_KEY", "stub-attio-key-for-tests")
    monkeypatch.setattr(_preflight, "fetch_token_scopes", _full_scope)


def test_dry_run_prints_ops_and_does_not_execute(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    monkeypatch.setattr(module, "iter_meetings", _stub_iter(_sample_meetings()))
    monkeypatch.setattr(module, "TMP_DIR", tmp_path)

    called = {"execute": 0}
    import src.attio.export as export_mod

    def _fail_execute(_plan):  # pragma: no cover - must not run in dry run
        called["execute"] += 1
        raise AssertionError("execute() must not be called in a dry run")

    monkeypatch.setattr(export_mod, "execute", _fail_execute)
    monkeypatch.setattr("sys.argv", ["prog"])

    rc = module.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert called["execute"] == 0
    assert "DRY RUN" in out
    # Meeting 111111 → upsert_meeting + summary note + action-items note.
    assert "ical_uid=dlt-mtg-" in out
    assert "Fathom summary — Sales Discovery" in out
    assert "processed=2" in out
    assert list(tmp_path.glob("fathom-backfill-*.md"))


def test_execute_dispatches_each_meeting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    monkeypatch.setattr(module, "iter_meetings", _stub_iter(_sample_meetings()))
    monkeypatch.setattr(module, "TMP_DIR", tmp_path)

    plans: list[list[Any]] = []
    import src.attio.export as export_mod

    def _record_execute(plan):
        plan = list(plan)
        plans.append(plan)
        return SimpleNamespace(success=True, fail_index=None, fail_reason=None)

    monkeypatch.setattr(export_mod, "execute", _record_execute)
    monkeypatch.setattr("sys.argv", ["prog", "--execute"])

    rc = module.main()

    assert rc == 0
    # One execute() call per meeting (2 in the sample).
    assert len(plans) == 2
    # First meeting carries meeting + 2 notes; second carries meeting only.
    assert [op.op_type for op in plans[0]][0] == "upsert_meeting"
    assert any(op.op_type == "upsert_note" for op in plans[0])


def test_no_notes_filters_note_ops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    monkeypatch.setattr(module, "iter_meetings", _stub_iter(_sample_meetings()))
    monkeypatch.setattr(module, "TMP_DIR", tmp_path)

    plans: list[list[Any]] = []
    import src.attio.export as export_mod

    def _record(plan: Any) -> SimpleNamespace:
        plans.append(list(plan))
        return SimpleNamespace(success=True, fail_index=None, fail_reason=None)

    monkeypatch.setattr(export_mod, "execute", _record)
    monkeypatch.setattr("sys.argv", ["prog", "--execute", "--no-notes"])

    module.main()

    all_ops = [op for plan in plans for op in plan]
    assert all_ops
    assert all(op.op_type == "upsert_meeting" for op in all_ops)
    # --no-notes drops the separate note ops but must NOT strip the summary from
    # the meeting description itself (recording 111111 carries a summary).
    first = next(op for op in all_ops if op.external_ref.ical_uid)
    assert "Went well." in first.description
