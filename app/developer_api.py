"""Admin-only OpenAPI description of implemented Engine request routes.

Do not describe proposed endpoints as existing. Schemas are sourced from Pydantic.
"""
from app.schemas import EngineRequest, EngineResponse, JobFeedbackRequest


def build_openapi():
    # Pydantic JSON schema definitions are bundled into components; preserve $refs.
    schemas = {}
    for cls in (EngineRequest, EngineResponse, JobFeedbackRequest):
        raw = cls.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(raw.pop("$defs", {}))
        schemas[cls.__name__] = raw
    bearer = [{"bearerAuth": []}]
    return {
      "openapi": "3.1.0",
      "info": {"title": "Fresh AI Engine – Developer API", "version": "0.8.0-docs.1",
               "description": "Actual endpoints audited against flask_app.py; no new AI capabilities implied."},
      "servers": [{"url": "https://aiengine.freshcrackers.cz"}],
      "components": {"securitySchemes": {"bearerAuth": {"type":"http", "scheme":"bearer"}}, "schemas": schemas},
      "paths": {
        "/v1/health": {"get": {"summary":"Service health (public)", "responses":{"200":{"description":"Health payload"}}}},
        "/v1/engine/run": {"post": {"security":bearer,"summary":"Synchronous AI execution", "requestBody":_body(), "responses":_responses()}},
        "/v1/engine/jobs": {"post": {"security":bearer,"summary":"Queue asynchronous text job", "requestBody":_body(), "responses":{"201":{"description":"Returns job_id and queued status"}, "401":{"description":"Missing or invalid API key"},"402":{"description":"Insufficient credits"},"422":{"description":"Validation error"}}}},
        "/v1/engine/jobs/{job_id}": {"get": {"security":bearer, "summary":"Get job state and result", "parameters":[{"name":"job_id","in":"path","required":True,"schema":{"type":"string"}}],"responses":{"200":{"description":"Job detail","content":{"application/json":{"schema":{"$ref":"#/components/schemas/EngineResponse"}}}},"404":{"description":"Job not found"}}}},
        "/v1/engine/route": {"post":{"security":bearer,"summary":"Routing decision only", "responses":{"200":{"description":"Model routing output"}}}},
        "/v1/media/capabilities": {"get":{"summary":"Media capabilities (check authorization in current deployment)","responses":{"200":{"description":"Available media providers"}}}}
      }
    }


def _body():
    return {"required": True, "content": {"application/json": {"schema": {"$ref":"#/components/schemas/EngineRequest"}}}}


def _responses():
    return {"200":{"description":"Job result","content":{"application/json":{"schema":{"$ref":"#/components/schemas/EngineResponse"}}}},"401":{"description":"Unauthorized"},"402":{"description":"Insufficient credits"},"422":{"description":"Invalid request"}}


def public_capability_catalog(db):
    """Only return non-secret metadata; no customer authorization implied."""
    from app.capabilities import definitions
    try:
        return [(c.code, c.label, bool(c.enabled)) for c in definitions(db)]
    except Exception:
        return []
