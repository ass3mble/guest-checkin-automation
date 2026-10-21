from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_tuple_env(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            values.append(int(item))
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    app_name: str
    base_dir: Path
    database_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_webhook_secret: str
    admin_api_key: str
    jotform_webhook_token: str
    ocr_provider_order: tuple[str, ...]
    azure_document_intelligence_endpoint: str
    azure_document_intelligence_key: str
    azure_document_intelligence_api_version: str
    azure_document_intelligence_id_model: str
    azure_document_intelligence_read_model: str
    openai_api_key: str
    openai_model: str
    paddle_ocr_python: str
    paddle_ocr_worker_script: str
    paddle_ocr_lang: str
    paddle_ocr_use_gpu: bool
    app_base_url: str
    review_base_url: str
    jotform_form_url: str
    beds24_api_base_url: str
    beds24_organization: str
    beds24_webhook_url: str
    beds24_api_token: str
    beds24_refresh_token: str
    beds24_invite_code: str
    beds24_approval_info_code: str
    beds24_approval_info_text: str
    beds24_approval_flag_text: str
    beds24_approval_flag_color: str
    beds24_room_labels: dict[str, str]
    ttlock_api_base_url: str
    ttlock_client_id: str
    ttlock_client_secret: str
    ttlock_username: str
    ttlock_password: str
    ttlock_lock_ids: tuple[int, ...]
    ttlock_property_lock_ids: dict[str, list[int]]
    ttlock_code_start_time: str
    ttlock_code_end_time: str
    ttlock_code_length: int
    guest_message_brand: str
    guest_message_wifi_name: str
    guest_message_wifi_password: str
    guest_message_directions_url: str
    sync_google_sheet: bool
    google_sheet_webhook_url: str
    allowed_document_types: tuple[str, ...]
    webhook_max_part_size_mb: int
    ocr_allowed_download_hosts: tuple[str, ...]
    ocr_max_download_mb: int

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("This MVP currently supports sqlite URLs only.")
        return self.base_dir / self.database_url.removeprefix("sqlite:///")


def load_settings() -> Settings:
    _load_env_file(Path.cwd() / ".env")

    allowed_document_types = tuple(
        item.strip()
        for item in os.getenv("ALLOWED_DOCUMENT_TYPES", "passport,visa").split(",")
        if item.strip()
    )
    ocr_provider_order = tuple(
        item.strip().lower()
        for item in os.getenv("OCR_PROVIDER_ORDER", "azure,paddle,openai").split(",")
        if item.strip()
    )
    default_paddle_python = os.getenv("PADDLE_OCR_PYTHON")
    if not default_paddle_python:
        py311 = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe"
        if py311.exists():
            default_paddle_python = str(py311)
        else:
            default_paddle_python = shutil.which("python") or ""
    return Settings(
        app_name=os.getenv("APP_NAME", "guest-checkin-automation"),
        base_dir=Path(os.getenv("APP_BASE_DIR", Path.cwd())).resolve(),
        database_url=os.getenv("DATABASE_URL", "sqlite:///data/checkin.db"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        admin_api_key=os.getenv("ADMIN_API_KEY", ""),
        jotform_webhook_token=os.getenv("JOTFORM_WEBHOOK_TOKEN", ""),
        ocr_provider_order=ocr_provider_order,
        azure_document_intelligence_endpoint=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").rstrip("/"),
        azure_document_intelligence_key=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", ""),
        azure_document_intelligence_api_version=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2023-07-31"),
        azure_document_intelligence_id_model=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ID_MODEL", "prebuilt-idDocument"),
        azure_document_intelligence_read_model=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_READ_MODEL", "prebuilt-read"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        paddle_ocr_python=default_paddle_python,
        paddle_ocr_worker_script=os.getenv("PADDLE_OCR_WORKER_SCRIPT", str((Path(os.getenv("APP_BASE_DIR", Path.cwd())).resolve() / "tools" / "paddle_ocr_worker.py"))),
        paddle_ocr_lang=os.getenv("PADDLE_OCR_LANG", "en"),
        paddle_ocr_use_gpu=_bool_env("PADDLE_OCR_USE_GPU", False),
        app_base_url=os.getenv("APP_BASE_URL", "http://127.0.0.1:8000"),
        review_base_url=os.getenv("REVIEW_BASE_URL", ""),
        jotform_form_url=os.getenv("JOTFORM_FORM_URL", ""),
        beds24_api_base_url=os.getenv("BEDS24_API_BASE_URL", "https://beds24.com/api/v2").rstrip("/"),
        beds24_organization=os.getenv("BEDS24_ORGANIZATION", ""),
        beds24_webhook_url=os.getenv("BEDS24_WEBHOOK_URL", ""),
        beds24_api_token=os.getenv("BEDS24_API_TOKEN", ""),
        beds24_refresh_token=os.getenv("BEDS24_REFRESH_TOKEN", ""),
        beds24_invite_code=os.getenv("BEDS24_INVITE_CODE", ""),
        beds24_approval_info_code=os.getenv("BEDS24_APPROVAL_INFO_CODE", "CHECKIN_STATUS"),
        beds24_approval_info_text=os.getenv("BEDS24_APPROVAL_INFO_TEXT", "approved"),
        beds24_approval_flag_text=os.getenv("BEDS24_APPROVAL_FLAG_TEXT", ""),
        beds24_approval_flag_color=os.getenv("BEDS24_APPROVAL_FLAG_COLOR", ""),
        beds24_room_labels=parse_json_env("BEDS24_ROOM_LABELS", {}) or {},
        ttlock_api_base_url=os.getenv("TTLOCK_API_BASE_URL", "https://euapi.ttlock.com").rstrip("/"),
        ttlock_client_id=os.getenv("TTLOCK_CLIENT_ID", ""),
        ttlock_client_secret=os.getenv("TTLOCK_CLIENT_SECRET", ""),
        ttlock_username=os.getenv("TTLOCK_USERNAME", ""),
        ttlock_password=os.getenv("TTLOCK_PASSWORD", ""),
        ttlock_lock_ids=_int_tuple_env("TTLOCK_LOCK_IDS"),
        ttlock_property_lock_ids=parse_json_env("TTLOCK_PROPERTY_LOCK_IDS", {}) or {},
        ttlock_code_start_time=os.getenv("TTLOCK_CODE_START_TIME", "13:00"),
        ttlock_code_end_time=os.getenv("TTLOCK_CODE_END_TIME", "13:00"),
        ttlock_code_length=max(4, min(9, int(os.getenv("TTLOCK_CODE_LENGTH", "6")))),
        guest_message_brand=os.getenv("GUEST_MESSAGE_BRAND", "Guest Check-in"),
        guest_message_wifi_name=os.getenv("GUEST_MESSAGE_WIFI_NAME", ""),
        guest_message_wifi_password=os.getenv("GUEST_MESSAGE_WIFI_PASSWORD", ""),
        guest_message_directions_url=os.getenv("GUEST_MESSAGE_DIRECTIONS_URL", ""),
        sync_google_sheet=_bool_env("SYNC_GOOGLE_SHEET", False),
        google_sheet_webhook_url=os.getenv("GOOGLE_SHEET_WEBHOOK_URL", ""),
        allowed_document_types=allowed_document_types,
        webhook_max_part_size_mb=max(1, int(os.getenv("WEBHOOK_MAX_PART_SIZE_MB", "25"))),
        ocr_allowed_download_hosts=tuple(
            host.strip().lower() for host in os.getenv(
                "OCR_ALLOWED_DOWNLOAD_HOSTS",
                "jotform.com,jotformusercontent.com,jotformpro.com",
            ).split(",") if host.strip()
        ),
        ocr_max_download_mb=max(1, min(25, int(os.getenv("OCR_MAX_DOWNLOAD_MB", "10")))),
    )


def parse_json_env(name: str, default: dict | list | None = None):
    raw = os.getenv(name)
    if not raw:
        return default
    return json.loads(raw)
