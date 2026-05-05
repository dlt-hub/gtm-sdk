# trunk-ignore-all(pyrefly/bad-index)
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResponseFormatSchema(BaseModel):
    structured_output: dict[str, Any] = Field(
        alias="schema",
    )

    @classmethod
    def from_structured_output(
        cls: type[ResponseFormatSchema],
        structured_output: type[BaseModel],
    ) -> ResponseFormatSchema:
        schema_json = structured_output.model_json_schema()
        return cls(
            schema=schema_json,
        )


class ResponseFormat(BaseModel):
    type: str = "json_schema"
    json_schema: ResponseFormatSchema

    @classmethod
    def from_structured_output(
        cls: type[ResponseFormat],
        structured_output: type[BaseModel],
    ) -> ResponseFormat:
        schema_dict = ResponseFormatSchema.from_structured_output(
            structured_output=structured_output,
        )
        return cls(
            type="json_schema",
            json_schema=schema_dict,
        )


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


# noinspection PyDataclass
class ChatCompletion(BaseModel):
    model: str | None = "llama-3.1-sonar-small-128k-online"
    messages: list[ChatMessage] | None
    max_tokens: str | None = None
    temperature: float | None = 0.2
    top_p: float | None = 0.9
    return_citations: bool | None = True
    search_domain_filter: list[str] | None = ["perplexity.ai"]
    return_images: bool | None = False
    return_related_questions: bool | None = False
    search_recency_filter: str | None = "month"
    top_k: int | None = 0
    stream: bool | None = False
    presence_penalty: int | None = 0
    frequency_penalty: int | None = 1
    response_format: ResponseFormat | None = None

    def add_structured_output(
        self: ChatCompletion,
        structured_output: type[BaseModel],
    ) -> None:
        self.response_format = ResponseFormat.from_structured_output(
            structured_output=structured_output,
        )
