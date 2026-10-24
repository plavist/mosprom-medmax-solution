import os


def gateway_url() -> str:
    return os.getenv("GATEWAY_URL", "http://llm-gateway:8000")


def ingest_url() -> str:
    return os.getenv("INGEST_URL", "http://ingest:8000")


def data_dir() -> str:
    return os.getenv("DATA_DIR", "/data")


def default_mode() -> str:
    return os.getenv("CHANNEL_MODE", "web")  # web | max | both


def max_token() -> str:
    return os.getenv("MAX_BOT_TOKEN", "")


def max_api() -> str:
    return os.getenv("MAX_API_URL", "https://platform-api2.max.ru")


def max_webhook_secret() -> str:
    return os.getenv("MAX_WEBHOOK_SECRET", "")


def min_score() -> float:
    return float(os.getenv("RAG_MIN_SCORE", "0.5"))


def top_k() -> int:
    return int(os.getenv("RAG_TOP_K", "5"))


def max_steps() -> int:
    return int(os.getenv("AGENT_MAX_STEPS", "4"))


def max_transport() -> str:
    """polling — для разработки (GET /updates, публичный адрес не нужен); webhook — для продакшена."""
    return os.getenv("MAX_TRANSPORT", "polling")


def max_ca_bundle() -> str | bool:
    """Путь к CA-bundle с сертификатом Минцифры, либо true/false (проверка TLS)."""
    v = os.getenv("MAX_CA_BUNDLE", "")
    return v if v else os.getenv("MAX_VERIFY_TLS", "true").lower() != "false"


def internal_token() -> str:
    """Токен для админских маршрутов pipeline (их вызывает только сервис admin)."""
    import hashlib
    return hashlib.sha256(("internal:" + os.getenv("ADMIN_PASSWORD", "admin")).encode()).hexdigest()
