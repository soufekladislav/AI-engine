from dataclasses import dataclass
from typing import Protocol, Any


class ProviderAPIError(RuntimeError):
    """Normalized provider transport/API failure without leaking credentials."""
    def __init__(self, provider: str, message: str, *, status_code: int | None = None,
                 public_detail: str | None = None, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.public_detail = public_detail or message
        self.retry_after_seconds = retry_after_seconds


@dataclass
class ProviderResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] | None = None


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...

    def generate(self, model: str, prompt: str, context: dict, max_output_tokens: int) -> ProviderResult: ...
