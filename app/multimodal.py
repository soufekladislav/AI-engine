"""Provider-independent media adapters for Fresh AI Engine.

Image generation is deliberately separated from the text router. The public
Engine operation ``image.generate`` and the legacy media endpoint both call the
same adapter registry, so a text model can never be treated as an image
fallback.
"""
from __future__ import annotations

import base64
import binascii
import re
import struct
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
OUTPUT_MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}

# Current public OpenAI image-token prices (USD / 1M tokens). These values are
# only used to estimate provider cost from the provider-reported usage object.
# If usage is absent, a conservative per-image estimate is used and the trace
# explicitly marks the cost source as an estimate.
OPENAI_IMAGE_TOKEN_RATES = {
    "gpt-image-2.5": {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0},
    "gpt-image-2": {"text_input": 2.5, "image_input": 4.0, "image_output": 15.0},
    "gpt-image-1.5": {"text_input": 5.0, "image_input": 8.0, "image_output": 32.0},
    "gpt-image-1-mini": {"text_input": 2.0, "image_input": 2.5, "image_output": 8.0},
    "gpt-image-1": {"text_input": 5.0, "image_input": 10.0, "image_output": 40.0},
}

# Output-side per-image estimates for the common legacy sizes. Input prompt
# tokens are added separately when usage is available. GPT Image 2.5 does not
# publish a fixed per-image table, so its fallback estimate is intentionally
# conservative relative to GPT Image 2.
_PER_IMAGE_ESTIMATES = {
    "gpt-image-1-mini": {
        "low": {"1024x1024": 0.005, "1024x1536": 0.006, "1536x1024": 0.006},
        "medium": {"1024x1024": 0.011, "1024x1536": 0.015, "1536x1024": 0.015},
        "high": {"1024x1024": 0.036, "1024x1536": 0.052, "1536x1024": 0.052},
    },
    "gpt-image-1": {
        "low": {"1024x1024": 0.011, "1024x1536": 0.016, "1536x1024": 0.016},
        "medium": {"1024x1024": 0.042, "1024x1536": 0.063, "1536x1024": 0.063},
        "high": {"1024x1024": 0.167, "1024x1536": 0.25, "1536x1024": 0.25},
    },
    "gpt-image-1.5": {
        "low": {"1024x1024": 0.009, "1024x1536": 0.013, "1536x1024": 0.013},
        "medium": {"1024x1024": 0.034, "1024x1536": 0.05, "1536x1024": 0.05},
        "high": {"1024x1024": 0.133, "1024x1536": 0.20, "1536x1024": 0.20},
    },
    "gpt-image-2": {
        "low": {"1024x1024": 0.006, "1024x1536": 0.005, "1536x1024": 0.005},
        "medium": {"1024x1024": 0.053, "1024x1536": 0.041, "1536x1024": 0.041},
        "high": {"1024x1024": 0.211, "1024x1536": 0.165, "1536x1024": 0.165},
    },
}


@dataclass(frozen=True)
class ImageProviderSpec:
    provider: str
    adapter: str
    default_model: str
    configured: bool


def _image_data_url(mime: str, data: str) -> str:
    if mime not in ALLOWED_MIME:
        raise ValueError("Unsupported image MIME")
    if not isinstance(data, str) or len(data) > MAX_INPUT_IMAGE_BYTES * 4 // 3 + 32:
        raise ValueError("Image missing or too large")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid base64 image")
    if not decoded or len(decoded) > MAX_INPUT_IMAGE_BYTES:
        raise ValueError("Image missing or too large")
    return f"data:{mime};base64,{data}"


def _post_openai(path: str, body: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    if not s.openai_api_key:
        raise ValueError("OpenAI API key not configured")
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        response = client.post(
            s.openai_base_url.rstrip("/") + path,
            headers={"Authorization": "Bearer " + s.openai_api_key},
            json=body,
        )
        response.raise_for_status()
        return response.json()


def _model_family(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("gpt-image-2.5"):
        return "gpt-image-2.5"
    if m.startswith("gpt-image-2"):
        return "gpt-image-2"
    if m.startswith("gpt-image-1.5"):
        return "gpt-image-1.5"
    if m.startswith("gpt-image-1-mini"):
        return "gpt-image-1-mini"
    if m.startswith("gpt-image-1"):
        return "gpt-image-1"
    if m == "chatgpt-image-latest":
        return "gpt-image-2.5"
    return "unknown"


def _validate_image_model(model: str) -> str:
    model = (model or "").strip()
    if model == "chatgpt-image-latest" or model.startswith("gpt-image-"):
        return model
    raise ValueError("Unsupported image model")


def _validate_size(model: str, size: str) -> str:
    size = (size or "1024x1024").strip().lower()
    if size == "auto":
        return size
    m = re.fullmatch(r"(\d{2,4})x(\d{2,4})", size)
    if not m:
        raise ValueError("Unsupported image size")
    w, h = int(m.group(1)), int(m.group(2))
    family = _model_family(model)
    if family in {"gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5"}:
        if size not in {"1024x1024", "1536x1024", "1024x1536"}:
            raise ValueError("This image model supports only 1024x1024, 1536x1024 or 1024x1536")
        return size
    # Current GPT Image 2/2.5 custom-size guardrails. Keeping validation local
    # avoids pointless paid requests with impossible dimensions.
    if w % 16 or h % 16 or w > 3840 or h > 3840:
        raise ValueError("Image dimensions must be multiples of 16 and <= 3840")
    ratio = max(w, h) / min(w, h)
    pixels = w * h
    if ratio > 3.0 or pixels < 655_360 or pixels > 8_294_400:
        raise ValueError("Image dimensions are outside supported aspect/pixel limits")
    return size


def _quality_for_engine(engine_quality: str | None, explicit: str | None = None) -> str:
    if explicit:
        q = str(explicit).strip().lower()
        if q not in {"auto", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Unsupported image quality")
        return q
    return {
        "economy": "low",
        "balanced": "medium",
        "high": "high",
        "best": "high",
        "auto": "medium",
    }.get((engine_quality or "auto").lower(), "medium")


def _image_dimensions(data: bytes, declared_size: str | None = None) -> tuple[int | None, int | None, str | None]:
    """Read dimensions/MIME without adding Pillow as a production dependency."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])[0], struct.unpack(">II", data[16:24])[1], "image/png"
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(data):
                break
            seglen = int.from_bytes(data[i:i + 2], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and i + 7 < len(data):
                h = int.from_bytes(data[i + 3:i + 5], "big")
                w = int.from_bytes(data[i + 5:i + 7], "big")
                return w, h, "image/jpeg"
            i += max(seglen, 2)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        # VP8X stores width/height minus one as 24-bit little-endian values.
        if len(data) >= 30 and data[12:16] == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h, "image/webp"
    if declared_size and re.fullmatch(r"\d+x\d+", declared_size):
        w, h = declared_size.split("x", 1)
        return int(w), int(h), None
    return None, None, None


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    inp = usage.get("input_tokens_details") or {}
    out = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "input_text_tokens": int(inp.get("text_tokens") or 0),
        "input_image_tokens": int(inp.get("image_tokens") or 0),
        "output_image_tokens": int(out.get("image_tokens") or usage.get("output_tokens") or 0),
        "output_text_tokens": int(out.get("text_tokens") or 0),
    }


def estimate_openai_image_cost(model: str, usage: dict[str, Any] | None, quality: str, size: str) -> tuple[float, str]:
    family = _model_family(model)
    normalized = _normalize_usage(usage)
    rates = OPENAI_IMAGE_TOKEN_RATES.get(family)
    if rates and (normalized["input_tokens"] or normalized["output_tokens"] or normalized["output_image_tokens"]):
        text_in = normalized["input_text_tokens"]
        image_in = normalized["input_image_tokens"]
        # If provider exposes only aggregate input_tokens, treat unclassified
        # prompt input as text for text-to-image generation.
        if text_in == 0 and image_in == 0:
            text_in = normalized["input_tokens"]
        image_out = normalized["output_image_tokens"] or normalized["output_tokens"]
        cost = (
            text_in * rates["text_input"]
            + image_in * rates["image_input"]
            + image_out * rates["image_output"]
        ) / 1_000_000.0
        return round(cost, 8), "provider_usage_tokens"
    return image_request_cost_estimate(model, quality, size), "fallback_per_image_estimate"


def image_request_cost_estimate(model: str, quality: str, size: str) -> float:
    family = _model_family(model)
    q = "medium" if quality in {"auto", "xhigh", "max"} else quality
    common_size = size if size in {"1024x1024", "1024x1536", "1536x1024"} else "1024x1024"
    table = _PER_IMAGE_ESTIMATES.get(family)
    if table:
        return float(table.get(q, table.get("medium", {})).get(common_size, 0.05))
    if family == "gpt-image-2.5":
        # Conservative reserve/estimate based on current GPT Image 2 output
        # examples and the 2.5 output-token rate. Actual billing is replaced by
        # provider usage after the call whenever usage is present.
        base = _PER_IMAGE_ESTIMATES["gpt-image-2"].get(q, _PER_IMAGE_ESTIMATES["gpt-image-2"]["medium"]).get(common_size, 0.053)
        return round(max(0.01, base * 2.0), 6)
    return 0.10


def available_image_generation_providers() -> list[ImageProviderSpec]:
    s = get_settings()
    return [
        ImageProviderSpec(
            provider="openai",
            adapter="OpenAIImagesAPI",
            default_model=s.openai_image_model,
            configured=bool(s.openai_api_key),
        )
    ]


def available_capabilities() -> dict[str, Any]:
    s = get_settings()
    image_providers = available_image_generation_providers()
    return {
        "image_generation": {
            "enabled": any(p.configured for p in image_providers),
            "providers": [
                {"provider": p.provider, "configured": p.configured, "adapter": p.adapter, "default_model": p.default_model}
                for p in image_providers
            ],
            # Backward-compatible boolean used by older diagnostics/UI.
            "openai": bool(s.openai_api_key),
            "operation": "image.generate",
            "endpoint": "/v1/media/image/generate",
            "shared_adapter": True,
        },
        "image_understanding": {"openai": bool(s.openai_api_key)},
        "video_generation": {"status": "not_connected"},
        "video_understanding": {
            "status": "not_connected",
            "alternative": "Provide extracted video frames to image-understanding with timestamps",
        },
    }


def generate_image_with_provider(
    provider: str,
    prompt: str,
    *,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str = "medium",
    output_format: str = "png",
    background: str = "auto",
    output_compression: int | None = None,
) -> dict[str, Any]:
    """Generate one normalized image asset through a real image provider."""
    if provider != "openai":
        raise ValueError(f"Unsupported image provider: {provider}")
    s = get_settings()
    if not s.openai_api_key:
        raise ValueError("OpenAI API key not configured")
    prompt = (prompt or "").strip()
    if not prompt or len(prompt) > 32_000:
        raise ValueError("Invalid prompt length")
    model = _validate_image_model(model or s.openai_image_model)
    size = _validate_size(model, size)
    quality = _quality_for_engine(None, quality)
    output_format = (output_format or "png").strip().lower()
    if output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("Unsupported image output format")
    background = (background or "auto").strip().lower()
    if background not in {"auto", "opaque", "transparent"}:
        raise ValueError("Unsupported image background")
    if background == "transparent" and output_format not in {"png", "webp"}:
        raise ValueError("Transparent background requires PNG or WebP")
    if output_compression is not None:
        output_compression = int(output_compression)
        if output_format == "png" or not (0 <= output_compression <= 100):
            raise ValueError("output_compression is supported only for JPEG/WebP and must be 0..100")

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "output_format": output_format,
        "background": background,
        "n": 1,
    }
    if output_compression is not None:
        body["output_compression"] = output_compression
    data = _post_openai("/images/generations", body)
    item = (data.get("data") or [{}])[0]
    b64 = item.get("b64_json")
    url = item.get("url")
    raw: bytes | None = None
    if b64:
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, binascii.Error):
            raise RuntimeError("Image provider returned invalid base64")
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise RuntimeError("Image provider returned an empty or oversized image")
    if not raw and not url:
        raise RuntimeError("Image provider returned no image payload")

    width, height, detected_mime = _image_dimensions(raw or b"", data.get("size") or size)
    mime = detected_mime or OUTPUT_MIME.get(data.get("output_format") or output_format, "image/png")
    provider_cost, cost_source = estimate_openai_image_cost(model, data.get("usage") or {}, data.get("quality") or quality, data.get("size") or size)
    normalized_usage = _normalize_usage(data.get("usage") or {})
    return {
        "kind": "image",
        "mime_type": mime,
        "url": url,
        "base64": b64,
        "width": width,
        "height": height,
        "size_bytes": len(raw) if raw is not None else None,
        "provider": "openai",
        "model": model,
        "quality": data.get("quality") or quality,
        "size": data.get("size") or size,
        "output_format": data.get("output_format") or output_format,
        "background": data.get("background") or background,
        "usage": normalized_usage,
        "provider_cost_usd": provider_cost,
        "cost_source": cost_source,
        "revised_prompt": item.get("revised_prompt"),
    }


def generate_image(prompt: str, model: str | None = None, size: str = "1024x1024", **kwargs) -> dict[str, Any]:
    """Backward-compatible wrapper for older internal callers.

    New code should call ``generate_image_with_provider``. Legacy aliases are
    retained in the returned object so older media clients can migrate without
    a hard cut-over.
    """
    result = generate_image_with_provider("openai", prompt, model=model, size=size, **kwargs)
    return {
        **result,
        "image_base64": result.get("base64"),
        "image_url": result.get("url"),
    }


def understand_image(prompt: str, image_base64: str, mime_type: str = "image/jpeg", model: str | None = None):
    s = get_settings()
    url = _image_data_url(mime_type, image_base64)
    if not prompt or len(prompt) > 8000:
        raise ValueError("Invalid prompt length")
    model = model or s.openai_vision_model
    data = _post_openai(
        "/chat/completions",
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            "max_completion_tokens": 1200,
        },
    )
    return {
        "provider": "openai",
        "model": model,
        "output": (data.get("choices") or [{}])[0].get("message", {}).get("content"),
        "usage": data.get("usage"),
    }


def image_generation_request_parameters(input_data: dict[str, Any], engine_quality: str | None) -> dict[str, Any]:
    """Normalize connector media options without letting output_contract steer routing."""
    s = get_settings()
    data = input_data or {}
    size = data.get("size")
    if not size and data.get("width") and data.get("height"):
        size = f"{int(data['width'])}x{int(data['height'])}"
    return {
        "model": str(data.get("model") or s.openai_image_model),
        "size": str(size or "1024x1024"),
        "quality": _quality_for_engine(engine_quality, data.get("quality") or data.get("image_quality")),
        "output_format": str(data.get("output_format") or data.get("format") or "png").lower(),
        "background": str(data.get("background") or "auto").lower(),
        "output_compression": data.get("output_compression"),
    }
