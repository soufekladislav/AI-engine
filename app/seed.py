from sqlalchemy import select

from app.config import get_settings
from app.models import ModelRoute, RuntimeSetting, CapabilityDefinition
from app.capabilities import DEFAULTS, MEDIA_CAPABILITIES
import json


def seed_defaults(db):
    s = get_settings()
    if not db.scalar(select(ModelRoute.id).limit(1)):
        # New-install defaults. Existing databases are never overwritten here;
        # route settings changed in the admin console remain intact on upgrade.
        routes = [
            ModelRoute(
                code="openai-fast",
                provider="openai",
                model_name=s.openai_model,
                quality=4,
                priority=80,
                capabilities="text,general,chat,marketing,seo",
                input_usd_per_million=0.20,
                output_usd_per_million=1.20,
            ),
            ModelRoute(
                code="anthropic-quality",
                provider="anthropic",
                model_name=s.anthropic_model,
                quality=5,
                priority=75,
                capabilities="text,analysis,coding,chat,general",
                input_usd_per_million=3.00,
                output_usd_per_million=15.00,
            ),
            ModelRoute(
                code="gemini-fast",
                provider="gemini",
                model_name=s.gemini_model,
                quality=4,
                priority=70,
                capabilities="text,general,chat,analysis,marketing,seo",
            ),
            ModelRoute(
                code="deepseek-fast",
                provider="deepseek",
                model_name=s.deepseek_model,
                quality=3,
                priority=65,
                capabilities="text,general,chat,analysis,coding",
                input_usd_per_million=0.30,
                output_usd_per_million=1.20,
            ),
            ModelRoute(
                code="mock-local",
                provider="mock",
                model_name="mock-v1",
                quality=1,
                priority=1,
                capabilities="text,general,chat,analysis,marketing,seo,coding",
                enabled=False,
            ),
        ]
        db.add_all(routes)
    if not db.get(RuntimeSetting, "queue_paused"):
        db.add(RuntimeSetting(key="queue_paused", value="false"))
    if not db.get(RuntimeSetting, "credits_per_usd"):
        db.add(RuntimeSetting(key="credits_per_usd", value="1000"))
    if not db.get(RuntimeSetting, "credit_price_czk"):
        db.add(RuntimeSetting(key="credit_price_czk", value="0.10"))
    for code, label, description, priority, phrases in DEFAULTS:
        row = db.get(CapabilityDefinition, code)
        if row is None:
            db.add(CapabilityDefinition(code=code, label=label, description=description,
                                        priority=priority, phrases_json=json.dumps(phrases, ensure_ascii=False),
                                        enabled=code not in MEDIA_CAPABILITIES))
        elif code in MEDIA_CAPABILITIES:
            # Reserved media capability descriptions are Engine-owned; updating
            # them does not overwrite operator routing configuration.
            row.label = label
            row.description = description
    db.commit()
