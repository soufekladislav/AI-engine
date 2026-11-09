from app.providers.base import ProviderResult


class MockProvider:
    name = "mock"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def available(self) -> bool:
        return self.enabled

    def generate(self, model: str, prompt: str, context: dict, max_output_tokens: int) -> ProviderResult:
        short = prompt.strip().replace("\n", " ")[:240]
        return ProviderResult(
            text=f"[MOCK] Fresh AI Engine funguje. Přijatý úkol: {short}",
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=20,
            raw={"mock": True},
        )
