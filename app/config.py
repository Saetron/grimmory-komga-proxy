import os

class Settings:
    GRIMMORY_URL: str = os.getenv("GRIMMORY_URL", "http://localhost:8080").rstrip("/")
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8080"))
    CACHE_TTL: int = int(os.getenv("CACHE_TTL", "300"))
    DEFAULT_USERNAME: str = os.getenv("GRIMMORY_USERNAME", "")
    DEFAULT_PASSWORD: str = os.getenv("GRIMMORY_PASSWORD", "")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", os.path.join(os.getenv("DATA_DIR", "data"), "bridge.db"))
    SYNC_INTERVAL_MINUTES: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "30"))
    SYNC_ON_STARTUP: bool = os.getenv("SYNC_ON_STARTUP", "true").lower() in ("true", "1", "yes")
    SYNC_CONCURRENCY: int = int(os.getenv("SYNC_CONCURRENCY", "6"))

settings = Settings()
