import os
import json
from typing import Dict, Tuple

def parse_user_mappings(raw_str: str) -> Dict[str, Tuple[str, str]]:
    """
    Parse USER_MAPPING / USER_MAP environment variable.
    Supports formats:
      - Comma-separated: 'komic_user:grimmory_user:password,user2:grimmory_user2:pwd2'
      - Equal-sign mapping: 'komic_user=grimmory_user:password,user2=grimmory_user2:pwd2'
      - JSON string: '{"komic_user": ["grimmory_user", "password"]}'
    """
    mappings: Dict[str, Tuple[str, str]] = {}
    if not raw_str or not raw_str.strip():
        return mappings
    raw_str = raw_str.strip()
    if raw_str.startswith("{"):
        try:
            data = json.loads(raw_str)
            for k, v in data.items():
                if isinstance(v, list) and len(v) >= 2:
                    mappings[str(k).strip()] = (str(v[0]).strip(), str(v[1]).strip())
                elif isinstance(v, dict):
                    mappings[str(k).strip()] = (
                        str(v.get("username") or v.get("user") or "").strip(),
                        str(v.get("password") or v.get("pwd") or "").strip()
                    )
                elif isinstance(v, str) and ":" in v:
                    u, p = v.split(":", 1)
                    mappings[str(k).strip()] = (u.strip(), p.strip())
            return mappings
        except Exception:
            pass

    entries = [e.strip() for e in raw_str.split(",") if e.strip()]
    for entry in entries:
        if "=" in entry:
            client_user, grimmory_part = entry.split("=", 1)
            client_user = client_user.strip()
            if ":" in grimmory_part:
                g_user, g_pwd = grimmory_part.split(":", 1)
                mappings[client_user] = (g_user.strip(), g_pwd.strip())
        elif entry.count(":") >= 2:
            parts = entry.split(":", 2)
            mappings[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    return mappings

def parse_library_mappings(raw_str: str) -> Dict[str, str]:
    """Parse LIBRARIES / LIBRARY_MAP env var e.g. '14:Manga,16:Comics,25:Novels'."""
    mappings: Dict[str, str] = {}
    if not raw_str or not raw_str.strip():
        return mappings
    for entry in raw_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            k, v = entry.split(":", 1)
            mappings[k.strip()] = v.strip()
        elif "=" in entry:
            k, v = entry.split("=", 1)
            mappings[k.strip()] = v.strip()
    return mappings

class Settings:
    GRIMMORY_URL: str = os.getenv("GRIMMORY_URL", "http://localhost:8080").rstrip("/")
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8080"))
    CACHE_TTL: int = int(os.getenv("CACHE_TTL", "300"))
    DEFAULT_USERNAME: str = (
        os.getenv("GRIMMORY_USERNAME")
        or os.getenv("GRIMMORY_USER")
        or os.getenv("KOMGA_USER")
        or os.getenv("KOMGA_USERNAME")
        or os.getenv("DEFAULT_USERNAME")
        or ""
    )
    DEFAULT_PASSWORD: str = (
        os.getenv("GRIMMORY_PASSWORD")
        or os.getenv("GRIMMORY_PASS")
        or os.getenv("KOMGA_PASSWORD")
        or os.getenv("KOMGA_PASS")
        or os.getenv("DEFAULT_PASSWORD")
        or ""
    )
    USER_MAPPINGS: Dict[str, Tuple[str, str]] = parse_user_mappings(
        os.getenv("USER_MAPPING") or os.getenv("USER_MAPPINGS") or os.getenv("USER_MAP") or ""
    )
    CUSTOM_LIBRARY_NAMES: Dict[str, str] = parse_library_mappings(
        os.getenv("LIBRARIES") or os.getenv("LIBRARY_MAP") or os.getenv("CUSTOM_LIBRARIES") or ""
    )
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info")
    DATA_DIR: str = os.getenv("DATA_DIR", "data")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", os.path.join(DATA_DIR, "bridge.db"))
    THUMBNAILS_DIR: str = os.getenv("THUMBNAILS_DIR", os.path.join(DATA_DIR, "thumbnails"))
    SYNC_INTERVAL_MINUTES: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "30"))
    SYNC_ON_STARTUP: bool = os.getenv("SYNC_ON_STARTUP", "true").lower() in ("true", "1", "yes")
    SYNC_CONCURRENCY: int = int(os.getenv("SYNC_CONCURRENCY", "6"))

settings = Settings()
