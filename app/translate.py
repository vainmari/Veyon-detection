"""
app/translate.py
────────────────
Translation layer for English (default) + Lithuanian.

Usage
─────
    from app.translate import t, get_lang, set_lang

    ui.label(t("dashboard_title"))
    ui.label(t("status_running_group").format(g=group_name))

Language is stored per-browser-session via NiceGUI's app.storage.user.
Switching language triggers a page reload so all t() calls re-execute.
"""
from __future__ import annotations
import json
from pathlib import Path

# Resolve the path to the locales folder relative to this file
LOCALES_DIR = Path(__file__).parent / "locales"

TRANSLATIONS: dict[str, dict[str, str]] = {}

def _load_translations() -> None:
    """Load all .json translation files into memory on startup."""
    if not LOCALES_DIR.exists():
        print(f"Warning: Locales directory not found at {LOCALES_DIR}")
        return

    # Iterate over all .json files in the directory
    for file_path in LOCALES_DIR.glob("*.json"):
        lang_code = file_path.stem  # e.g., 'en' from 'en.json'
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                TRANSLATIONS[lang_code] = json.load(f)
        except Exception as e:
            print(f"Error loading {file_path.name}: {e}")

# Automatically load translations when this module is imported
_load_translations()


def t(key: str) -> str:
    """Return the translated string for *key* in the current UI language.

    Reads the language from NiceGUI's per-browser-session storage.
    Falls back to English, then to the raw key if the key is missing.
    """
    from nicegui import app as _app  # deferred import — avoids circular dep
    lang = _app.storage.user.get("lang", "en")
    return (
        TRANSLATIONS.get(lang, {}).get(key)
        or TRANSLATIONS.get("en", {}).get(key)
        or key
    )


def get_lang() -> str:
    """Return the current UI language code (e.g. ``"en"`` or ``"lt"``)."""
    from nicegui import app as _app
    return _app.storage.user.get("lang", "en")


def set_lang(lang: str) -> None:
    """Persist *lang* to the per-session storage (e.g. ``"en"`` or ``"lt"``)."""
    from nicegui import app as _app
    _app.storage.user["lang"] = lang