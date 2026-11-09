# Fresh AI Engine 0.6.3 – nasazení

## Co se mění

0.6.3 nemění image adapter ani databázový kontrakt. Image generation z 0.6.2 už funguje. Release doplňuje operation profily pro Lindu tak, aby kritické strukturované úlohy nebyly směrovány jen podle obecného task type/ceny:

- `chat.conversation`
- `analysis.project_profile`
- `analysis.brand_style`
- `marketing.campaign.plan`
- `marketing.social.create`

Profil projektu, Brand Style DNA a plán obsahu preferují spolehlivost a validní JSON. Chat používá balanced profil.

## Nasazení

Aktivuj existující venv AI Enginu, přejdi do app rootu a udělej zálohu aplikačních souborů. Rozbal `fresh-ai-engine-0.6.3-update.zip` přes 0.6.2.

Pak:

```bash
python -m py_compile app/__init__.py app/diagnostics.py app/routing_knowledge.py flask_app.py
mkdir -p tmp
touch tmp/restart.txt
curl --max-time 20 -s https://aiengine.freshcrackers.cz/v1/health
```

Image generation není potřeba předělávat: 0.6.2 už používá operation-first `image.generate` a OpenAI Images adapter. 0.6.3 pouze zlepšuje volbu textových modelů pro Lindiny pracovní operace.

## Doporučené pořadí

1. AI Engine 0.6.3.
2. Linda 0.4.7.0 + `pip install -r requirements.txt`.
3. Linda `check_install.py`.
4. Jeden `test_engine_image.py`.
5. Jeden obrázek přes UI.
6. Teprve potom staré retry nebo maintenance.
