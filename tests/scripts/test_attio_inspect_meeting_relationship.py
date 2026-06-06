"""Unit tests for scripts/attio-inspect-meeting-relationship.py pure logic.

Covers the config verdict and the linked-record set helpers without standing up
a live Attio workspace (the live reads/writes are prod-only and guarded behind
``--execute``). BD: ai-3hq.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "attio-inspect-meeting-relationship.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "attio_inspect_meeting_relationship",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses with `from __future__ import annotations`
    # resolve string annotations via sys.modules[cls.__module__] during
    # @dataclass processing, which fails if the module isn't registered.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_script_module()
InverseAttr = _MOD.InverseAttr
evaluate_inverse_multiselect = _MOD.evaluate_inverse_multiselect


def _attr(target: str, multiselect: bool) -> object:
    return InverseAttr(
        target_object=target,
        api_slug="meetings",
        is_multiselect=multiselect,
    )


def test_both_multiselect_true_passes() -> None:
    verdict = evaluate_inverse_multiselect(
        [_attr("people", True)],
        [_attr("companies", True)],
    )
    assert verdict.status == "pass"


def test_people_single_valued_stops() -> None:
    verdict = evaluate_inverse_multiselect(
        [_attr("people", False)],
        [_attr("companies", True)],
    )
    assert verdict.status == "stop"
    assert any("people.meetings" in r for r in verdict.reasons)


def test_companies_single_valued_stops() -> None:
    verdict = evaluate_inverse_multiselect(
        [_attr("people", True)],
        [_attr("companies", False)],
    )
    assert verdict.status == "stop"
    assert any("companies.meetings" in r for r in verdict.reasons)


def test_any_single_valued_dominates_a_missing_side() -> None:
    # A single-valued inverse is a hard STOP even if the other side is absent.
    verdict = evaluate_inverse_multiselect(
        [_attr("people", False)],
        [],
    )
    assert verdict.status == "stop"


def test_missing_inverse_attr_is_inconclusive() -> None:
    verdict = evaluate_inverse_multiselect([_attr("people", True)], [])
    assert verdict.status == "inconclusive"
    assert any("companies" in r for r in verdict.reasons)


def test_no_inverse_attrs_anywhere_is_inconclusive() -> None:
    verdict = evaluate_inverse_multiselect([], [])
    assert verdict.status == "inconclusive"


def test_linked_record_keys_accepts_read_and_write_shapes() -> None:
    keys = _MOD._linked_record_keys(
        [
            {"object_slug": "people", "record_id": "p1"},  # read model
            {"object": "companies", "record_id": "c1"},  # write model
        ],
    )
    assert keys == {("people", "p1"), ("companies", "c1")}


def test_has_duplicate_links_detects_repeat() -> None:
    assert _MOD._has_duplicate_links(
        [
            {"object_slug": "people", "record_id": "p1"},
            {"object_slug": "people", "record_id": "p1"},
        ],
    )
    assert not _MOD._has_duplicate_links(
        [
            {"object_slug": "people", "record_id": "p1"},
            {"object_slug": "companies", "record_id": "c1"},
        ],
    )
