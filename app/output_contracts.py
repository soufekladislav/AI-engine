"""Structured output contracts for Fresh AI Engine.

The Engine stays provider-agnostic: connectors describe the desired result,
providers return text, and this module parses/validates the result before a job
is marked completed. A small registry provides defaults for well-known
operations while connectors may also submit an inline JSON-schema subset.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


SEO_METADATA_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["issue_id", "post_id", "changes"],
                "properties": {
                    "issue_id": {"type": ["integer", "string"]},
                    "post_id": {"type": ["integer", "string"]},
                    "changes": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1},
                            "description": {"type": "string", "minLength": 1},
                        },
                        "anyOf": [
                            {"required": ["title"]},
                            {"required": ["description"]},
                        ],
                    },
                },
            },
        }
    },
}

SCHEMA_REGISTRY: dict[str, dict[str, Any]] = {
    "seo.metadata.optimize.v1": SEO_METADATA_SCHEMA,
}

OPERATION_DEFAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "seo.metadata.optimize": {
        "type": "json",
        "schema_id": "seo.metadata.optimize.v1",
        "strict": True,
    }
}


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return 10**9


def validate_contract_declaration(contract: dict[str, Any] | None) -> None:
    if not contract:
        return
    kind = str(contract.get("type") or "text")
    if kind not in {"text", "json"}:
        raise ValueError("output_contract.type must be 'text' or 'json'")
    schema_id = contract.get("schema_id")
    inline = contract.get("schema")
    if schema_id and not isinstance(schema_id, str):
        raise ValueError("output_contract.schema_id must be a string")
    if inline is not None and not isinstance(inline, dict):
        raise ValueError("output_contract.schema must be a JSON object")
    if inline is not None and _json_len(inline) > 30000:
        raise ValueError("output_contract.schema is too large (max 30000 JSON characters)")
    if kind == "json" and schema_id and schema_id not in SCHEMA_REGISTRY and inline is None:
        raise ValueError(f"Unknown output_contract.schema_id: {schema_id}")


def resolve_contract(operation: str | None, declared: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return normalized contract. Explicit connector contract wins over defaults."""
    contract = deepcopy(declared) if declared else None
    if contract is None and operation:
        contract = deepcopy(OPERATION_DEFAULT_CONTRACTS.get(operation))
    if not contract:
        return None
    validate_contract_declaration(contract)
    contract.setdefault("type", "text")
    contract.setdefault("strict", True)
    if contract.get("type") == "json":
        schema_id = contract.get("schema_id")
        if contract.get("schema") is None and schema_id:
            contract["schema"] = deepcopy(SCHEMA_REGISTRY[schema_id])
    return contract


def contract_public_metadata(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract:
        return None
    return {
        "type": contract.get("type", "text"),
        "schema_id": contract.get("schema_id"),
        "strict": bool(contract.get("strict", True)),
    }


def contract_instruction(contract: dict[str, Any] | None) -> str:
    if not contract:
        return ""
    if contract.get("type") != "json":
        return "Return a useful non-empty text answer."
    schema = contract.get("schema")
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":")) if schema else "{}"
    return (
        "Return ONLY one valid JSON value. Do not use Markdown fences or explanatory text. "
        "The JSON must satisfy this schema: " + schema_text
    )


def _extract_json_candidate(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        json.loads(s)
        return s
    except Exception:
        pass

    starts = [i for i, ch in enumerate(s) if ch in "{["]
    for start in starts:
        opening = s[start]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except Exception:
                        break
    return s


def _repair_json_lexical(candidate: str) -> tuple[str, list[str]]:
    """Apply conservative, deterministic JSON repairs only.

    We repair lexical mistakes that preserve the provider's actual content:
    invalid backslash escapes, raw control characters inside strings and
    trailing commas. We deliberately DO NOT invent missing closing quotes,
    braces or brackets, because that could turn a truncated article into a
    deceptively valid result. Truncation must use the recovery route instead.
    """
    if not candidate:
        return candidate, []
    out: list[str] = []
    notes: list[str] = []
    in_string = False
    i = 0
    while i < len(candidate):
        ch = candidate[i]
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if in_string and ch == "\\":
            if i + 1 >= len(candidate):
                # A dangling backslash is structural truncation, not safe repair.
                out.append(ch)
                i += 1
                continue
            nxt = candidate[i + 1]
            if nxt in '"\\/bfnrt':
                out.extend((ch, nxt))
                i += 2
                continue
            if nxt == 'u' and i + 5 < len(candidate):
                code = candidate[i + 2:i + 6]
                if all(c in '0123456789abcdefABCDEF' for c in code):
                    out.append(candidate[i:i + 6])
                    i += 6
                    continue
            # Preserve the original slash literally instead of interpreting an
            # invalid JSON escape. Example: ``\x`` -> ``\\x``.
            out.extend(('\\', '\\', nxt))
            notes.append('escaped_invalid_backslash')
            i += 2
            continue
        if in_string and ord(ch) < 0x20:
            out.append(json.dumps(ch, ensure_ascii=False)[1:-1])
            notes.append('escaped_control_character')
            i += 1
            continue
        out.append(ch)
        i += 1

    repaired = ''.join(out)
    without_trailing = re.sub(r',\s*([}\]])', r'\1', repaired)
    if without_trailing != repaired:
        repaired = without_trailing
        notes.append('removed_trailing_comma')
    return repaired, list(dict.fromkeys(notes))


def parse_json_output_detailed(text: str) -> tuple[Any | None, list[str], bool, list[str]]:
    candidate = _extract_json_candidate(text)
    try:
        return json.loads(candidate), [], False, []
    except Exception as first_exc:
        repaired, notes = _repair_json_lexical(candidate)
        if notes and repaired != candidate:
            try:
                return json.loads(repaired), [], True, notes
            except Exception:
                pass
        return None, [f"Invalid JSON: {type(first_exc).__name__}: {str(first_exc)[:240]}"], False, notes


def parse_json_output(text: str) -> tuple[Any | None, list[str]]:
    value, errors, _, _ = parse_json_output_detailed(text)
    return value, errors


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_json_schema(value: Any, schema: dict[str, Any] | None, path: str = "$") -> list[str]:
    """Validate the JSON-schema subset used by connector contracts.

    Supported: type, required, properties, items, min/maxItems, min/maxLength,
    min/maxProperties, enum, const, anyOf, allOf, additionalProperties=false.
    """
    if not schema:
        return []
    errors: list[str] = []

    if "allOf" in schema:
        for part in schema.get("allOf") or []:
            if isinstance(part, dict):
                errors.extend(validate_json_schema(value, part, path))
    if "anyOf" in schema:
        alternatives = [validate_json_schema(value, part, path) for part in (schema.get("anyOf") or []) if isinstance(part, dict)]
        if alternatives and not any(not e for e in alternatives):
            errors.append(f"{path}: does not satisfy anyOf")

    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, str(t)) for t in expected_types):
            errors.append(f"{path}: expected type {expected_types}, got {type(value).__name__}")
            return errors[:20]

    if "enum" in schema and value not in schema.get("enum", []):
        errors.append(f"{path}: value is not in enum")
    if "const" in schema and value != schema.get("const"):
        errors.append(f"{path}: value does not match const")

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        props = schema.get("properties") or {}
        for key, child in props.items():
            if key in value and isinstance(child, dict):
                errors.extend(validate_json_schema(value[key], child, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extras = [k for k in value if k not in props]
            if extras:
                errors.append(f"{path}: unexpected properties {extras[:8]}")
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            errors.append(f"{path}: requires at least {schema['minProperties']} properties")
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            errors.append(f"{path}: allows at most {schema['maxProperties']} properties")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: requires at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(validate_json_schema(item, item_schema, f"{path}[{idx}]"))
                if len(errors) >= 20:
                    break

    if isinstance(value, str):
        if "minLength" in schema and len(value.strip()) < int(schema["minLength"]):
            errors.append(f"{path}: string is shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: string is longer than {schema['maxLength']}")

    return errors[:20]


def validate_provider_output_detailed(
    text: str, contract: dict[str, Any] | None
) -> tuple[bool, Any, str, list[str], dict[str, Any]]:
    """Return validation result plus deterministic-repair metadata."""
    meta = {"repaired": False, "repair_notes": []}
    if not contract or contract.get("type") == "text":
        normalized = (text or "").strip()
        if not normalized:
            return False, None, "", ["Provider returned an empty text response"], meta
        return True, normalized, normalized, [], meta

    value, errors, repaired, repair_notes = parse_json_output_detailed(text)
    meta = {"repaired": bool(repaired), "repair_notes": repair_notes}
    if errors:
        return False, None, "", errors, meta
    schema_errors = validate_json_schema(value, contract.get("schema"))
    if schema_errors:
        return False, value, json.dumps(value, ensure_ascii=False, separators=(",", ":")), schema_errors, meta
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return True, value, canonical, [], meta


def validate_provider_output(text: str, contract: dict[str, Any] | None) -> tuple[bool, Any, str, list[str]]:
    """Backward-compatible 4-tuple wrapper used by older Engine callers."""
    valid, value, canonical, errors, _ = validate_provider_output_detailed(text, contract)
    return valid, value, canonical, errors
