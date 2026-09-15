from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .config import load_settings
from .db import Database
from .integrations import Beds24Client, GoogleSheetMirror, OCRProvider, TelegramClient, TTLockClient
from .schemas import BookingInput, BookingView
from .service import CheckinService

# === DIAGNOSTIC LOGGING (for tracking flow, easy to remove later) ===
LOG_PATH = Path("bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8", mode="a"),
    ],
    force=True,
)
diag_logger = logging.getLogger("checkin-bot")


def create_app() -> FastAPI:
    settings = load_settings()
    db = Database(settings)
    db.init()
    service = CheckinService(
        db=db,
        ocr=OCRProvider(settings),
        telegram=TelegramClient(settings),
        beds24=Beds24Client(settings),
        ttlock=TTLockClient(settings),
        sheet_mirror=GoogleSheetMirror(settings),
    )

    app = FastAPI(title=settings.app_name)
    app.state.settings = settings
    app.state.service = service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": settings.app_name,
            "time_utc": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/webhooks/jotform")
    async def jotform_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        client_host = request.client.host if request.client else "-"
        content_type = request.headers.get("content-type", "-")
        diag_logger.info(
            "JOTFORM webhook start request_id=%s client=%s content_type=%s",
            request_id,
            client_host,
            content_type,
        )
        payload = await _read_webhook_payload(request, settings)
        try:
            booking_input = parse_jotform_payload(payload)
            booking_id = await service.ingest_booking(booking_input)
            diag_logger.info(
                "JOTFORM webhook ingested request_id=%s booking_id=%s external_booking_id=%s",
                request_id,
                booking_id,
                booking_input.external_booking_id,
            )
            background_tasks.add_task(service.process_booking_ocr, booking_id)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            diag_logger.info(
                "JOTFORM webhook completed request_id=%s status=accepted booking_id=%s elapsed_ms=%s",
                request_id,
                booking_id,
                elapsed_ms,
            )
            return {"status": "accepted", "booking_id": booking_id}
        except Exception as exc:
            error_id = _write_webhook_error(settings.base_dir, payload, exc)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            diag_logger.exception(
                "JOTFORM webhook failed request_id=%s error_id=%s elapsed_ms=%s",
                request_id,
                error_id,
                elapsed_ms,
            )
            return {"status": "error_logged", "error_id": error_id}

    @app.post("/webhooks/telegram")
    async def telegram_webhook(
        payload: dict[str, Any],
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret.")

        callback_query = payload.get("callback_query")
        if callback_query and callback_query.get("data"):
            callback_chat = (callback_query.get("message") or {}).get("chat", {})
            diag_logger.info(
                "TELEGRAM callback chat_id=%s type=%s title=%s",
                callback_chat.get("id"),
                callback_chat.get("type"),
                callback_chat.get("title") or callback_chat.get("username"),
            )
            parsed = service.telegram.parse_callback(callback_query["data"])
            actor = callback_query.get("from", {}).get("username") or str(callback_query.get("from", {}).get("id", "telegram"))
            message = await service.apply_review_action(parsed.booking_id, parsed.action, actor)
            await service.telegram.answer_callback(callback_query["id"], message)
            await service.telegram.send_text_message(f"{message}\nBooking: {parsed.booking_id}\nBy: {actor}")
            return {"status": "ok"}

        message = payload.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if chat_id:
            diag_logger.info(
                "TELEGRAM message chat_id=%s type=%s title=%s text=%s",
                chat_id,
                message.get("chat", {}).get("type"),
                message.get("chat", {}).get("title") or message.get("chat", {}).get("username"),
                text[:120],
            )
        if text.startswith("/") and chat_id:
            actor = message.get("from", {}).get("username") or str(message.get("from", {}).get("id", "telegram"))
            reply = await service.handle_telegram_command(text=text, actor=actor)
            await service.telegram.send_chat_message(str(chat_id), reply)
            return {"status": "ok"}

        return {"status": "ignored"}

    @app.get("/bookings/{booking_id}", response_model=BookingView)
    async def get_booking(booking_id: str) -> BookingView:
        try:
            return service.get_booking_view(booking_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/bookings/{booking_id}/process-ocr")
    async def process_ocr(booking_id: str) -> dict[str, str]:
        await service.process_booking_ocr(booking_id)
        return {"status": "processed", "booking_id": booking_id}

    @app.post("/bookings/{booking_id}/review/send")
    async def review_send(booking_id: str) -> dict[str, str]:
        await service.send_review_message(booking_id)
        return {"status": "sent", "booking_id": booking_id}

    @app.post("/bookings/{booking_id}/sheet/sync")
    async def sync_sheet(booking_id: str) -> dict[str, str]:
        await service.sync_booking_to_sheet(booking_id)
        return {"status": "synced", "booking_id": booking_id}

    @app.post("/bookings/{booking_id}/beds24/refresh")
    async def refresh_beds24_booking(booking_id: str) -> dict[str, Any]:
        try:
            beds24_booking = await service.refresh_beds24_booking(booking_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "ok", "booking_id": booking_id, "beds24_booking": beds24_booking}

    @app.post("/beds24/retry-unmatched")
    async def retry_unmatched_beds24() -> dict[str, int | str]:
        result = await service.retry_unmatched_beds24_bookings()
        return {"status": "ok", **result}

    return app


async def _read_webhook_payload(request: Request, settings) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception as exc:
            return {"rawBody": (await request.body()).decode("utf-8", errors="replace"), "_json_error": str(exc)}

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        try:
            form = await request.form(max_part_size=settings.webhook_max_part_size_mb * 1024 * 1024)
            return {key: await _coerce_form_value(value) for key, value in form.multi_items()}
        except Exception as exc:
            raw_body = await request.body()
            decoded = raw_body.decode("utf-8", errors="replace")
            result = {
                "rawBody": decoded,
                "_form_error": str(exc),
                "_content_type": content_type,
            }
            # Try to recover whatever we can even on form parse failure
            try:
                from urllib.parse import parse_qs
                for k, v in parse_qs(decoded).items():
                    if k not in result:
                        result[k] = v[0] if v else ""
            except:
                pass
            return result

    raw_body = await request.body()
    if not raw_body:
        return {}
    try:
        decoded = raw_body.decode("utf-8")
        return json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"rawBody": raw_body.decode("utf-8", errors="replace")}


async def _coerce_form_value(value: Any) -> Any:
    if hasattr(value, "filename"):
        filename = getattr(value, "filename", "") or ""
        content_type = str(getattr(value, "content_type", "") or "").split(";", 1)[0].strip().lower()
        if hasattr(value, "read"):
            body = await value.read()
            if body and content_type.startswith("image/"):
                encoded = base64.b64encode(body).decode("ascii")
                return f"data:{content_type};base64,{encoded}"
        return filename
    return value


def _write_webhook_error(base_dir: Path, payload: dict[str, Any], exc: Exception) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    error_id = f"jotform-error-{timestamp}"
    error_dir = base_dir / "data" / "webhook-errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    (error_dir / f"{error_id}.json").write_text(
        json.dumps(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "payload_keys": sorted(payload.keys()),
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return error_id


def parse_jotform_payload(payload: dict[str, Any]) -> BookingInput:
    if "booking" in payload and "guests" in payload:
        booking = payload["booking"]
        return BookingInput.model_validate(
            {
                "external_booking_id": booking["external_booking_id"],
                "jotform_submission_id": payload.get("submission_id"),
                "lead_guest_name": booking.get("lead_guest_name"),
                "guest_count": booking.get("guest_count", len(payload["guests"])),
                "guests": payload["guests"],
                "raw_payload": payload,
            }
        )

    raw_request = payload.get("rawRequest")
    if isinstance(raw_request, str):
        try:
            raw_request_payload = json.loads(raw_request)
        except json.JSONDecodeError:
            raw_request_payload = {}
    elif isinstance(raw_request, dict):
        raw_request_payload = raw_request
    else:
        raw_request_payload = {}

    form_data = payload.get("formData", payload)
    pretty_payload = _extract_pretty_payload(payload.get("rawBody"))
    merged_payload = {**form_data, **raw_request_payload, **pretty_payload}

    # Recovery for malformed webhook payloads (e.g. _form_error with only rawBody)
    # This happens sometimes with certain Jotform submissions or content-types.
    # Try to recover fields from rawBody (as JSON or urlencoded).
    parse_source = "form"
    if ("_form_error" in payload or "_json_error" in payload) and "rawBody" in payload:
        raw_body = payload["rawBody"]
        parse_source = "rawBody_recovery"

        # Save rawBody to file for deep debugging (as requested)
        try:
            debug_path = Path("data/last_jotform_rawbody.txt")
            debug_path.write_text(raw_body, encoding="utf-8", errors="replace")
            diag_logger.warning(f"Saved full rawBody ({len(raw_body)} chars) to {debug_path} for inspection")
        except Exception as e:
            diag_logger.error(f"Failed to save rawBody: {e}")

        # Save the whole error payload as JSON too
        try:
            err_payload = dict(payload)
            err_payload["_raw_body_length"] = len(raw_body)
            err_path = Path("data/last_jotform_error_payload.json")
            err_path.write_text(json.dumps(err_payload, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

        try:
            recovered = json.loads(raw_body)
            if isinstance(recovered, dict):
                merged_payload = {**recovered, **merged_payload}
        except (json.JSONDecodeError, TypeError):
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(raw_body)
                recovered = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
                merged_payload = {**recovered, **merged_payload}
            except Exception:
                pass

        # 1. name="key"\nvalue  (multipart text parts)
        try:
            for m in re.finditer(r'name="([^"]+)"\s*\r?\n\s*([^\r\n\-]{1,200})', raw_body):
                k, v = m.group(1).strip(), m.group(2).strip()
                if k and v and k not in merged_payload:
                    merged_payload[k] = v
        except Exception:
            pass

        # 2. key=VALUE anywhere (urlencoded style)
        for key in ["q32_BOOKINGID", "BOOKINGID", "booking_id", "bookingId", "external_booking_id",
                    "q38_fullName38", "q39_fullName39", "q20_birthDate", "q30_idFrontPhoto", "q31_idBackPhoto"]:
            match = re.search(rf'{key}=([^&\s"\'\n\r]+)', raw_body, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                if val and val.lower() != "unknown-booking":
                    merged_payload[key] = _normalize_jotform_value(val)

        # 3. rawRequest JSON (very common in Jotform)
        try:
            m = re.search(r'"rawRequest"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_body)
            if m:
                raw_req = m.group(1).replace('\\"', '"').replace('\\/', '/').replace('\\n', '\n')
                j = json.loads(raw_req)
                if isinstance(j, dict):
                    merged_payload = {**j, **merged_payload}
        except Exception:
            pass

        # 4. Any q-prefixed field (Mandala form style)
        try:
            for m in re.finditer(r'(q\d+_[a-zA-Z0-9_]+)=([^&\s"\'\n\r]+)', raw_body):
                k, v = m.group(1), m.group(2).strip()
                if k not in merged_payload:
                    merged_payload[k] = _normalize_jotform_value(v)
        except Exception:
            pass

        diag_logger.info(f"rawBody recovery done. Keys now in merged_payload: {list(merged_payload.keys())[:12]}")
        if not any(k in merged_payload for k in ["BOOKINGID", "q32_BOOKINGID"]):
            diag_logger.warning("Still no BOOKINGID/q32_BOOKINGID after aggressive recovery. Inspect data/last_jotform_rawbody.txt")

    booking_id_hint = _extract_booking_id(merged_payload)
    if booking_id_hint or _read_any(merged_payload, ["q27_roomName", "q5_numGuest", "reservation_code", "Reservation Code"]):
        return _parse_mandala_jotform_payload(payload, merged_payload)

    guest_count = _safe_int(merged_payload.get("guest_count") or merged_payload.get("number_of_guests"), default=1)
    guests: list[dict[str, Any]] = []
    for index in range(1, guest_count + 1):
        guest = {
            "guest_index": index,
            "full_name": _read_any(merged_payload, [f"guest_{index}_full_name", f"guest{index}_full_name", f"full_name_{index}"]),
            "date_of_birth": _read_any(merged_payload, [f"guest_{index}_date_of_birth", f"guest{index}_dob", f"date_of_birth_{index}"]),
            "gender": _read_any(merged_payload, [f"guest_{index}_gender", f"gender_{index}"]),
            "nationality": _read_any(merged_payload, [f"guest_{index}_nationality", f"nationality_{index}"]),
            "passport_number": _read_any(merged_payload, [f"guest_{index}_passport_number"]),
            "visa_number": _read_any(merged_payload, [f"guest_{index}_visa_number"]),
            "documents": [],
        }
        passport_url = _read_any(merged_payload, [f"guest_{index}_passport_url", f"passport_url_{index}", f"guest{index}_passport"])
        visa_url = _read_any(merged_payload, [f"guest_{index}_visa_url", f"visa_url_{index}", f"guest{index}_visa"])
        if passport_url:
            guest["documents"].append({"doc_type": "passport", "source_url": passport_url})
        if visa_url:
            guest["documents"].append({"doc_type": "visa", "source_url": visa_url})
        if any([guest["full_name"], passport_url, visa_url]):
            guests.append(guest)

    external_booking_id = _extract_booking_id(merged_payload) or payload.get("submission_id") or "unknown-booking"
    if external_booking_id == "unknown-booking":
        diag_logger.warning(
            "Parser fell back to 'unknown-booking'. "
            f"submission_id={payload.get('submissionID') or payload.get('submission_id')}. "
            f"Available keys sample: {sorted(list(merged_payload.keys()))[:15]}"
        )
    lead_guest_name = _read_any(merged_payload, ["lead_guest_name", "main_guest_name", "guest_1_full_name"])

    return BookingInput.model_validate(
        {
            "external_booking_id": str(external_booking_id),
            "jotform_submission_id": _submission_id(payload),
            "lead_guest_name": lead_guest_name,
            "guest_count": guest_count,
            "guests": guests,
            "raw_payload": {
                "webhook": payload,
                "form": merged_payload,
                "parse_debug": {
                    "parse_source": parse_source,
                    "has_raw_body": bool(payload.get("rawBody")),
                    "raw_body_length": len(payload.get("rawBody") or "") if isinstance(payload.get("rawBody"), str) else None,
                },
            },
        }
    )


def _parse_mandala_jotform_payload(payload: dict[str, Any], form_data: dict[str, Any]) -> BookingInput:
    guest_count = _safe_int(_read_any(form_data, ["guest_count", "q5_numGuest", "number_of_guests"]), default=1)
    guest_count = min(max(guest_count, 1), 4)

    guests: list[dict[str, Any]] = []
    for guest_index in range(1, guest_count + 1):
        full_name = _read_any(
            form_data,
            [
                f"guest_{guest_index}_full_name",
                f"full_name_{guest_index}",
                "guest_1_full_name" if guest_index == 1 else None,
                "q38_fullName38" if guest_index == 1 else None,
                "q39_fullName39" if guest_index == 1 else None,
                "q39_fullName39" if guest_index == 2 else None,
            ],
        )
        date_of_birth = _format_jotform_date(form_data.get(f"guest_{guest_index}_date_of_birth")) or _parse_pretty_date(
            _read_any(
                form_data,
                [
                    f"guest_{guest_index}_date_of_birth",
                    f"date_of_birth_{guest_index}",
                    "q20_birthDate" if guest_index == 1 else None,
                    "q34_birthDate34" if guest_index == 2 else None,
                ],
            )
        )
        phone_number = _read_any(
            form_data,
            [
                f"guest_{guest_index}_phone_number",
                f"phone_number_{guest_index}",
                "q23_mobile" if guest_index == 1 else None,
                "q35_phoneNumber" if guest_index == 2 else None,
            ],
        )
        passport_image = _normalize_jotform_value(
            _read_any(
                form_data,
                [
                    f"guest_{guest_index}_passport_url",
                    f"passport_url_{guest_index}",
                    "q30_idFrontPhoto" if guest_index == 1 else None,
                    "q36_photoOf" if guest_index == 2 else None,
                ],
            )
        )
        visa_image = _normalize_jotform_value(
            _read_any(
                form_data,
                [
                    f"guest_{guest_index}_visa_url",
                    f"visa_url_{guest_index}",
                    "q31_idBackPhoto" if guest_index == 1 else None,
                    "q37_photoOf37" if guest_index == 2 else None,
                ],
            )
        )

        guest_payload = {
            "guest_index": guest_index,
            "full_name": full_name,
            "date_of_birth": date_of_birth,
            "gender": None,
            "nationality": None,
            "passport_number": None,
            "visa_number": None,
            "documents": [],
            "phone_number": phone_number,
        }

        if passport_image:
            guest_payload["documents"].append({"doc_type": "passport", "source_url": passport_image})
        if visa_image:
            guest_payload["documents"].append({"doc_type": "visa", "source_url": visa_image})

        if any([full_name, date_of_birth, phone_number, passport_image, visa_image]):
            guests.append(guest_payload)

    # Migration-safe priority:
    # 1. explicit Beds24 keys
    # 2. legacy Mandala/Jotform BOOKINGID keys
    # 3. generic extractor / submission id fallback
    beds24_booking_id = _read_any(form_data, ["beds24_booking_id", "beds24BookingId", "q33_beds24BookingId"])
    beds24_api_reference = _read_any(
        form_data,
        ["beds24_api_reference", "beds24ApiReference", "api_reference", "apiReference", "q34_beds24ApiReference"],
    )
    legacy_booking_id = (
        _read_any(form_data, ["q32_BOOKINGID"]) or
        _read_any(form_data, ["BOOKINGID", "booking_id", "bookingId", "external_booking_id", "reservation_code", "Reservation Code"]) or
        _read_any(form_data, ["pretty_booking_code"])
    )
    booking_id = beds24_booking_id or beds24_api_reference or legacy_booking_id or _extract_booking_id(form_data)

    booking_metadata = {
        "booking_id": booking_id,
        "beds24_booking_id": beds24_booking_id,
        "beds24_api_reference": beds24_api_reference,
        "legacy_booking_id": legacy_booking_id,
        "room_name": _read_any(form_data, ["q27_roomName"]),
        "check_in": _format_jotform_date(form_data.get("q26_firstNight")),
        "check_out": _format_jotform_date(form_data.get("q19_leavingDay")),
        "guest_count": guest_count,
        "email": _read_any(form_data, ["q14_email"]),
        "terms_accepted": _read_any(form_data, ["q11_pleaseSign"]),
        "signature": _read_any(form_data, ["q10_signature"]),
        "form_id": payload.get("formID") or form_data.get("formID"),
        "submission_id": payload.get("submissionID") or payload.get("submission_id") or form_data.get("submissionID"),
        "username": payload.get("username") or form_data.get("username"),
        "ip": _read_any(form_data, ["ip"]),
        "parse_source": "pretty_rawBody" if payload.get("rawBody") else "form",
        "has_raw_body": bool(payload.get("rawBody")),
        "raw_body_length": len(payload.get("rawBody") or "") if isinstance(payload.get("rawBody"), str) else None,
    }

    external_booking_id = str(booking_id or booking_metadata["submission_id"] or "unknown-booking")
    if external_booking_id == "unknown-booking":
        diag_logger.warning(
            "Mandala parser fell back to 'unknown-booking'. "
            f"q32_BOOKINGID={_read_any(form_data, ['q32_BOOKINGID'])}, "
            f"BOOKINGID from URL/generic={_read_any(form_data, ['BOOKINGID']) or _extract_booking_id(form_data)}, "
            f"submission_id={booking_metadata.get('submission_id')}. "
            f"Keys sample: {sorted([k for k in form_data if not k.startswith('q') or k in ('q32_BOOKINGID', 'q5_numGuest')])[:10]}"
        )

    return BookingInput.model_validate(
        {
            "external_booking_id": external_booking_id,
            "jotform_submission_id": _submission_id(payload),
            "lead_guest_name": guests[0]["full_name"] if guests else None,
            "guest_count": guest_count,
            "guests": guests,
            "raw_payload": {
                "webhook": payload,
                "form": form_data,
                "pretty": _extract_pretty_payload(payload.get("rawBody")),
                "booking_metadata": booking_metadata,
                "parse_debug": {
                    "parse_source": booking_metadata["parse_source"],
                    "has_raw_body": booking_metadata["has_raw_body"],
                    "raw_body_length": booking_metadata["raw_body_length"],
                },
            },
        }
    )


def _read_any(payload: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key is None:
            continue
        value = payload.get(key)
        if value not in (None, "", []):
            return value
    return None


def _format_jotform_date(value: Any) -> str | None:
    if isinstance(value, str):
        parsed = _parse_pretty_date(value)
        return parsed or value or None
    if not isinstance(value, dict):
        return None
    day = str(value.get("day") or "").strip()
    month = str(value.get("month") or "").strip()
    year = str(value.get("year") or "").strip()
    if not any([day, month, year]):
        return None
    if day and month and year:
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return "-".join(part for part in [year, month.zfill(2) if month else "", day.zfill(2) if day else ""] if part)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _submission_id(payload: dict[str, Any]) -> str | None:
    submission_id = payload.get("submissionID") or payload.get("submission_id")
    if submission_id in (None, ""):
        return None
    return str(submission_id)


def _extract_booking_id(payload: dict[str, Any]) -> str | None:
    candidate_keys = [
        "beds24_booking_id",
        "beds24BookingId",
        "beds24_api_reference",
        "beds24ApiReference",
        "api_reference",
        "apiReference",
        "q32_BOOKINGID",
        "BOOKINGID",
        "bookingCode",
        "booking_code",
        "booking_id",
        "bookingId",
        "booking_reference",
        "bookingReference",
        "reservation_code",
        "reservationCode",
        "Reservation Code",
        "reservation_id",
        "reservationId",
        "external_booking_id",
        "externalBookingId",
        "q1_bookingId",
    ]
    for key in candidate_keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return str(value)

    for key, value in payload.items():
        if not isinstance(key, str) or value in (None, "", [], {}):
            continue
        normalized = key.lower()
        if normalized.endswith("bookingid") or normalized.endswith("booking_id") or normalized.endswith("reservationid"):
            return str(value)
    return None


def _extract_pretty_payload(raw_body: Any) -> dict[str, Any]:
    if not isinstance(raw_body, str) or not raw_body:
        return {}

    pretty_text = _extract_pretty_text(raw_body)
    if not pretty_text:
        return {}

    pretty_fields: dict[str, Any] = {}

    booking_code = _match_group(pretty_text, r"Reservation Code:([^,\r\n]+)")
    if booking_code:
        pretty_fields["BOOKINGID"] = booking_code
        pretty_fields["reservation_code"] = booking_code

    guest_count = _match_group(pretty_text, r"Number of Guests:([^,\r\n]+)")
    if guest_count:
        pretty_fields["guest_count"] = guest_count
        pretty_fields["number_of_guests"] = guest_count

    email = _match_group(pretty_text, r"Email::?([^,\r\n]+)")
    if email:
        pretty_fields["q14_email"] = email
        pretty_fields["email"] = email

    signature = _match_group(pretty_text, r"Guest Signature \(Representative\):([^,\r\n]+)")
    if signature:
        pretty_fields["signature"] = _normalize_jotform_value(signature)

    terms = _match_group(pretty_text, r"Please Sign Terms and Conditions:([^,\r\n]+)")
    if terms:
        pretty_fields["q11_pleaseSign"] = terms

    for guest_idx, full_name in re.findall(r"Full Name - Guest (\d+):([^,\r\n]+)", pretty_text):
        pretty_fields[f"guest_{guest_idx}_full_name"] = full_name.strip()
    for guest_idx, dob in re.findall(r"Birth Date - Guest (\d+):([^,\r\n]+)", pretty_text):
        pretty_fields[f"guest_{guest_idx}_date_of_birth"] = _parse_pretty_date(dob)
    for guest_idx, phone in re.findall(r"Phone Number - Guest (\d+):([^,\r\n]+)", pretty_text):
        pretty_fields[f"guest_{guest_idx}_phone_number"] = phone.strip()
    for guest_idx, passport in re.findall(r"Photo of Passport Main Page - Guest (\d+):([^,\r\n]+)", pretty_text):
        pretty_fields[f"guest_{guest_idx}_passport_url"] = _normalize_jotform_value(passport)
    for guest_idx, visa in re.findall(r"Photo of Visa(?: Entry Stamp)? - Guest (\d+):([^,\r\n]+)", pretty_text):
        pretty_fields[f"guest_{guest_idx}_visa_url"] = _normalize_jotform_value(visa)

    return pretty_fields


def _extract_pretty_text(raw_body: str) -> str:
    match = re.search(
        r'name="pretty"\s*\r?\n(?:\r?\n)?(?P<pretty>.*?)(?:\r?\n--------------------------|\Z)',
        raw_body,
        re.DOTALL,
    )
    if match:
        return match.group("pretty").strip()
    return ""


def _match_group(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip() or None


def _parse_pretty_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = [part for part in re.split(r"[./\-\s]+", text) if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return text
    if len(parts[0]) == 4:
        year, month, day = parts
        return f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"
    day, month, year = parts
    if len(year) == 2:
        year = f"20{year}"
    return f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"


def _normalize_jotform_value(value: Any) -> Any:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return text
    if text.startswith("http://") or text.startswith("https://") or text.startswith("data:"):
        return text
    data_url = _try_build_image_data_url(text)
    if data_url:
        return data_url
    match = re.search(r"(uploads/[^\"\s<>]+)", text)
    if match:
        return f"https://www.jotform.com/{match.group(1).lstrip('/')}"
    return text


def _try_build_image_data_url(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text).replace("\\/", "/")
    if len(compact) < 256:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return None
    try:
        blob = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return None
    mime_type = _guess_image_mime_type(blob)
    if not mime_type:
        return None
    return f"data:{mime_type};base64,{compact}"


def _guess_image_mime_type(blob: bytes) -> str | None:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if blob.startswith(b"RIFF") and b"WEBP" in blob[:16]:
        return "image/webp"
    return None


app = create_app()
