from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel

from libs.webhook.filter import WebhookFilter, WebhookFilters


class _FakeWebhook(BaseModel):
    value: int


# Concrete subclasses live at module scope (not inside test functions) so they
# only register once with the shared subclass tree. The shared WebhookFilters
# validator walks WebhookFilter.__subclasses__() at validate time to dispatch
# JSON-loaded configs back to the right concrete subclass.


class _LessThanFilter(WebhookFilter):
    # trunk-ignore(pyrefly/bad-override-mutable-attribute): underscore-prefixed
    # test subclass; the base has no `type` field — pyrefly false-positive.
    type: Literal["_test_less_than"] = "_test_less_than"
    threshold: int

    def should_exclude(self, webhook: Any) -> bool:
        return webhook.value < self.threshold


class _EqualsFilter(WebhookFilter):
    # trunk-ignore(pyrefly/bad-override-mutable-attribute): see above
    type: Literal["_test_equals"] = "_test_equals"
    target: int

    def should_exclude(self, webhook: Any) -> bool:
        return webhook.value == self.target


def test_base_should_exclude_raises_for_unimplemented_subclass() -> None:
    class _Unimplemented(WebhookFilter):
        # trunk-ignore(pyrefly/bad-override-mutable-attribute): see above
        type: Literal["_test_unimplemented"] = "_test_unimplemented"

    f = _Unimplemented(name="unimplemented")
    with pytest.raises(NotImplementedError):
        f.should_exclude(_FakeWebhook(value=1))


def test_filters_returns_none_when_no_filter_matches() -> None:
    filters = WebhookFilters(
        root=[
            _LessThanFilter(name="under-10", threshold=10),
            _EqualsFilter(name="equals-42", target=42),
        ],
    )
    assert filters.should_exclude(_FakeWebhook(value=100)) is None


def test_filters_returns_first_matching_filter_short_circuits() -> None:
    """Order matters — the first matching filter is returned, not all matches."""
    filters = WebhookFilters(
        root=[
            _LessThanFilter(name="under-10", threshold=10),
            _EqualsFilter(name="equals-5", target=5),
        ],
    )
    matched = filters.should_exclude(_FakeWebhook(value=5))
    assert matched is not None
    assert matched.name == "under-10"  # first match wins; equals-5 never evaluated


def test_filters_returns_match_when_only_later_filter_matches() -> None:
    filters = WebhookFilters(
        root=[
            _LessThanFilter(name="under-10", threshold=10),
            _EqualsFilter(name="equals-42", target=42),
        ],
    )
    matched = filters.should_exclude(_FakeWebhook(value=42))
    assert matched is not None
    assert matched.name == "equals-42"


def test_filters_empty_collection_never_excludes() -> None:
    filters = WebhookFilters(root=[])
    assert filters.should_exclude(_FakeWebhook(value=0)) is None


def test_filters_json_roundtrip_recovers_concrete_subclass() -> None:
    """The shared collection walks the WebhookFilter subclass tree at validate
    time so JSON-loaded filter configs come back as concrete subclasses without
    each source declaring its own discriminated union.
    """
    original = WebhookFilters(
        root=[
            _LessThanFilter(name="under-10", threshold=10),
            _EqualsFilter(name="equals-42", target=42),
        ],
    )
    roundtrip = WebhookFilters.model_validate_json(original.model_dump_json())
    assert isinstance(roundtrip.root[0], _LessThanFilter)
    assert roundtrip.root[0].threshold == 10
    assert isinstance(roundtrip.root[1], _EqualsFilter)
    assert roundtrip.root[1].target == 42


def test_filters_rejects_unknown_type_tag() -> None:
    """A typo'd or removed filter type surfaces as a validation error so the
    config drift is loud, not silent.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        WebhookFilters.model_validate(
            [{"name": "broken", "type": "definitely-not-a-real-filter-type"}],
        )
    assert "Unknown WebhookFilter type tag" in str(exc_info.value)
