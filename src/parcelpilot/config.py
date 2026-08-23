from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    environment: str
    source_zip: Path | None
    source_db: Path
    runtime_db: Path
    cookie_secure: bool
    llm_mode: str
    llm_provider: str
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_headers: dict[str, str]


def get_settings() -> Settings:
    source_value = os.getenv("DATA_PACK_ZIP", "").strip()
    default_zip = ROOT / "data" / "AI Agent Assessment - Candidate Pack.zip"
    source_zip = Path(source_value).expanduser() if source_value else (default_zip if default_zip.exists() else None)
    if source_zip and not source_zip.is_absolute():
        source_zip = ROOT / source_zip
    try:
        headers = json.loads(os.getenv("LLM_EXTRA_HEADERS_JSON", "{}"))
    except json.JSONDecodeError as error:
        raise ValueError("LLM_EXTRA_HEADERS_JSON must be valid JSON.") from error
    if not isinstance(headers, dict):
        raise TypeError("LLM_EXTRA_HEADERS_JSON must be a JSON object.")
    llm_mode = os.getenv("LLM_MODE", "auto").strip().lower()
    if llm_mode not in {"auto", "offline", "provider"}:
        raise ValueError("LLM_MODE must be auto, offline, or provider.")
    environment = os.getenv("APP_ENV", "demo").strip().lower()
    if environment not in {"demo", "production"}:
        raise ValueError("APP_ENV must be demo or production.")
    cookie_secure = os.getenv("APP_COOKIE_SECURE", "false").strip().lower() == "true"
    if environment == "production" and not cookie_secure:
        raise ValueError("APP_COOKIE_SECURE=true is required when APP_ENV=production.")
    return Settings(
        environment=environment,
        source_zip=source_zip,
        source_db=ROOT / "var" / "source.db",
        runtime_db=ROOT / "var" / "runtime.db",
        cookie_secure=cookie_secure,
        llm_mode=llm_mode,
        llm_provider=os.getenv("LLM_PROVIDER", "custom"),
        llm_api_key=(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip() or None,
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-5.5").strip(),
        llm_headers={str(k): str(v) for k, v in headers.items()},
    )
