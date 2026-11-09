import json
import httpx
from app.providers.base import ProviderResult, ProviderAPIError


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, model: str, prompt: str, context: dict, max_output_tokens: int) -> ProviderResult:
        from app.providers.messages import provider_messages
        instruction, conversation = provider_messages(prompt, context)
        contents = [{'role': 'model' if m['role'] == 'assistant' else 'user',
                     'parts': [{'text':m['content']}]} for m in conversation]
        generation_config = {'maxOutputTokens': max_output_tokens}
        contract = context.get('_engine_output_contract') or {}
        if contract.get('type') == 'json':
            generation_config['responseMimeType'] = 'application/json'
            schema = contract.get('schema')
            if isinstance(schema, dict) and schema:
                # generateContent still accepts JSON Schema here; the adapter can
                # move to Google's Interactions API later without changing Engine.
                generation_config['responseJsonSchema'] = schema
        body = {'contents': contents, 'generationConfig': generation_config}
        if instruction:
            body['systemInstruction'] = {'parts':[{'text':instruction}]}
        # generateContent is deliberately isolated behind an adapter. Google recommends
        # the newer Interactions API for new builds; this adapter can be swapped later
        # without changing Fresh Engine's public contract.
        url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, json=body)
            if r.is_error:
                retry_after = r.headers.get("retry-after")
                retry_after_seconds = int(retry_after) if retry_after and str(retry_after).isdigit() else None
                raise ProviderAPIError(
                    self.name, f"gemini API {r.status_code}",
                    status_code=r.status_code, public_detail=r.text[:1500],
                    retry_after_seconds=retry_after_seconds,
                )
            data = r.json()
        candidates = data.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        out = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata") or {}
        return ProviderResult(
            text=out,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            raw=data,
        )
