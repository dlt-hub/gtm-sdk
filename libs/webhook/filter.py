"""Source-agnostic webhook filter primitives.

A ``WebhookFilter`` is a predicate over a webhook ("does this webhook
match my criterion?"). What the caller does with a positive match is
source-local — octolens uses filter hits to drop a webhook from the
Attio path entirely; rb2b uses them to skip a specific op (the
tracking-event emit) while letting the rest of the webhook through.

The shared module owns the whole framework, not just a base class:

- ``WebhookFilter`` — a named pydantic model with a single method,
  ``should_exclude(webhook) -> bool``. Subclasses implement the
  predicate and declare a unique ``type: Literal["..."]`` tag that
  auto-registers them in ``WebhookFilter._registry`` at class
  definition time.
- ``WebhookFilters`` — an ordered collection over *any* subclass of
  ``WebhookFilter``. A ``model_validator`` consults the registry to
  dispatch JSON-loaded filter configs back to their concrete subclass,
  so filter lists round-trip through ``model_dump_json`` /
  ``model_validate_json`` without each source declaring its own
  discriminated union.

Adding a new filter is now one file: subclass ``WebhookFilter``, set
``type: Literal["my-filter"] = "my-filter"``, implement
``should_exclude``. The registry picks it up automatically. New
sources never need to redeclare a ``RootModel`` wrapper or a
``Field(discriminator="type")`` union — that machinery lives here once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel, model_serializer, model_validator


def _resolve_subclass_by_tag(tag: str) -> type[WebhookFilter] | None:
    """Walk the ``WebhookFilter`` subclass tree to find a tag → class mapping.

    Built lazily at validate time rather than eagerly at class-definition
    time because pydantic v2 finalizes ``model_fields`` (where overridden
    ``type: Literal["..."] = "..."`` defaults are materialized) in the
    metaclass ``__init__``, which runs *after* Python calls
    ``__init_subclass__`` on the parent. A class-definition-time hook
    therefore sees only the parent's ``type: str`` and can't read the
    subclass's discriminator tag.

    The lazy walk is fine because every concrete filter subclass is
    instantiated at module load (each source declares its
    ``DEFAULT_FILTERS = WebhookFilters(root=[...])`` at import time), so
    by the time anyone calls ``model_validate``, all subclasses are
    already in ``WebhookFilter.__subclasses__()``.
    """

    def walk(klass: type[WebhookFilter]) -> type[WebhookFilter] | None:
        for sub in klass.__subclasses__():
            field = sub.model_fields.get("type")
            if field is not None and field.default == tag:
                return sub
            hit = walk(sub)
            if hit is not None:
                return hit
        return None

    return walk(WebhookFilter)


def _registered_tags() -> list[str]:
    """Snapshot of every concrete subclass's tag — diagnostic only."""
    tags: list[str] = []

    def walk(klass: type[WebhookFilter]) -> None:
        for sub in klass.__subclasses__():
            field = sub.model_fields.get("type")
            if field is not None and isinstance(field.default, str) and field.default:
                tags.append(field.default)
            walk(sub)

    walk(WebhookFilter)
    return sorted(tags)


class WebhookFilter(BaseModel):
    """Base class for composable webhook filters.

    Subclasses declare ``type: Literal["<tag>"] = "<tag>"`` and implement
    ``should_exclude``. The shared ``WebhookFilters`` collection walks
    the subclass tree at validate time to dispatch JSON-loaded configs
    back to the right concrete subclass — no per-source discriminated
    union boilerplate required.

    ``type`` is intentionally *not* declared on the base: pyright treats
    overriding a mutable-typed attribute with a ``Literal`` as a variance
    violation, and the runtime walker reads ``model_fields["type"]``
    directly from each subclass.

    ``name`` is the human-stable identifier surfaced when a filter
    matches (e.g. in a log line, skipped-reason envelope, or test
    assertion). Prefer a kebab-case slug.
    """

    name: str

    def should_exclude(self, webhook: Any) -> bool:
        raise NotImplementedError


class WebhookFilters(RootModel[list[WebhookFilter]]):
    """Ordered collection of filters; returns the first match.

    Order matters: filters are evaluated top-to-bottom and short-circuit
    at the first hit. Put the cheapest / most-frequently-firing checks
    first if performance ever becomes load-bearing.

    JSON roundtrip is supported via the ``WebhookFilter`` registry —
    each element's ``type`` tag is used to look up the concrete
    subclass on validate, and ``model_dump`` recurses through the
    runtime type of each element so subclass fields survive.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def _dispatch_to_subclass(cls, data: Any) -> Any:
        if not isinstance(data, list):
            return data
        resolved: list[Any] = []
        for item in data:
            if isinstance(item, WebhookFilter):
                resolved.append(item)
                continue
            if isinstance(item, dict):
                tag = item.get("type")
                subclass = (
                    _resolve_subclass_by_tag(tag) if isinstance(tag, str) else None
                )
                if subclass is None:
                    raise ValueError(
                        f"Unknown WebhookFilter type tag {tag!r}; known tags: "
                        f"{_registered_tags()}",
                    )
                resolved.append(subclass.model_validate(item))
                continue
            # Unknown shape — let pydantic surface the error from the
            # default validator.
            resolved.append(item)
        return resolved

    @model_serializer(mode="plain")
    def _serialize_each_with_runtime_type(self) -> list[dict[str, Any]]:
        # Each filter is dumped through its concrete subclass's serializer
        # so subclass-specific fields survive — the default RootModel
        # dump uses the static type's schema and would strip them.
        return [f.model_dump() for f in self.root]

    def should_exclude(self, webhook: Any) -> WebhookFilter | None:
        for f in self.root:
            if f.should_exclude(webhook):
                return f
        return None
