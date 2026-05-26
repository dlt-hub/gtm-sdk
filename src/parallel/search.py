from typing import Any

from pydantic import BaseModel, ConfigDict

from libs.parallel.client import search
from libs.parallel.models import SearchInput, SearchResponse
from libs.parallel.types import SearchMode
from src.api_keys import inject_api_keys
from src.app import app, image
from src.secrets_bootstrap import bootstrap_secret, with_secrets


def _decorate_parallel_key_error(exc: ValueError) -> ValueError:
    msg = str(exc)
    if "PARALLEL_API_KEY" not in msg:
        return exc
    return ValueError(
        f"{msg} Set PARALLEL_API_KEY in Infisical (env=dev|staging|prod) — "
        "the bootstrap pattern fetches it at function entry. See ai-672 / "
        "src/secrets_bootstrap.py.",
    )


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("PARALLEL_API_KEY")
def parallel_search(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> SearchResponse:
    with inject_api_keys(api_keys or {}):
        query = SearchQuery.model_validate(payload)
        try:
            return search(
                SearchInput(
                    objective=query.objective,
                    mode=query.mode,
                    max_results=query.max_results,
                ),
            )
        except ValueError as exc:
            raise _decorate_parallel_key_error(exc) from None
        except TypeError:
            raise
        except Exception as exc:
            raise ValueError(f"{type(exc).__name__}: {exc}") from None


# Query model
class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    mode: SearchMode = "one-shot"
    max_results: int = 10
