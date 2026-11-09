import json

import httpx

from app.providers.base import ProviderResult, ProviderAPIError


class OpenAICompatibleProvider:
    def __init__(self, name: str, api_key: str, base_url: str):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        model: str,
        prompt: str,
        context: dict,
        max_output_tokens: int,
    ) -> ProviderResult:
        from app.providers.messages import provider_messages
        instruction, conversation = provider_messages(prompt, context)
        messages = ([{'role':'system','content':instruction}] if instruction else []) + conversation

        body = {
            "model": model,
            "messages": messages,
        }

        # Native structured output is materially more reliable than prompt-only
        # JSON instructions. Keep the public Engine contract provider-agnostic;
        # the adapter translates it to the provider-specific response format.
        contract = context.get("_engine_output_contract") or {}
        if contract.get("type") == "json":
            schema = contract.get("schema")
            if self.name == "openai" and isinstance(schema, dict) and schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "fresh_ai_output",
                        "schema": schema,
                        "strict": bool(contract.get("strict", True)),
                    },
                }
            elif self.name in {"openai", "deepseek"}:
                # DeepSeek's Chat Completions JSON mode supports json_object.
                # OpenAI uses the same format when no explicit schema exists.
                body["response_format"] = {"type": "json_object"}

        # Current OpenAI GPT models use max_completion_tokens. Keep DeepSeek
        # and other OpenAI-compatible endpoints on max_tokens unless their
        # adapter is split later.
        if self.name == "openai":
            body["max_completion_tokens"] = max_output_tokens
            body["reasoning_effort"] = "none"
        else:
            body["max_tokens"] = max_output_tokens

        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if r.is_error:
                retry_after = r.headers.get("retry-after")
                retry_after_seconds = int(retry_after) if retry_after and str(retry_after).isdigit() else None
                detail = r.text[:1500]
                raise ProviderAPIError(
                    self.name, f"{self.name} API {r.status_code}",
                    status_code=r.status_code, public_detail=detail,
                    retry_after_seconds=retry_after_seconds,
                )
            data = r.json()

        text = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        )
        usage = data.get("usage") or {}
        return ProviderResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
