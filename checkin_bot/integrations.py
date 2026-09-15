from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .config import Settings
from .schemas import OCRExtractionResult, TelegramCallbackPayload

# Diagnostic logger (configured in app.py)
diag_logger = logging.getLogger("checkin-bot")


class OCRProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def extract_document(self, *, doc_type: str, source_url: str) -> tuple[str, dict[str, Any], OCRExtractionResult]:
        image_base64 = await self._build_image_base64(source_url)
        image_data_url = f"data:image/jpeg;base64,{image_base64}"
        errors: list[str] = []

        diag_logger.info(f"OCR extract start doc_type={doc_type} providers_order={self.settings.ocr_provider_order}")

        for provider_name in self.settings.ocr_provider_order:
            if provider_name == "azure" and self._azure_enabled():
                diag_logger.info(f"  trying azure for {doc_type}...")
                try:
                    async with httpx.AsyncClient(timeout=90) as client:
                        raw = await self._analyze_with_azure(
                            client,
                            model_id=self._model_for_doc_type(doc_type),
                            image_base64=image_base64,
                        )
                    extracted = OCRExtractionResult.model_validate(self._extract_azure_fields(doc_type=doc_type, raw=raw))
                    diag_logger.info(f"  azure SUCCEEDED for {doc_type}")
                    return "azure-document-intelligence", raw, extracted
                except Exception as exc:
                    errors.append(f"azure: {exc}")
                    diag_logger.warning(f"  azure FAILED for {doc_type}: {str(exc)[:120]}")
                    continue

            if provider_name == "paddle" and self._paddle_enabled():
                diag_logger.info(f"  trying paddle for {doc_type} (subprocess)...")
                try:
                    raw = await self._analyze_with_paddle(doc_type=doc_type, image_base64=image_base64)
                    extracted = OCRExtractionResult.model_validate(self._extract_paddle_fields(doc_type=doc_type, raw=raw))
                    diag_logger.info(f"  paddle SUCCEEDED for {doc_type}")
                    return "paddleocr", raw, extracted
                except Exception as exc:
                    errors.append(f"paddle: {exc}")
                    diag_logger.warning(f"  paddle FAILED for {doc_type}: {str(exc)[:120]}")
                    continue

            if provider_name == "openai" and self._openai_enabled():
                diag_logger.info(f"  trying openai for {doc_type}...")
                try:
                    async with httpx.AsyncClient(timeout=90) as client:
                        raw = await self._analyze_with_openai(client, doc_type=doc_type, image_data_url=image_data_url)
                    extracted = OCRExtractionResult.model_validate(_extract_openai_output(raw))
                    diag_logger.info(f"  openai SUCCEEDED for {doc_type}")
                    return "openai", raw, extracted
                except Exception as exc:
                    errors.append(f"openai: {exc}")
                    diag_logger.warning(f"  openai FAILED for {doc_type}: {str(exc)[:120]}")
                    continue

        if not errors:
            raise RuntimeError("No OCR provider is configured. Set OCR_PROVIDER_ORDER and provider credentials.")
        diag_logger.error(f"OCR extract ALL providers failed for {doc_type}: {' | '.join(errors)[:200]}")
        raise RuntimeError(" | ".join(errors))

    async def _analyze_with_paddle(self, *, doc_type: str, image_base64: str) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            self.settings.paddle_ocr_python,
            self.settings.paddle_ocr_worker_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = {
            "doc_type": doc_type,
            "image_base64": image_base64,
            "lang": self.settings.paddle_ocr_lang,
            "use_gpu": self.settings.paddle_ocr_use_gpu,
        }
        stdout, stderr = await process.communicate(json.dumps(payload).encode("utf-8"))
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error_text or f"PaddleOCR worker exited with code {process.returncode}.")
        try:
            raw = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"PaddleOCR returned invalid JSON: {exc}") from exc
        if not raw.get("success", False):
            raise RuntimeError(str(raw.get("error") or "PaddleOCR worker failed."))
        return raw

    async def _build_image_base64(self, source_url: str) -> str:
        if source_url.startswith("data:image/"):
            return self._normalize_data_image(source_url)

        async with httpx.AsyncClient(timeout=60) as client:
            source_response = await client.get(source_url, follow_redirects=True)
            source_response.raise_for_status()
            content_type = source_response.headers.get("content-type", "image/jpeg")
            return self._normalize_binary_image(source_response.content, content_type)

    async def _analyze_with_azure(self, client: httpx.AsyncClient, *, model_id: str, image_base64: str) -> dict[str, Any]:
        analyze_url = (
            f"{self.settings.azure_document_intelligence_endpoint}/formrecognizer/documentModels/"
            f"{model_id}:analyze?api-version={self.settings.azure_document_intelligence_api_version}"
        )
        response = await self._post_with_retry(
            client,
            url=analyze_url,
            json_payload={"base64Source": image_base64},
        )
        operation_location = response.headers.get("operation-location")
        if not operation_location:
            raise RuntimeError("Azure Document Intelligence did not return operation-location.")
        return await self._poll_azure_result(client, operation_location)

    async def _analyze_with_openai(self, client: httpx.AsyncClient, *, doc_type: str, image_data_url: str) -> dict[str, Any]:
        payload = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extract identity data from a hospitality guest document image and return JSON only. "
                                "The document may be a passport main page or a visa / entry stamp page. "
                                "Use visible text and MRZ when present. Do not guess. "
                                "If a field is not clearly visible, return null. "
                                "Normalize dates to YYYY-MM-DD when possible. "
                                "For visa or entry-stamp images, use visa_number only if an actual visa/reference number is visible; "
                                "otherwise leave it null and mention useful context in notes. "
                                "Keys: full_name, passport_number, visa_number, date_of_birth, gender, nationality, expiry_date, confidence, notes. "
                                f"Document type: {doc_type}."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "passport_extraction",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "full_name": {"type": ["string", "null"]},
                            "passport_number": {"type": ["string", "null"]},
                            "visa_number": {"type": ["string", "null"]},
                            "date_of_birth": {"type": ["string", "null"]},
                            "gender": {"type": ["string", "null"]},
                            "nationality": {"type": ["string", "null"]},
                            "expiry_date": {"type": ["string", "null"]},
                            "confidence": {"type": ["number", "null"]},
                            "notes": {"type": ["string", "null"]},
                        },
                        "required": [
                            "full_name",
                            "passport_number",
                            "visa_number",
                            "date_of_birth",
                            "gender",
                            "nationality",
                            "expiry_date",
                            "confidence",
                            "notes",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        }
        response = await self._post_with_retry(
            client,
            url="https://api.openai.com/v1/responses",
            json_payload=payload,
            headers_override={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        return response.json()

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        json_payload: dict[str, Any],
        headers_override: dict[str, str] | None = None,
    ) -> httpx.Response:
        backoff_seconds = [2, 5, 10]
        last_error: Exception | None = None
        for attempt in range(len(backoff_seconds) + 1):
            try:
                api_response = await client.post(
                    url,
                    headers=headers_override or self._azure_headers(),
                    json=json_payload,
                )
                if api_response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = api_response.headers.get("retry-after")
                    if attempt < len(backoff_seconds):
                        delay = float(retry_after) if retry_after else backoff_seconds[attempt]
                        await asyncio.sleep(delay)
                        continue
                api_response.raise_for_status()
                return api_response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= len(backoff_seconds):
                    break
                await asyncio.sleep(backoff_seconds[attempt])
        if last_error is not None:
            raise last_error
        raise RuntimeError("Azure OCR request failed without a captured error.")

    def _normalize_data_image(self, source_url: str) -> str:
        header, encoded = source_url.split(",", 1)
        mime_type = header.split(";", 1)[0].removeprefix("data:")
        image_bytes = base64.b64decode(encoded)
        return self._normalize_binary_image(image_bytes, mime_type)

    def _normalize_binary_image(self, image_bytes: bytes, mime_type: str) -> str:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            image = _flatten_to_rgb(image)
            image = _resize_for_ocr(image)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=72, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    async def _poll_azure_result(self, client: httpx.AsyncClient, operation_location: str) -> dict[str, Any]:
        for _ in range(30):
            response = await client.get(operation_location, headers=self._azure_headers())
            response.raise_for_status()
            payload = response.json()
            status = str(payload.get("status", "")).lower()
            if status == "succeeded":
                return payload
            if status == "failed":
                error_message = payload.get("error", {}).get("message") or payload.get("message") or "Azure OCR failed."
                raise RuntimeError(error_message)
            await asyncio.sleep(1.2)
        raise RuntimeError("Azure OCR polling timed out.")

    def _azure_headers(self) -> dict[str, str]:
        return {
            "Ocp-Apim-Subscription-Key": self.settings.azure_document_intelligence_key,
            "Content-Type": "application/json",
        }

    def _model_for_doc_type(self, doc_type: str) -> str:
        if doc_type == "passport":
            return self.settings.azure_document_intelligence_id_model
        return self.settings.azure_document_intelligence_read_model

    def _azure_enabled(self) -> bool:
        return bool(self.settings.azure_document_intelligence_endpoint and self.settings.azure_document_intelligence_key)

    def _openai_enabled(self) -> bool:
        return bool(self.settings.openai_api_key and self.settings.openai_model)

    def _paddle_enabled(self) -> bool:
        return bool(
            self.settings.paddle_ocr_python
            and self.settings.paddle_ocr_worker_script
            and Path(self.settings.paddle_ocr_worker_script).exists()
        )

    def _extract_azure_fields(self, *, doc_type: str, raw: dict[str, Any]) -> dict[str, Any]:
        if doc_type == "passport":
            return _extract_azure_passport_fields(raw)
        return _extract_azure_visa_fields(raw)

    def _extract_paddle_fields(self, *, doc_type: str, raw: dict[str, Any]) -> dict[str, Any]:
        if doc_type == "passport":
            return _extract_paddle_passport_fields(raw)
        return _extract_paddle_visa_fields(raw)


def _extract_azure_passport_fields(raw: dict[str, Any]) -> dict[str, Any]:
    documents = raw.get("analyzeResult", {}).get("documents") or []
    if not documents:
        content = str(raw.get("analyzeResult", {}).get("content") or "")
        fallback = _extract_passport_fields_from_content(content)
        if any(fallback.get(key) for key in ("passport_number", "date_of_birth", "gender", "nationality")):
            return fallback
        raise ValueError("Azure OCR did not return structured passport fields.")

    fields = documents[0].get("fields") or {}
    first_name = _azure_field_value(fields.get("FirstName"))
    middle_name = _azure_field_value(fields.get("MiddleName"))
    last_name = _azure_field_value(fields.get("LastName"))
    full_name = " ".join(part for part in [first_name, middle_name, last_name] if part) or None
    content = str(raw.get("analyzeResult", {}).get("content") or "")
    fallback = _extract_passport_fields_from_content(content)

    used_confidences = [
        _azure_field_confidence(fields.get("DocumentNumber")),
        _azure_field_confidence(fields.get("DateOfBirth")),
        _azure_field_confidence(fields.get("Sex")),
        _azure_field_confidence(fields.get("Nationality")),
        _azure_field_confidence(fields.get("CountryRegion")),
    ]
    confidence_values = [value for value in used_confidences if value is not None]

    return {
        "full_name": full_name or fallback.get("full_name"),
        "passport_number": _azure_field_value(fields.get("DocumentNumber")) or fallback.get("passport_number"),
        "visa_number": None,
        "date_of_birth": _normalize_loose_date(_azure_field_value(fields.get("DateOfBirth"))) or fallback.get("date_of_birth"),
        "gender": _normalize_gender(_azure_field_value(fields.get("Sex"))) or fallback.get("gender"),
        "nationality": _azure_field_value(fields.get("Nationality")) or _azure_field_value(fields.get("CountryRegion")) or fallback.get("nationality"),
        "expiry_date": _normalize_loose_date(_azure_field_value(fields.get("DateOfExpiration"))) or fallback.get("expiry_date"),
        "confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else None,
        "notes": documents[0].get("docType"),
    }


def _extract_azure_visa_fields(raw: dict[str, Any]) -> dict[str, Any]:
    content = str(raw.get("analyzeResult", {}).get("content") or "")
    compact = " ".join(content.split())

    passport_number = _search_labeled_identifier(
        compact,
        labels=[
            r"passport\s*(?:no\.?|number|nr\.?|id|document\s*no\.?)",
            r"document\s*(?:no\.?|number|nr\.?)",
        ],
    ) or _search_first(
        compact,
        [
            r"\b[A-Z][0-9]{7,8}\b",
            r"\b[A-Z0-9]{8,12}\b",
        ],
    )
    visa_number = _search_labeled_identifier(
        compact,
        labels=[
            r"visa\s*(?:no\.?|number|nr\.?|ref(?:erence)?)",
            r"reference\s*(?:no\.?|number|nr\.?)",
        ],
    )
    date_of_birth = _search_first(
        compact,
        [
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{2}[\/\-]\d{2}[\/\-]\d{4}\b",
        ],
    )
    normalized_dob = _normalize_loose_date(date_of_birth)

    return {
        "full_name": None,
        "passport_number": passport_number,
        "visa_number": visa_number,
        "date_of_birth": normalized_dob,
        "gender": None,
        "nationality": None,
        "expiry_date": None,
        "confidence": None,
        "notes": compact[:500] if compact else None,
    }


def _azure_field_value(field: dict[str, Any] | None) -> str | None:
    if not field:
        return None
    for key in ("valueString", "valueDate", "valueCountryRegion", "content"):
        value = field.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _azure_field_confidence(field: dict[str, Any] | None) -> float | None:
    if not field:
        return None
    confidence = field.get("confidence")
    if confidence is None:
        return None
    try:
        return float(confidence)
    except (TypeError, ValueError):
        return None


def _search_first(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = (match.group(1) if match.lastindex else match.group(0)).strip()
            if _looks_like_identifier(candidate):
                return candidate
    return None


def _search_labeled_identifier(text: str, *, labels: list[str]) -> str | None:
    for label in labels:
        pattern = rf"{label}\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-\/]{{4,24}})"
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip()
        if _looks_like_identifier(candidate):
            return candidate
    return None


def _looks_like_identifier(value: str) -> bool:
    cleaned = value.strip().strip(".,;:()[]{}")
    if len(cleaned) < 5:
        return False
    if cleaned.lower() in _identifier_stopwords():
        return False
    if not re.search(r"\d", cleaned):
        return False
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-\/ ]{4,24}", cleaned, re.IGNORECASE):
        return False
    alpha_count = sum(1 for ch in cleaned if ch.isalpha())
    digit_count = sum(1 for ch in cleaned if ch.isdigit())
    return digit_count >= 2 or (digit_count >= 1 and alpha_count <= 6)


def _identifier_stopwords() -> set[str]:
    return {
        "winners",
        "winner",
        "world",
        "cup",
        "passport",
        "visa",
        "document",
        "reference",
        "number",
        "expiry",
        "nationality",
        "country",
        "name",
        "date",
        "birth",
        "male",
        "female",
    }


def _normalize_loose_date(value: str | None) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.fullmatch(r"(\d{2})[./-](\d{2})[./-](\d{4})", value)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return value


def _extract_passport_fields_from_content(content: str) -> dict[str, Any]:
    compact = " ".join(content.split())
    mrz = _parse_td3_mrz(content)
    dates = [_normalize_loose_date(item) for item in _find_all_dates(content)]

    return {
        "full_name": mrz.get("full_name"),
        "passport_number": mrz.get("passport_number")
        or _search_first(
            compact,
            [
                r"\b[A-Z]\s?\d{7,8}\b",
                r"\b[A-Z0-9]{8,12}\b",
            ],
        ),
        "visa_number": None,
        "date_of_birth": mrz.get("date_of_birth") or (dates[0] if dates else None),
        "gender": mrz.get("gender") or _normalize_gender(
            _search_first(
                compact,
                [
                    r"\b(MALE|FEMALE)\b",
                    r"\b([MF])\b",
                ],
            )
        ),
        "nationality": mrz.get("nationality")
        or _search_first(
            compact,
            [
                r"\b([A-Z]{3})\b",
            ],
        ),
        "expiry_date": mrz.get("expiry_date") or (dates[-1] if len(dates) >= 2 else None),
        "confidence": None,
        "notes": compact[:500] if compact else None,
    }


def _parse_td3_mrz(content: str) -> dict[str, str | None]:
    lines = [
        re.sub(r"[^A-Z0-9<]", "", line.strip().upper())
        for line in content.splitlines()
        if line.strip()
    ]
    for index, line1 in enumerate(lines[:-1]):
        line2 = lines[index + 1]
        if not line1.startswith("P<") or len(line2) < 28:
            continue

        name_part = line1[5:] if len(line1) > 5 else ""
        name_chunks = [chunk.replace("<", " ").strip() for chunk in name_part.split("<<") if chunk.strip("<")]
        surname = name_chunks[0] if name_chunks else ""
        given = name_chunks[1] if len(name_chunks) > 1 else ""
        full_name = " ".join(part for part in [given, surname] if part).strip() or None

        passport_number = line2[:9].replace("<", "").strip() or None
        nationality = line2[10:13].replace("<", "").strip() or None
        birth_raw = line2[13:19]
        gender = _normalize_gender(line2[20:21])
        expiry_raw = line2[21:27]

        return {
            "full_name": full_name,
            "passport_number": passport_number,
            "date_of_birth": _normalize_mrz_date(birth_raw, prefer_past=True),
            "gender": gender,
            "nationality": nationality,
            "expiry_date": _normalize_mrz_date(expiry_raw, prefer_past=False),
        }
    return {}


def _normalize_mrz_date(value: str, *, prefer_past: bool) -> str | None:
    if not re.fullmatch(r"\d{6}", value or ""):
        return None
    yy = int(value[:2])
    month = value[2:4]
    day = value[4:6]
    current_year_suffix = datetime.now(timezone.utc).year % 100
    if prefer_past:
        century = 1900 if yy > current_year_suffix else 2000
    else:
        century = 2000 if yy < 70 else 1900
    return f"{century + yy:04d}-{month}-{day}"


def _extract_openai_output(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("output_parsed"), dict):
        return raw["output_parsed"]

    for item in raw.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return json.loads(content["text"])
            if content.get("type") == "json_schema" and content.get("json"):
                return content["json"]
    raise ValueError("OpenAI response did not return parseable JSON.")


def _extract_paddle_passport_fields(raw: dict[str, Any]) -> dict[str, Any]:
    content = " ".join(str(raw.get("content") or "").split())
    dates = [_normalize_loose_date(item) for item in _find_all_dates(content)]
    confidence = _paddle_confidence(raw)

    return {
        "full_name": _search_first(
            content,
            [
                r"(?:name|surname|given names?)[:\s]+([A-Z][A-Z\s]{3,60})",
            ],
        ),
        "passport_number": _search_labeled_identifier(
            content,
            labels=[
                r"passport\s*(?:no\.?|number|nr\.?|id)",
                r"document\s*(?:no\.?|number|nr\.?)",
            ],
        ) or _search_first(
            content,
            [
                r"\b[A-Z][0-9]{7,8}\b",
                r"\b[A-Z0-9]{8,12}\b",
            ],
        ),
        "visa_number": None,
        "date_of_birth": dates[0] if dates else None,
        "gender": _normalize_gender(
            _search_first(
                content,
                [
                    r"\b(MALE|FEMALE)\b",
                    r"\b(M|F)\b",
                ],
            )
        ),
        "nationality": _search_first(
            content,
            [
                r"(?:nationality|country code|country)[:\s]+([A-Z]{2,3})\b",
            ],
        ),
        "expiry_date": dates[-1] if len(dates) >= 2 else None,
        "confidence": confidence,
        "notes": content[:500] if content else None,
    }


def _extract_paddle_visa_fields(raw: dict[str, Any]) -> dict[str, Any]:
    content = " ".join(str(raw.get("content") or "").split())
    confidence = _paddle_confidence(raw)

    passport_number = _search_labeled_identifier(
        content,
        labels=[
            r"passport\s*(?:no\.?|number|nr\.?|id)",
            r"document\s*(?:no\.?|number|nr\.?)",
        ],
    ) or _search_first(
        content,
        [
            r"\b[A-Z][0-9]{7,8}\b",
            r"\b[A-Z0-9]{8,12}\b",
        ],
    )
    visa_number = _search_labeled_identifier(
        content,
        labels=[
            r"visa\s*(?:no\.?|number|nr\.?|ref(?:erence)?)",
            r"reference\s*(?:no\.?|number|nr\.?)",
        ],
    )
    dates = [_normalize_loose_date(item) for item in _find_all_dates(content)]

    return {
        "full_name": None,
        "passport_number": passport_number,
        "visa_number": visa_number,
        "date_of_birth": dates[0] if dates else None,
        "gender": None,
        "nationality": _search_first(
            content,
            [
                r"(?:nationality|country|nation)[:\s]+([A-Z]{2,3})\b",
            ],
        ),
        "expiry_date": dates[-1] if len(dates) >= 2 else None,
        "confidence": confidence,
        "notes": content[:500] if content else None,
    }


def _find_all_dates(text: str) -> list[str]:
    values = re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}[./-]\d{2}[./-]\d{4}\b", text)
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_loose_date(value)
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _normalize_gender(value: str | None) -> str | None:
    if not value:
        return None
    upper = value.strip().upper()
    if upper in {"M", "MALE"}:
        return "M"
    if upper in {"F", "FEMALE"}:
        return "F"
    return value


def _paddle_confidence(raw: dict[str, Any]) -> float | None:
    confidence = raw.get("confidence")
    if confidence is None:
        return None
    try:
        return round(float(confidence), 4)
    except (TypeError, ValueError):
        return None


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    converted = image.convert("RGBA")
    background = Image.new("RGB", converted.size, (255, 255, 255))
    background.paste(converted, mask=converted.getchannel("A"))
    return background


def _resize_for_ocr(image: Image.Image, *, max_side: int = 1280) -> Image.Image:
    width, height = image.size
    longest_side = max(width, height)
    if longest_side <= max_side:
        return image
    scale = max_side / float(longest_side)
    resized = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
    return resized


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_review_message(self, *, booking_id: str, summary_text: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            diag_logger.warning("send_review_message skipped: missing TELEGRAM_BOT_TOKEN or CHAT_ID")
            return
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": _plain_telegram_text(summary_text),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Approve booking",
                            "callback_data": self._encode_callback(
                                TelegramCallbackPayload(action="approve_booking", booking_id=booking_id)
                            ),
                        },
                        {
                            "text": "Reject booking",
                            "callback_data": self._encode_callback(
                                TelegramCallbackPayload(action="reject_booking", booking_id=booking_id)
                            ),
                        },
                    ]
                ]
            },
        }
        diag_logger.info(f"Telegram send_review_message (text+buttons) for booking {booking_id}")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await self._telegram_post_with_retry(client, "sendMessage", payload)
            diag_logger.info(f"Telegram sendMessage response status={response.status_code}")
            response.raise_for_status()

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        if not self.settings.telegram_bot_token:
            return
        async with httpx.AsyncClient(timeout=20) as client:
            response = await self._telegram_post_with_retry(
                client,
                "answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text, "show_alert": False},
            )
            response.raise_for_status()

    async def send_text_message(self, text: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        await self.send_chat_message(str(self.settings.telegram_chat_id), text)

    async def send_chat_message(self, chat_id: str, text: str) -> None:
        if not self.settings.telegram_bot_token or not chat_id:
            return
        diag_logger.info("Telegram send_chat_message")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await self._telegram_post_with_retry(
                client,
                "sendMessage",
                {"chat_id": chat_id, "text": _plain_telegram_text(text)},
            )
            diag_logger.info(f"Telegram send_chat_message status={response.status_code}")
            response.raise_for_status()

    async def send_document_images(self, *, booking_id: str, documents: list[dict[str, Any]]) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id or not documents:
            diag_logger.info("send_document_images skipped (no token or no documents)")
            return

        diag_logger.info(f"send_document_images starting for booking {booking_id}, count={len(documents)}")
        async with httpx.AsyncClient(timeout=60) as client:
            for item in documents:
                try:
                    image = await self._load_image_payload(client, item["source_url"], item["doc_type"])
                    caption = (
                        f"Booking: {item['external_booking_id']}\n"
                        f"Guest {item['guest_index']}: {item['guest_name'] or '-'}\n"
                        f"Document: {item['doc_type']}\n"
                        f"Internal ID: {booking_id}"
                    )
                    response = await client.post(
                        self._telegram_url("sendPhoto"),
                        data={
                            "chat_id": self.settings.telegram_chat_id,
                            "caption": caption,
                        },
                        files={
                            "photo": (
                                image["filename"],
                                image["content"],
                                image["mime_type"],
                            )
                        },
                    )
                    diag_logger.info(f"  sendPhoto {item['doc_type']} status={response.status_code}")
                    if response.status_code == 400:
                        await self.send_text_message(
                            f"Khong gui duoc anh {item['doc_type']} cho booking {item['external_booking_id']}."
                        )
                        continue
                    response.raise_for_status()
                except Exception as exc:
                    diag_logger.warning(f"  sendPhoto FAILED {item['doc_type']}: {exc}")
                    await self.send_text_message(
                        f"Loi gui anh Telegram cho booking {item['external_booking_id']} / {item['doc_type']}: {str(exc)[:200]}"
                    )

    def parse_callback(self, callback_data: str) -> TelegramCallbackPayload:
        if callback_data.startswith("approve:"):
            return TelegramCallbackPayload(action="approve_booking", booking_id=callback_data.removeprefix("approve:"))
        if callback_data.startswith("reject:"):
            return TelegramCallbackPayload(action="reject_booking", booking_id=callback_data.removeprefix("reject:"))
        decoded = base64.urlsafe_b64decode(callback_data.encode("ascii")).decode("utf-8")
        return TelegramCallbackPayload.model_validate_json(decoded)

    def _encode_callback(self, payload: TelegramCallbackPayload) -> str:
        if payload.action == "approve_booking":
            return f"approve:{payload.booking_id}"
        if payload.action == "reject_booking":
            return f"reject:{payload.booking_id}"
        return base64.urlsafe_b64encode(payload.model_dump_json().encode("utf-8")).decode("ascii")

    def _telegram_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"

    async def _load_image_payload(
        self,
        client: httpx.AsyncClient,
        source_url: str,
        doc_type: str,
    ) -> dict[str, Any]:
        if source_url.startswith("data:image/"):
            header, encoded = source_url.split(",", 1)
            mime_type = header.split(";", 1)[0].removeprefix("data:")
            extension = mimetypes.guess_extension(mime_type) or ".jpg"
            return {
                "filename": f"{doc_type}{extension}",
                "mime_type": mime_type,
                "content": base64.b64decode(encoded),
            }

        response = await client.get(source_url, follow_redirects=True)
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        extension = mimetypes.guess_extension(mime_type) or ".jpg"
        return {
            "filename": f"{doc_type}{extension}",
            "mime_type": mime_type,
            "content": response.content,
        }

    async def _telegram_post_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        delays = [1, 3, 6]
        last_response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(len(delays) + 1):
            try:
                response = await client.post(self._telegram_url(method), json=payload)
                last_response = response
                if response.status_code in {429, 500, 502, 503, 504} and attempt < len(delays):
                    diag_logger.info(f"  Telegram {method} got {response.status_code}, retrying in {delays[attempt]}s (attempt {attempt+1})")
                    await asyncio.sleep(delays[attempt])
                    continue
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= len(delays):
                    break
                diag_logger.info(f"  Telegram {method} HTTP error, retrying... (attempt {attempt+1})")
                await asyncio.sleep(delays[attempt])
        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        raise RuntimeError("Telegram request failed without response.")


def _plain_telegram_text(text: str) -> str:
    return text[:3500]


class Beds24Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._auth_lock = asyncio.Lock()
        self._auth_cache_path = self.settings.base_dir / "data" / "beds24_auth.json"
        self._access_token = self.settings.beds24_api_token or ""
        self._refresh_token = self.settings.beds24_refresh_token or ""

    def _compact_for_log(self, value: Any, *, max_string_len: int = 320) -> Any:
        if isinstance(value, dict):
            return {key: self._compact_for_log(item, max_string_len=max_string_len) for key, item in value.items()}
        if isinstance(value, list):
            return [self._compact_for_log(item, max_string_len=max_string_len) for item in value]
        if isinstance(value, tuple):
            return [self._compact_for_log(item, max_string_len=max_string_len) for item in value]
        if isinstance(value, str) and len(value) > max_string_len:
            return value[:max_string_len] + f"...<truncated {len(value) - max_string_len} chars>"
        return value

    def _log_booking_sample(self, *, booking_identifier: str, bookings: list[dict[str, Any]], source: str) -> None:
        if not bookings:
            diag_logger.info(f"Beds24 debug: no bookings returned for identifier={booking_identifier} source={source}")
            return

        diag_logger.info(
            f"Beds24 debug: identifier={booking_identifier} source={source} candidate_count={len(bookings)}"
        )
        for index, booking in enumerate(bookings[:3], start=1):
            match_keys: list[str] = []
            for key in ("id", "apiReference", "reference", "comments", "notes", "groupNote", "voucher"):
                value = booking.get(key)
                if value is not None and str(value).strip() == booking_identifier:
                    match_keys.append(key)
            compact = self._compact_for_log(booking)
            diag_logger.info(
                "Beds24 debug sample #%s match_keys=%s payload=%s",
                index,
                match_keys or "-",
                json.dumps(compact, ensure_ascii=False, indent=2),
            )

    def _normalize_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    def _normalize_digits(self, value: Any) -> str:
        return re.sub(r"\D+", "", str(value or ""))

    def _candidate_texts(self, hints: dict[str, Any] | None) -> list[str]:
        if not hints:
            return []

        values: list[str] = []
        for key in (
            "external_booking_id",
            "submission_id",
            "email",
            "lead_guest_name",
            "full_name",
            "first_name",
            "last_name",
            "room_name",
            "room_label",
            "check_in",
            "check_out",
        ):
            value = hints.get(key)
            if value:
                values.append(str(value))

        for key in ("guest_names", "phone_numbers"):
            items = hints.get(key)
            if isinstance(items, list):
                values.extend(str(item) for item in items if item)

        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if len(normalized) < 3:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(normalized)
        return unique

    def _room_label(self, booking: dict[str, Any]) -> str:
        room_id = booking.get("roomId")
        unit_id = booking.get("unitId")
        if room_id and unit_id not in (None, ""):
            return f"room:{room_id}/unit:{unit_id}"
        if room_id:
            return f"room:{room_id}"
        if unit_id not in (None, ""):
            return f"unit:{unit_id}"
        return ""

    def _best_match_score(self, booking: dict[str, Any], booking_identifier: str, hints: dict[str, Any] | None) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        custom1 = booking.get("custom1")
        if custom1 is not None and str(custom1).strip() == booking_identifier:
            score += 300
            reasons.append("custom1=exact")

        hint_submission_id = str((hints or {}).get("submission_id") or "").strip()
        custom2 = booking.get("custom2")
        if hint_submission_id and custom2 is not None and str(custom2).strip() == hint_submission_id:
            score += 280
            reasons.append("custom2=exact")

        notes = booking.get("notes")
        if notes is not None and booking_identifier in str(notes):
            score += 220
            reasons.append("notes=contains")

        exact_keys = ("apiReference", "reference", "id", "comments", "notes", "groupNote", "voucher")
        for key in exact_keys:
            value = booking.get(key)
            if value is not None and str(value).strip() == booking_identifier:
                score += 100
                reasons.append(f"{key}=exact")

        if not hints:
            return score, reasons

        booking_email = self._normalize_text(booking.get("email"))
        hint_email = self._normalize_text(hints.get("email"))
        if booking_email and hint_email and booking_email == hint_email:
            score += 40
            reasons.append("email=exact")

        booking_phone = self._normalize_digits(booking.get("phone") or booking.get("mobile"))
        hint_phones = [self._normalize_digits(phone) for phone in hints.get("phone_numbers", []) if phone]
        if booking_phone and booking_phone in hint_phones:
            score += 35
            reasons.append("phone=exact")

        booking_first = self._normalize_text(booking.get("firstName"))
        booking_last = self._normalize_text(booking.get("lastName"))
        booking_full_name = " ".join(part for part in [booking_first, booking_last] if part).strip()
        hint_names = []
        for key in ("lead_guest_name", "full_name"):
            value = hints.get(key)
            if value:
                hint_names.append(self._normalize_text(value))
        for value in hints.get("guest_names", []) if isinstance(hints.get("guest_names"), list) else []:
            if value:
                hint_names.append(self._normalize_text(value))

        if booking_full_name and booking_full_name in hint_names:
            score += 30
            reasons.append("name=exact")
        elif booking_first and booking_first in hint_names:
            score += 15
            reasons.append("first_name=partial")
        elif booking_last and booking_last in hint_names:
            score += 15
            reasons.append("last_name=partial")

        booking_arrival = self._normalize_text(booking.get("arrival"))
        booking_departure = self._normalize_text(booking.get("departure"))
        hint_check_in = self._normalize_text(hints.get("check_in"))
        hint_check_out = self._normalize_text(hints.get("check_out"))
        if booking_arrival and hint_check_in and booking_arrival == hint_check_in:
            score += 25
            reasons.append("check_in=exact")
        if booking_departure and hint_check_out and booking_departure == hint_check_out:
            score += 25
            reasons.append("check_out=exact")

        booking_room = self._normalize_text(self._room_label(booking))
        hint_room = self._normalize_text(hints.get("room_label") or hints.get("room_name"))
        if booking_room and hint_room and booking_room == hint_room:
            score += 10
            reasons.append("room=exact")

        booking_referer = self._normalize_text(booking.get("referer"))
        hint_channel = self._normalize_text(hints.get("channel"))
        if booking_referer and hint_channel and booking_referer == hint_channel:
            score += 5
            reasons.append("referer=exact")

        return score, reasons

    def _select_best_candidate(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        booking_identifier: str,
        hints: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, int, list[str]]:
        best_booking: dict[str, Any] | None = None
        best_score = -1
        best_reasons: list[str] = []
        for booking in candidates.values():
            score, reasons = self._best_match_score(booking, booking_identifier, hints)
            if score > best_score:
                best_booking = booking
                best_score = score
                best_reasons = reasons
        return best_booking, best_score, best_reasons

    def _load_auth_cache(self) -> dict[str, Any]:
        if not self._auth_cache_path.exists():
            return {}
        try:
            return json.loads(self._auth_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_auth_cache(self) -> None:
        self._auth_cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self._access_token,
            "refreshToken": self._refresh_token,
        }
        self._auth_cache_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    async def _ensure_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._auth_lock:
            if not force_refresh and self._access_token:
                return self._access_token

            cache = self._load_auth_cache()
            if not self._refresh_token:
                self._refresh_token = str(cache.get("refreshToken") or "")
            if not force_refresh and not self._access_token:
                self._access_token = str(cache.get("token") or "")
                if self._access_token:
                    return self._access_token

            if self._refresh_token:
                await self._refresh_access_token()
                return self._access_token

            invite_code = self.settings.beds24_invite_code.strip()
            if invite_code:
                await self._exchange_invite_code(invite_code)
                return self._access_token

            return self._access_token

    async def _exchange_invite_code(self, invite_code: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.settings.beds24_api_base_url}/authentication/setup",
                headers={"accept": "application/json", "code": invite_code},
            )
            response.raise_for_status()
            raw = response.json()
        self._access_token = str(raw.get("token") or "")
        self._refresh_token = str(raw.get("refreshToken") or "")
        if self._access_token or self._refresh_token:
            self._save_auth_cache()
        if not self._access_token:
            raise RuntimeError("Beds24 authentication/setup did not return an access token.")

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise RuntimeError("Beds24 refresh token is not configured.")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.settings.beds24_api_base_url}/authentication/token",
                headers={"accept": "application/json", "refreshToken": self._refresh_token},
            )
            response.raise_for_status()
            raw = response.json()
        self._access_token = str(raw.get("token") or "")
        self._refresh_token = str(raw.get("refreshToken") or self._refresh_token)
        self._save_auth_cache()
        if not self._access_token:
            raise RuntimeError("Beds24 authentication/token did not return an access token.")

    async def find_booking(self, booking_identifier: str, *, hints: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not booking_identifier:
            return None
        token = await self._ensure_access_token()
        if not token:
            return None
        if booking_identifier.isdigit():
            bookings = await self._get_bookings({"id": [int(booking_identifier)]})
            self._log_booking_sample(booking_identifier=booking_identifier, bookings=bookings, source="id")
            for booking in bookings:
                if str(booking.get("id")) == booking_identifier:
                    diag_logger.info(
                        f"Beds24 debug: matched identifier={booking_identifier} on key=id booking_id={booking.get('id')}"
                    )
                    return booking

        candidates: dict[str, dict[str, Any]] = {}
        search_terms = [booking_identifier]
        if hints:
            search_terms.extend(self._candidate_texts(hints))

        for term in search_terms:
            bookings = await self._get_bookings({"searchString": term})
            self._log_booking_sample(booking_identifier=booking_identifier, bookings=bookings, source=f"searchString:{term}")
            for booking in bookings:
                booking_id = str(booking.get("id") or "")
                if booking_id and booking_id not in candidates:
                    candidates[booking_id] = booking

        if not candidates and hints:
            bookings = await self._get_bookings({})
            self._log_booking_sample(booking_identifier=booking_identifier, bookings=bookings, source="all")
            for booking in bookings:
                booking_id = str(booking.get("id") or "")
                if booking_id and booking_id not in candidates:
                    candidates[booking_id] = booking

        if not candidates:
            return None

        best_booking, best_score, best_reasons = self._select_best_candidate(
            candidates,
            booking_identifier=booking_identifier,
            hints=hints,
        )

        if best_booking is None:
            return None

        exact_reason_keys = {"custom1=exact", "custom2=exact"}
        if hints and not any(reason in exact_reason_keys for reason in best_reasons):
            bookings = await self._get_bookings({})
            self._log_booking_sample(booking_identifier=booking_identifier, bookings=bookings, source="all-rescore")
            for booking in bookings:
                booking_id = str(booking.get("id") or "")
                if booking_id and booking_id not in candidates:
                    candidates[booking_id] = booking
            rescored_booking, rescored_score, rescored_reasons = self._select_best_candidate(
                candidates,
                booking_identifier=booking_identifier,
                hints=hints,
            )
            if rescored_booking is not None and rescored_score > best_score:
                best_booking = rescored_booking
                best_score = rescored_score
                best_reasons = rescored_reasons

        if any(reason in exact_reason_keys for reason in best_reasons):
            diag_logger.info(
                "Beds24 debug: immediate custom-field match booking_id=%s score=%s reasons=%s",
                best_booking.get("id"),
                best_score,
                best_reasons,
            )
            return best_booking

        if best_score <= 0:
            diag_logger.info(
                f"Beds24 debug: candidate found for identifier={booking_identifier} but score={best_score}; returning no match"
            )
            return None

        diag_logger.info(
            "Beds24 debug: selected booking_id=%s score=%s reasons=%s",
            best_booking.get("id"),
            best_score,
            best_reasons or ["search_term_only"],
        )
        return best_booking

    async def mark_booking_approved(self, booking: dict[str, Any]) -> None:
        token = await self._ensure_access_token()
        if not token:
            return

        booking_id = booking.get("id")
        if booking_id is None:
            raise ValueError("Beds24 booking is missing id.")

        update_item: dict[str, Any] = {"id": booking_id}
        info_items: list[dict[str, Any]] = []
        if self.settings.beds24_approval_info_code:
            info_items.append(
                {
                    "code": self.settings.beds24_approval_info_code,
                    "text": self.settings.beds24_approval_info_text or "approved",
                }
            )
        if info_items:
            update_item["infoItems"] = info_items
        if self.settings.beds24_approval_flag_text:
            update_item["flagText"] = self.settings.beds24_approval_flag_text
        if self.settings.beds24_approval_flag_color:
            update_item["flagColor"] = self.settings.beds24_approval_flag_color

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.beds24_api_base_url}/bookings",
                json=[update_item],
                headers=await self._api_headers(),
            )
            response.raise_for_status()
            raw = response.json()

        if isinstance(raw, list) and raw and raw[0].get("success") is False and raw[0].get("errors"):
            raise RuntimeError(json.dumps(raw[0], ensure_ascii=True))

    async def persist_lookup_marker(
        self,
        booking: dict[str, Any],
        external_booking_id: str,
        *,
        jotform_submission_id: str | None = None,
        hints: dict[str, Any] | None = None,
    ) -> None:
        token = await self._ensure_access_token()
        if not token or not external_booking_id:
            return

        booking_id = booking.get("id")
        if booking_id is None:
            return

        hint_email = (hints or {}).get("email")
        hint_check_in = (hints or {}).get("check_in")
        hint_check_out = (hints or {}).get("check_out")
        hint_room = (hints or {}).get("room_label") or (hints or {}).get("room_name")
        hint_submission_id = jotform_submission_id or (hints or {}).get("submission_id")

        marker = (
            "Mandala Jotform "
            f"BOOKINGID={external_booking_id} | "
            f"submission_id={hint_submission_id or '-'} | "
            f"Beds24 ID={booking_id} | "
            f"apiReference={booking.get('apiReference') or '-'} | "
            f"reference={booking.get('reference') or '-'} | "
            f"email={hint_email or booking.get('email') or '-'} | "
            f"check_in={hint_check_in or booking.get('arrival') or '-'} | "
            f"check_out={hint_check_out or booking.get('departure') or '-'} | "
            f"room={hint_room or booking.get('roomId') or '-'}"
        )
        existing_notes = str(booking.get("notes") or "")
        existing_custom1 = str(booking.get("custom1") or "")

        update_item: dict[str, Any] = {"id": booking_id}
        changed = False

        booking_token = f"BOOKINGID={external_booking_id}"
        if booking_token in existing_notes:
            diag_logger.info(
                f"Beds24 debug: lookup marker already present for booking_id={booking_id}; leaving notes unchanged"
            )
        else:
            update_item["notes"] = f"{existing_notes.rstrip()}\n{marker}".strip()
            changed = True

        if not existing_custom1:
            update_item["custom1"] = external_booking_id
            changed = True
        elif existing_custom1.strip() == external_booking_id:
            pass
        else:
            diag_logger.info(
                f"Beds24 debug: custom1 already occupied for booking_id={booking_id}; leaving existing value untouched"
            )

        existing_custom2 = str(booking.get("custom2") or "")
        if hint_submission_id and not existing_custom2:
            update_item["custom2"] = str(hint_submission_id)
            changed = True
        elif hint_submission_id and existing_custom2.strip() == str(hint_submission_id):
            pass
        elif hint_submission_id and existing_custom2:
            diag_logger.info(
                f"Beds24 debug: custom2 already occupied for booking_id={booking_id}; leaving existing value untouched"
            )

        if not changed:
            diag_logger.info(f"Beds24 debug: lookup marker already present for booking_id={booking_id}")
            return

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.beds24_api_base_url}/bookings",
                json=[update_item],
                headers=await self._api_headers(),
            )
            response.raise_for_status()
            raw = response.json()

        if isinstance(raw, list) and raw and raw[0].get("success") is False and raw[0].get("errors"):
            raise RuntimeError(json.dumps(raw[0], ensure_ascii=True))
        diag_logger.info(
            f"Beds24 debug: persisted lookup marker booking_id={booking_id} external_booking_id={external_booking_id}"
        )

    async def persist_checkin_link_note(self, booking: dict[str, Any], checkin_link: str) -> None:
        token = await self._ensure_access_token()
        if not token:
            raise RuntimeError("Beds24 API token is not available.")

        booking_id = booking.get("id")
        if booking_id is None:
            raise ValueError("Beds24 booking is missing id.")

        existing_notes = str(booking.get("notes") or "").strip()
        marker_prefix = "Mandala check-in link:"
        marker_line = f"{marker_prefix} {checkin_link}"
        kept_lines = [
            line
            for line in existing_notes.splitlines()
            if not line.strip().startswith(marker_prefix)
        ]
        updated_notes = "\n".join([line for line in kept_lines if line.strip()] + [marker_line]).strip()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.beds24_api_base_url}/bookings",
                json=[{"id": booking_id, "notes": updated_notes}],
                headers=await self._api_headers(),
            )
            response.raise_for_status()
            raw = response.json()

        if isinstance(raw, list) and raw and raw[0].get("success") is False and raw[0].get("errors"):
            raise RuntimeError(json.dumps(raw[0], ensure_ascii=True))
        diag_logger.info("Beds24 debug: persisted check-in link note booking_id=%s", booking_id)

    async def trigger_checkin_instructions(self, *, external_booking_id: str, payload: dict[str, Any], hints: dict[str, Any] | None = None) -> None:
        booking = await self.find_booking(external_booking_id, hints=hints)
        if booking is not None:
            await self.mark_booking_approved(booking)
            return

        if not self.settings.beds24_webhook_url:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.settings.beds24_webhook_url,
                json={"external_booking_id": external_booking_id, **payload},
                headers=await self._api_headers(legacy_webhook=True),
            )
            response.raise_for_status()

    async def _api_headers(self, *, legacy_webhook: bool = False) -> dict[str, str]:
        headers = {"accept": "application/json"}
        token = await self._ensure_access_token()
        if token:
            if legacy_webhook:
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["token"] = token
        if self.settings.beds24_organization and not legacy_webhook:
            headers["organization"] = self.settings.beds24_organization
        return headers

    async def _get_bookings(self, extra_params: dict[str, Any]) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "includeGuests": "true",
            "includeInfoItems": "true",
            "includeBookingGroup": "true",
            **extra_params,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.settings.beds24_api_base_url}/bookings",
                params=params,
                headers=await self._api_headers(),
            )
            if response.status_code == 401:
                diag_logger.warning("Beds24 returned 401. Refreshing token and retrying once.")
                await self._ensure_access_token(force_refresh=True)
                response = await client.get(
                    f"{self.settings.beds24_api_base_url}/bookings",
                    params=params,
                    headers=await self._api_headers(),
                )
            response.raise_for_status()
            raw = response.json()

        bookings = raw.get("data", [])
        if not isinstance(bookings, list):
            return []
        return bookings


class TTLockClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._auth_lock = asyncio.Lock()
        self._auth_cache_path = self.settings.base_dir / "data" / "ttlock_auth.json"
        self._access_token = ""
        self._expires_at_ms = 0

    def enabled(self) -> bool:
        return all(
            [
                self.settings.ttlock_api_base_url,
                self.settings.ttlock_client_id,
                self.settings.ttlock_client_secret,
                self.settings.ttlock_username,
                self.settings.ttlock_password,
            ]
        )

    def configured_lock_ids(self) -> tuple[int, ...]:
        return self.settings.ttlock_lock_ids

    def generate_passcode(self) -> str:
        lower_bound = 10 ** (self.settings.ttlock_code_length - 1)
        upper_span = 9 * lower_bound
        return str(lower_bound + secrets.randbelow(upper_span))

    async def list_locks(self) -> list[dict[str, Any]]:
        access_token = await self._ensure_access_token()
        raw = await self._post(
            "/v3/lock/list",
            {
                "accessToken": access_token,
                "pageNo": 1,
                "pageSize": 100,
            },
        )
        locks = raw.get("list") or raw.get("data") or []
        if not isinstance(locks, list):
            return []
        return locks

    async def add_custom_passcode(
        self,
        *,
        lock_id: int,
        passcode: str,
        passcode_name: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        access_token = await self._ensure_access_token()
        return await self._post(
            "/v3/keyboardPwd/add",
            {
                "accessToken": access_token,
                "lockId": lock_id,
                "keyboardPwd": passcode,
                "keyboardPwdName": passcode_name,
                "startDate": start_ms,
                "endDate": end_ms,
                "addType": 2,
            },
        )

    async def add_booking_passcode_to_locks(
        self,
        *,
        booking_id: str,
        guest_name: str,
        start_ms: int,
        end_ms: int,
        lock_ids: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("TTLock is not configured.")
        target_lock_ids = lock_ids or self.configured_lock_ids()
        if not target_lock_ids:
            raise RuntimeError("TTLock lock ids are not configured.")

        last_errors: list[str] = []
        for _attempt in range(5):
            passcode = self.generate_passcode()
            successes: list[dict[str, Any]] = []
            failures: list[dict[str, Any]] = []
            for lock_id in target_lock_ids:
                try:
                    raw = await self.add_custom_passcode(
                        lock_id=lock_id,
                        passcode=passcode,
                        passcode_name=self._passcode_name(booking_id, guest_name),
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    successes.append({"lock_id": lock_id, "response": raw})
                except Exception as exc:
                    message = str(exc)
                    failures.append({"lock_id": lock_id, "error": message})
                    last_errors.append(f"{lock_id}: {message}")

            duplicate_failure = any("duplicate" in str(item.get("error", "")).lower() for item in failures)
            if failures and duplicate_failure and not successes:
                continue

            return {
                "passcode": passcode,
                "successes": successes,
                "failures": failures,
            }

        raise RuntimeError("Could not create TTLock passcode after retries: " + " | ".join(last_errors[-5:]))

    def _passcode_name(self, booking_id: str, guest_name: str) -> str:
        clean_guest = re.sub(r"\s+", " ", guest_name or "").strip()
        if clean_guest:
            return f"Booking {booking_id} - {clean_guest}"[:80]
        return f"Booking {booking_id}"[:80]

    async def _ensure_access_token(self) -> str:
        if not self.enabled():
            raise RuntimeError("TTLock is not configured.")
        now_ms = self._now_ms()
        async with self._auth_lock:
            if self._access_token and self._expires_at_ms - now_ms > 300_000:
                return self._access_token

            cache = self._load_auth_cache()
            cached_token = str(cache.get("access_token") or "")
            cached_expires = int(cache.get("expires_at_ms") or 0)
            if cached_token and cached_expires - now_ms > 300_000:
                self._access_token = cached_token
                self._expires_at_ms = cached_expires
                return self._access_token

            await self._fetch_access_token()
            return self._access_token

    async def _fetch_access_token(self) -> None:
        raw = await self._post(
            "/oauth2/token",
            {
                "username": self.settings.ttlock_username,
                "password": hashlib.md5(self.settings.ttlock_password.encode("utf-8")).hexdigest(),
            },
            include_auth=False,
        )
        access_token = str(raw.get("access_token") or "")
        if not access_token:
            raise RuntimeError("TTLock token response did not include access_token.")
        expires_in = int(raw.get("expires_in") or 0)
        self._access_token = access_token
        self._expires_at_ms = self._now_ms() + max(0, expires_in - 300) * 1000
        self._save_auth_cache()

    async def _post(self, path: str, data: dict[str, Any], *, include_auth: bool = True) -> dict[str, Any]:
        payload = {
            "clientId": self.settings.ttlock_client_id,
            "date": self._now_ms(),
            **data,
        }
        if include_auth:
            payload["accessToken"] = data.get("accessToken") or self._access_token
        else:
            payload["client_secret"] = self.settings.ttlock_client_secret
            payload["client_id"] = self.settings.ttlock_client_id
            payload.pop("clientId", None)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.settings.ttlock_api_base_url}{path}", data=payload)
            response.raise_for_status()
            raw = response.json()

        errcode = raw.get("errcode")
        if errcode not in (None, 0, "0"):
            errmsg = raw.get("errmsg") or raw.get("description") or raw
            raise RuntimeError(f"TTLock API error {errcode}: {errmsg}")
        return raw

    def _load_auth_cache(self) -> dict[str, Any]:
        if not self._auth_cache_path.exists():
            return {}
        try:
            return json.loads(self._auth_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_auth_cache(self) -> None:
        self._auth_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._auth_cache_path.write_text(
            json.dumps(
                {
                    "access_token": self._access_token,
                    "expires_at_ms": self._expires_at_ms,
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _now_ms(self) -> int:
        return int(time.time() * 1000)


class GoogleSheetMirror:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def sync_booking(self, payload: dict[str, Any]) -> None:
        if not self.settings.sync_google_sheet or not self.settings.google_sheet_webhook_url:
            diag_logger.info("Google Sheet sync skipped: disabled or missing webhook URL")
            return
        booking_id = str(payload.get("id") or "-")
        submission_id = str(payload.get("jotform_submission_id") or "-")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(self.settings.google_sheet_webhook_url, json=payload)
            response.raise_for_status()
        response_preview = response.text[:300].replace("\n", " ").replace("\r", " ")
        diag_logger.info(
            "Google Sheet sync completed booking_id=%s submission_id=%s status=%s body=%s",
            booking_id,
            submission_id,
            response.status_code,
            response_preview,
        )
