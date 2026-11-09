import json
import httpx
from app.providers.base import ProviderResult, ProviderAPIError


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str, version: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.version = version

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, model: str, prompt: str, context: dict, max_output_tokens: int) -> ProviderResult:
        from app.providers.messages import provider_messages
        instruction, conversation = provider_messages(prompt, context)
        body = {'model': model, 'max_tokens': max_output_tokens,
                'messages': conversation}
        if instruction:
            body['system'] = instruction
        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.version,
                    "content-type": "application/json",
                },
                json=body,
            )
            if r.is_error:
                retry_after = r.headers.get("retry-after")
                retry_after_seconds = int(retry_after) if retry_after and str(retry_after).isdigit() else None
                raise ProviderAPIError(
                    self.name, f"anthropic API {r.status_code}",
                    status_code=r.status_code, public_detail=r.text[:1500],
                    retry_after_seconds=retry_after_seconds,
                )
            data = r.json()
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        usage = data.get("usage") or {}
        return ProviderResult(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )
