"""Structural type for source webhook handlers.

Every source's ``src/<source>/webhook/*.py`` ``Webhook`` class must satisfy
``WebhookModelProtocol``. The three handler files in ``webhooks/`` then
type-check against this Protocol via a ``TYPE_CHECKING`` alias for the
deploy-time ``WebhookModelToReplace`` placeholder.

We use ``typing.Protocol`` rather than ``abc.ABC`` because each concrete
``Webhook`` already inherits from a Pydantic ``BaseModel`` parent in
``libs/<source>/`` and a second ABC inheritance risks metaclass conflicts
with Pydantic's ``ModelMetaclass``. The Protocol is satisfied
structurally, so the runtime class hierarchy is unchanged.

``@runtime_checkable`` is enabled so a conformance test can use
``isinstance(webhook, WebhookModelProtocol)`` to catch missing methods
on new source classes at ``pytest`` time, instead of at ``modal deploy``
time when the placeholder substitution lands in an image build.

``WebhookModelTypeCheckShim`` is a separate concrete class (not a
``Protocol`` subclass) that webhook handlers alias as the
``WebhookModelToReplace`` placeholder under ``TYPE_CHECKING``. It mirrors
the Protocol surface with ``...`` method bodies, so pyright sees a
fully-implemented base class — no ``reportAbstractUsage`` complaints —
and it inherits from ``BaseModel`` so ``model_rebuild`` / ``model_validate``
type-check too. Runtime never sees this class: ``TYPE_CHECKING`` is
always ``False`` outside the type-checker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic import BaseModel


@runtime_checkable
class WebhookModelProtocol(Protocol):
    """Contract every source's ``Webhook`` class must satisfy.

    Method families:

    - ``modal_*`` — Modal app configuration shared across handlers.
    - ``etl_*`` — GCS ETL handler (``webhooks/export_to_gcp_etl.py``).
    - ``raw_*`` — Raw GCS passthrough handler
      (``webhooks/export_to_gcp_raw.py``).
    - ``storage_*`` — Modal Volume sidecar used by the ETL handler.
    - ``lance_*`` — LanceDB integration (Phase 2+; most sources raise
      ``NotImplementedError``).
    - ``attio_*`` — Attio export handler
      (``webhooks/export_to_attio.py``).
    - ``slack_*`` — Slack export handler
      (``webhooks/export_to_slack.py``).
    """

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]: ...

    @staticmethod
    def etl_get_bucket_name() -> str: ...

    @staticmethod
    def raw_get_bucket_name() -> str: ...

    @staticmethod
    def raw_get_app_name() -> str: ...

    @staticmethod
    def storage_get_app_name() -> str: ...

    @staticmethod
    def storage_get_base_model_type() -> type[BaseModel] | None: ...

    @staticmethod
    def lance_get_project_name() -> str: ...

    @staticmethod
    def lance_get_base_model_type() -> str: ...

    @staticmethod
    def required_api_keys() -> list[str]: ...

    @staticmethod
    def optional_api_keys() -> list[str]:
        """Names of API keys fetched lazily inside the handler but that
        should still be preflighted at deploy time.

        Use this for keys reached on only a subset of the handler's event
        types — declaring them in ``required_api_keys()`` would force every
        other event type to fail-fast on a missing/rotated key even when
        they never touch that API. ``scripts/webhooks-redeploy.py``
        preflights the union of ``required_api_keys()`` and
        ``optional_api_keys()`` so a missing/rotated key surfaces at
        ``modal deploy`` time instead of on the first qualifying Hookdeck
        event.
        """
        ...

    @staticmethod
    def attio_get_app_name() -> str: ...

    def raw_is_valid_webhook(self) -> bool: ...

    def raw_get_invalid_webhook_error_msg(self) -> str: ...

    def etl_is_valid_webhook(self) -> bool: ...

    def etl_get_invalid_webhook_error_msg(self) -> str: ...

    def etl_get_json(self, storage: Any = None) -> str: ...

    def etl_get_file_name(self) -> str: ...

    def etl_get_base_models(self, storage: Any) -> list[Any]: ...

    def attio_is_valid_webhook(self) -> bool: ...

    def attio_get_invalid_webhook_error_msg(self) -> str: ...

    def attio_get_operations(self) -> list[Any]: ...

    @staticmethod
    def slack_get_app_name() -> str: ...

    @staticmethod
    def slack_get_channel_secret_name() -> str: ...

    def slack_is_valid_webhook(self) -> bool: ...

    def slack_get_invalid_webhook_error_msg(self) -> str: ...

    def slack_get_messages(self) -> list[Any]: ...


if TYPE_CHECKING:

    class WebhookModelTypeCheckShim(BaseModel):
        """Concrete type-check stand-in for the ``WebhookModelToReplace``
        placeholder. Mirrors ``WebhookModelProtocol`` with ``...`` bodies so
        pyright sees a fully-implemented base class. Never instantiated at
        runtime — the ``scripts/webhooks-redeploy.py`` substitution pass swaps the
        placeholder for the concrete ``Webhook`` class before
        ``modal deploy``.
        """

        @staticmethod
        def modal_get_secret_collection_names() -> list[str]: ...

        @staticmethod
        def etl_get_bucket_name() -> str: ...

        @staticmethod
        def raw_get_bucket_name() -> str: ...

        @staticmethod
        def raw_get_app_name() -> str: ...

        @staticmethod
        def storage_get_app_name() -> str: ...

        @staticmethod
        def storage_get_base_model_type() -> type[BaseModel] | None: ...

        @staticmethod
        def lance_get_project_name() -> str: ...

        @staticmethod
        def lance_get_base_model_type() -> str: ...

        @staticmethod
        def required_api_keys() -> list[str]: ...

        @staticmethod
        def optional_api_keys() -> list[str]: ...

        @staticmethod
        def attio_get_app_name() -> str: ...

        def raw_is_valid_webhook(self) -> bool: ...

        def raw_get_invalid_webhook_error_msg(self) -> str: ...

        def etl_is_valid_webhook(self) -> bool: ...

        def etl_get_invalid_webhook_error_msg(self) -> str: ...

        def etl_get_json(self, storage: Any = None) -> str: ...

        def etl_get_file_name(self) -> str: ...

        def etl_get_base_models(self, storage: Any) -> list[Any]: ...

        def attio_is_valid_webhook(self) -> bool: ...

        def attio_get_invalid_webhook_error_msg(self) -> str: ...

        def attio_get_operations(self) -> list[Any]: ...

        @staticmethod
        def slack_get_app_name() -> str: ...

        @staticmethod
        def slack_get_channel_secret_name() -> str: ...

        def slack_is_valid_webhook(self) -> bool: ...

        def slack_get_invalid_webhook_error_msg(self) -> str: ...

        def slack_get_messages(self) -> list[Any]: ...
