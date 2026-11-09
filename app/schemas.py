from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.output_contracts import validate_contract_declaration


class OutputContract(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["text", "json"] = "text"
    schema_id: str | None = Field(default=None, max_length=160, pattern=r"^[a-z][a-z0-9_.-]*$")
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    strict: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_contract(cls, value):
        """Accept older connector shorthand without weakening the public contract.

        Legacy apps sometimes sent ``type=object``/``array`` instead of
        ``type=json``. Normalize that declaration locally so connector upgrades
        do not turn into avoidable HTTP 422 responses.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_type = str(data.get("type") or "").strip().lower()
        if raw_type in {"object", "array"}:
            data["type"] = "json"
            if data.get("schema") is None and data.get("json_schema") is None:
                data["schema"] = {"type": raw_type}
        elif raw_type == "string":
            data["type"] = "text"
        return data

    @model_validator(mode="after")
    def validate_declaration(self):
        validate_contract_declaration(self.model_dump(by_alias=True, exclude_none=True))
        return self


class EngineRequest(BaseModel):
    source: str = Field(default="fresh-chat", max_length=80)
    project_id: str | None = Field(default=None, max_length=120)
    user_id: str | None = Field(default=None, max_length=120)
    task_type: str | None = Field(default=None, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    operation: str | None = Field(
        default=None,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$",
    )
    quality: Literal["auto", "economy", "balanced", "high", "best"] = "auto"
    prompt: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_contract: OutputContract | None = None
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_output_tokens: int = Field(default=1200, ge=1, le=64000)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_task_type(cls, value):
        """Backward compatibility for connectors that used dotted task_type values."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("context") is None:
            data["context"] = {}
        if data.get("input_data") is None:
            data["input_data"] = {}
        raw_task = data.get("task_type")
        if isinstance(raw_task, str):
            raw_task = raw_task.strip().lower()
            if "." in raw_task:
                if not data.get("operation"):
                    data["operation"] = raw_task
                data["task_type"] = raw_task.split(".", 1)[0]
            elif raw_task:
                data["task_type"] = raw_task
        raw_operation = data.get("operation")
        if isinstance(raw_operation, str):
            data["operation"] = raw_operation.strip().lower() or None
        return data

    @model_validator(mode="after")
    def limit_connector_payloads(self):
        import json
        input_size = len(json.dumps(self.input_data, ensure_ascii=False, separators=(",", ":")))
        # Image understanding can legitimately carry one bounded base64 image.
        # app.multimodal still validates decoded bytes (8 MiB max).
        limit = 12_000_000 if self.operation in {"image.understand", "image_understand"} else 500_000
        if input_size > limit:
            raise ValueError(f"input_data is too large (max {limit} JSON characters)")

        # Long-form article JSON was repeatedly truncated with the historical
        # 1200-token default, producing syntactically incomplete objects.
        # A higher ceiling does not force the provider to spend these tokens;
        # it only gives article generation enough room to close the JSON.
        if self.operation == "marketing.article.create" and self.max_output_tokens < 4096:
            self.max_output_tokens = 4096
        return self


class RouteCandidate(BaseModel):
    code: str
    provider: str
    model: str
    score: float
    estimated_cost_usd: float | None = None


class EngineResponse(BaseModel):
    job_id: str
    status: str
    task_type: str | None = None
    operation: str | None = None
    provider: str | None = None
    model: str | None = None
    output: str | None = None  # legacy canonical string output
    result_type: str | None = None
    result: Any | None = None
    result_validated: bool = False
    schema_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    fallback_used: bool = False
    initial_provider: str | None = None
    final_provider: str | None = None
    recovery_trigger: str | None = None
    provider_calls: int = 0


class JobCreated(BaseModel):
    job_id: str
    status: str


class JobFeedbackRequest(BaseModel):
    outcome: Literal["accepted", "rejected", "regenerated"]
