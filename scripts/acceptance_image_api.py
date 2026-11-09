#!/usr/bin/env python3
"""One paid production acceptance call for Fresh AI Engine image.generate.

Run from the deployed application root with the application's virtualenv.
The script reads an existing Engine API key from the local .env through
app.config; it never prints the key.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

import httpx

from app.config import get_settings


def fail(message: str) -> None:
    print("FAIL:", message, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("FRESH_PUBLIC_ORIGIN", "https://aiengine.freshcrackers.cz"))
    parser.add_argument("--prompt", default="A clean editorial illustration of a green ceramic coffee cup on a light desk, no text")
    args = parser.parse_args()

    settings = get_settings()
    keys = sorted(settings.api_keys)
    if not keys:
        fail("No FRESH_ENGINE_API_KEYS key is available in the deployed .env")
    key = keys[0]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    payload = {
        "source": "Linda app",
        "task_type": "general",  # compatibility path intentionally tested
        "operation": "image.generate",
        "quality": "balanced",
        "prompt": args.prompt,
        "input_data": {"size": "1024x1024", "output_format": "png"},
        "max_cost_usd": 0.50,
    }

    with httpx.Client(timeout=240, follow_redirects=False) as client:
        r = client.post(args.url.rstrip("/") + "/v1/engine/run", headers=headers, json=payload)
        if r.status_code != 200:
            fail(f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if data.get("status") != "completed":
            fail(f"status={data.get('status')} category={data.get('error_category')} error={data.get('error')}")
        if data.get("result_type") != "image":
            fail(f"result_type={data.get('result_type')} (text routing regression)")
        result = data.get("result") or {}
        if result.get("kind") != "image":
            fail("result.kind is not image")
        if not result.get("base64") and not result.get("url"):
            fail("image result has neither base64 nor URL")
        if result.get("base64"):
            try:
                raw = base64.b64decode(result["base64"], validate=True)
            except Exception as exc:
                fail(f"base64 decode failed: {type(exc).__name__}")
            if not raw:
                fail("decoded image is empty")
            mime = result.get("mime_type") or ""
            if mime == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                fail("MIME says PNG but bytes are not PNG")
            if mime == "image/jpeg" and not raw.startswith(b"\xff\xd8"):
                fail("MIME says JPEG but bytes are not JPEG")
            if mime == "image/webp" and not (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
                fail("MIME says WebP but bytes are not WebP")
            decoded_bytes = len(raw)
        else:
            decoded_bytes = None

        provider = data.get("provider")
        model = data.get("model") or ""
        if not provider:
            fail("provider metadata missing")
        if model == settings.openai_model or model == "gpt-5.6-luna":
            fail(f"text model incorrectly used for image generation: {model}")
        if data.get("operation") != "image.generate" or data.get("task_type") != "image_generation":
            fail(f"operation/capability normalization failed: {data.get('task_type')} / {data.get('operation')}")

        run_id = data.get("run_id") or data.get("job_id")
        if not run_id:
            fail("run_id missing")
        status_r = client.get(args.url.rstrip("/") + f"/v1/engine/jobs/{run_id}", headers=headers)
        if status_r.status_code != 200:
            fail(f"job audit lookup HTTP {status_r.status_code}")
        audit = status_r.json()
        if audit.get("result_type") != "image" or audit.get("provider") != provider or audit.get("model") != model:
            fail("persisted audit metadata does not match synchronous result")
        if float(audit.get("estimated_cost_usd") or 0) <= 0:
            fail("image usage/cost was not recorded")

    print("PASS image.generate production acceptance")
    print("run_id:", run_id)
    print("provider/model:", provider, "/", model)
    print("mime:", result.get("mime_type"), "dimensions:", result.get("width"), "x", result.get("height"))
    print("decoded_bytes:", decoded_bytes if decoded_bytes is not None else "URL response")
    print("cost_usd:", data.get("estimated_cost_usd"), "credits:", data.get("credits"))
    print("latency_ms:", data.get("latency_ms"), "provider_calls:", data.get("provider_calls"))


if __name__ == "__main__":
    main()
