from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentInput(BaseModel):
    doc_type: str
    source_url: str


class GuestInput(BaseModel):
    guest_index: int = Field(ge=1, le=4)
    full_name: str | None = None
    date_of_birth: str | None = None
    phone_number: str | None = None
    gender: str | None = None
    nationality: str | None = None
    passport_number: str | None = None
    visa_number: str | None = None
    documents: list[DocumentInput] = Field(default_factory=list)


class BookingInput(BaseModel):
    external_booking_id: str
    jotform_submission_id: str | None = None
    lead_guest_name: str | None = None
    guest_count: int = Field(ge=1, le=4)
    guests: list[GuestInput]
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class OCRExtractionResult(BaseModel):
    full_name: str | None = None
    passport_number: str | None = None
    visa_number: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    nationality: str | None = None
    expiry_date: str | None = None
    confidence: float | None = None
    notes: str | None = None


class TelegramCallbackPayload(BaseModel):
    action: str
    booking_id: str
    guest_id: str | None = None


class BookingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    external_booking_id: str
    jotform_submission_id: str | None = None
    lead_guest_name: str | None = None
    guest_count: int
    status: str
    instructions_sent: bool
    guests: list[dict[str, Any]]
    review_actions: list[dict[str, Any]]


@dataclass
class BookingRecord:
    id: str
    external_booking_id: str
    jotform_submission_id: str | None
    lead_guest_name: str | None
    guest_count: int
    status: str
    instructions_sent: bool
    source_payload_json: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "BookingRecord":
        return cls(
            id=str(row["id"]),
            external_booking_id=str(row["external_booking_id"]),
            jotform_submission_id=row["jotform_submission_id"],
            lead_guest_name=row["lead_guest_name"],
            guest_count=int(row["guest_count"]),
            status=str(row["status"]),
            instructions_sent=bool(row["instructions_sent"]),
            source_payload_json=str(row["source_payload_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @property
    def source_payload(self) -> dict[str, Any]:
        return json.loads(self.source_payload_json)


@dataclass
class GuestRecord:
    id: str
    booking_id: str
    guest_index: int
    full_name: str | None
    date_of_birth: str | None
    gender: str | None
    nationality: str | None
    passport_number: str | None
    visa_number: str | None
    review_status: str
    source_payload_json: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "GuestRecord":
        return cls(
            id=str(row["id"]),
            booking_id=str(row["booking_id"]),
            guest_index=int(row["guest_index"]),
            full_name=row["full_name"],
            date_of_birth=row["date_of_birth"],
            gender=row["gender"],
            nationality=row["nationality"],
            passport_number=row["passport_number"],
            visa_number=row["visa_number"],
            review_status=str(row["review_status"]),
            source_payload_json=str(row["source_payload_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @property
    def source_payload(self) -> dict[str, Any]:
        return json.loads(self.source_payload_json)


@dataclass
class DocumentRecord:
    id: str
    guest_id: str
    doc_type: str
    source_url: str
    storage_path: str | None
    file_hash: str | None
    ocr_status: str
    ocr_engine: str | None
    ocr_raw_json: str | None
    extracted_json: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "DocumentRecord":
        return cls(
            id=str(row["id"]),
            guest_id=str(row["guest_id"]),
            doc_type=str(row["doc_type"]),
            source_url=str(row["source_url"]),
            storage_path=row["storage_path"],
            file_hash=row["file_hash"],
            ocr_status=str(row["ocr_status"]),
            ocr_engine=row["ocr_engine"],
            ocr_raw_json=row["ocr_raw_json"],
            extracted_json=row["extracted_json"],
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @property
    def ocr_raw(self) -> dict[str, Any] | None:
        return None if self.ocr_raw_json is None else json.loads(self.ocr_raw_json)

    @property
    def extracted(self) -> dict[str, Any] | None:
        return None if self.extracted_json is None else json.loads(self.extracted_json)


@dataclass
class ReviewActionRecord:
    id: str
    booking_id: str
    guest_id: str | None
    actor: str
    action: str
    note: str | None
    payload_json: str
    created_at: str

    @classmethod
    def from_row(cls, row) -> "ReviewActionRecord":
        return cls(
            id=str(row["id"]),
            booking_id=str(row["booking_id"]),
            guest_id=row["guest_id"],
            actor=str(row["actor"]),
            action=str(row["action"]),
            note=row["note"],
            payload_json=str(row["payload_json"]),
            created_at=str(row["created_at"]),
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)
