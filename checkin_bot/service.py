from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
import logging
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .db import Database
from .integrations import Beds24Client, GoogleSheetMirror, OCRProvider, TelegramClient, TTLockClient
from .schemas import BookingInput, BookingView, DocumentRecord

# Reuse the diagnostic logger set up in app.py (falls back to basic if imported directly)
diag_logger = logging.getLogger("checkin-bot")


class CheckinService:
    def __init__(
        self,
        db: Database,
        ocr: OCRProvider,
        telegram: TelegramClient,
        beds24: Beds24Client,
        ttlock: TTLockClient,
        sheet_mirror: GoogleSheetMirror,
    ) -> None:
        self.db = db
        self.ocr = ocr
        self.telegram = telegram
        self.beds24 = beds24
        self.ttlock = ttlock
        self.sheet_mirror = sheet_mirror

    async def ingest_booking(self, payload: BookingInput) -> str:
        source_payload = payload.raw_payload or payload.model_dump(mode="json")
        lookup_hints = _beds24_lookup_hints_from_booking_input(payload)
        beds24_matched = False

        # Best-effort Beds24 lookup: never let it kill the submission.
        # Even on 401, 429, timeout, or any error we still save the booking
        # and schedule the review notification.
        if payload.external_booking_id and payload.external_booking_id != "unknown-booking":
            try:
                beds24_booking = await self.beds24.find_booking(payload.external_booking_id, hints=lookup_hints)
                if beds24_booking is not None:
                    beds24_matched = True
                    source_payload = _merge_beds24_booking(source_payload, beds24_booking, self.beds24.settings.beds24_room_labels)
                    try:
                        await self.beds24.persist_lookup_marker(
                            beds24_booking,
                            payload.external_booking_id,
                            jotform_submission_id=payload.jotform_submission_id,
                            hints=lookup_hints,
                        )
                    except Exception as exc:
                        diag_logger.warning(
                            f"Beds24 lookup marker sync failed for external_booking_id={payload.external_booking_id}: {exc}"
                        )
            except Exception as exc:
                diag_logger.warning(
                    f"Beds24 lookup failed for external_booking_id={payload.external_booking_id} "
                    f"(will continue without merging Beds24 data): {exc}"
                )
        else:
            diag_logger.info(f"Skipping Beds24 lookup (external_booking_id={payload.external_booking_id})")

        booking_id = self.db.upsert_booking(
            external_booking_id=payload.external_booking_id,
            jotform_submission_id=payload.jotform_submission_id,
            lead_guest_name=payload.lead_guest_name or (payload.guests[0].full_name if payload.guests else "") or "",
            guest_count=payload.guest_count,
            status="submitted" if beds24_matched else "unmatched_submitted",
            source_payload=source_payload,
        )
        self.db.delete_review_actions(
            booking_id=booking_id,
            actor="system",
            actions=["ocr_warning", "sheet_sync_failed", "beds24_unmatched"],
        )
        if not beds24_matched:
            self.db.add_review_action(
                booking_id=booking_id,
                guest_id=None,
                actor="system",
                action="beds24_unmatched",
                note="Beds24 booking not matched at ingest; will retry after OCR or manual refresh.",
            )
        for guest in payload.guests:
            guest_id = self.db.upsert_guest(
                booking_id=booking_id,
                guest_index=guest.guest_index,
                payload=guest.model_dump(mode="json"),
            )
            for document in guest.documents:
                self.db.upsert_document(guest_id=guest_id, doc_type=document.doc_type, source_url=document.source_url)
        self.db.update_booking_status(booking_id, "ocr_pending" if beds24_matched else "unmatched_ocr_pending")
        return booking_id

    async def refresh_beds24_booking(self, booking_id: str) -> dict[str, Any] | None:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            raise ValueError("Booking not found.")
        return await self._sync_beds24_booking(booking_id, allow_status_update=True)

    async def retry_unmatched_beds24_bookings(self) -> dict[str, int]:
        bookings = self.db.list_bookings_by_status(
            ["unmatched_submitted", "unmatched_ocr_pending", "unmatched_pending_review"]
        )
        matched = 0
        still_unmatched = 0
        for booking in bookings:
            result = await self._sync_beds24_booking(booking.id, allow_status_update=True)
            if result is None:
                still_unmatched += 1
            else:
                matched += 1
        return {"checked": len(bookings), "matched": matched, "still_unmatched": still_unmatched}

    async def process_booking_ocr(self, booking_id: str) -> None:
        diag_logger.info(f"process_booking_ocr START booking_id={booking_id}")
        self.db.update_booking_status(booking_id, "ocr_processing")
        document_errors: list[str] = []
        for guest in self.db.list_guests(booking_id):
            merged_fields: dict[str, Any] = {}
            for document in self.db.list_documents_for_guest(guest.id):
                diag_logger.info(f"  OCR starting guest={guest.guest_index} doc={document.doc_type} url={document.source_url[:60]}...")
                await self._process_document(document)
                latest_document = self.db.fetch_document(document.id)
                if latest_document and latest_document.extracted:
                    merged_fields.update(_guest_fields_from_document(latest_document.doc_type, latest_document.extracted))
                    diag_logger.info(f"  OCR DONE guest={guest.guest_index} doc={document.doc_type} engine={latest_document.ocr_engine}")
                elif latest_document and latest_document.last_error:
                    err = latest_document.last_error
                    document_errors.append(f"{latest_document.doc_type}: {err}")
                    diag_logger.warning(f"  OCR FAILED guest={guest.guest_index} doc={document.doc_type} error={err[:150]}")
            self.db.update_guest_fields(guest.id, review_status="pending_review", fields=merged_fields)
        matched_booking = await self._sync_beds24_booking(booking_id, allow_status_update=False)
        final_status = "pending_review" if matched_booking is not None else "unmatched_pending_review"
        self.db.update_booking_status(booking_id, final_status)
        if matched_booking is None:
            self.db.delete_review_actions(booking_id=booking_id, actor="system", actions=["beds24_unmatched"])
            self.db.add_review_action(
                booking_id=booking_id,
                guest_id=None,
                actor="system",
                action="beds24_unmatched",
                note="Beds24 booking still not matched after OCR; use /beds24/refresh or bulk retry later.",
            )
        diag_logger.info(f"process_booking_ocr OCR done, status={final_status}, errors={len(document_errors)}")

        try:
            diag_logger.info(f"CALLING send_review_message for {booking_id}")
            await self.send_review_message(booking_id)
            diag_logger.info(f"send_review_message COMPLETED for {booking_id}")
        except Exception as exc:
            diag_logger.exception(f"send_review_message FAILED for {booking_id}")
            try:
                await self.telegram.send_text_message(
                    f"OCR/review notification failed for booking {booking_id}: {str(exc)[:200]}"
                )
            except Exception:
                diag_logger.exception("Even the failure notification text failed to send")

        try:
            await self.sync_booking_to_sheet(booking_id)
        except Exception as exc:
            self.db.add_review_action(
                booking_id=booking_id,
                guest_id=None,
                actor="system",
                action="sheet_sync_failed",
                note=str(exc)[:500],
            )

        if document_errors:
            self.db.add_review_action(
                booking_id=booking_id,
                guest_id=None,
                actor="system",
                action="ocr_warning",
                note=" | ".join(document_errors)[:500],
            )

    async def _process_document(self, document: DocumentRecord) -> None:
        try:
            ocr_engine, raw, extracted = await self.ocr.extract_document(doc_type=document.doc_type, source_url=document.source_url)
        except Exception as exc:
            self.db.update_document_after_ocr(
                document.id,
                ocr_status="failed",
                ocr_engine=None,
                ocr_raw=None,
                extracted=None,
                last_error=str(exc),
            )
            return

        self.db.update_document_after_ocr(
            document.id,
            ocr_status="done",
            ocr_engine=ocr_engine,
            ocr_raw=raw,
            extracted=extracted.model_dump(mode="json"),
            last_error=None,
        )

    async def send_review_message(self, booking_id: str) -> None:
        diag_logger.info(f"send_review_message: building summary + sending text for {booking_id}")
        main_summary, detail_summary = self._build_booking_review_messages(booking_id)
        await self.telegram.send_review_message(
            booking_id=booking_id,
            summary_text=main_summary,
        )
        if detail_summary:
            await self.telegram.send_text_message(detail_summary)
        diag_logger.info(f"send_review_message: text sent, now sending document images for {booking_id}")
        await self.telegram.send_document_images(
            booking_id=booking_id,
            documents=self._build_booking_document_payloads(booking_id),
        )
        diag_logger.info(f"send_review_message: images send attempted for {booking_id}")

    async def apply_review_action(self, booking_id: str, action: str, actor: str) -> str:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            raise ValueError("Booking not found.")

        if action == "approve_booking":
            await self._sync_beds24_booking(booking_id, allow_status_update=False)
            booking = self.db.fetch_booking(booking_id) or booking
            for guest in self.db.list_guests(booking_id):
                self.db.update_guest_fields(guest.id, review_status="approved")
                self.db.add_review_action(booking_id=booking_id, guest_id=guest.id, actor=actor, action="approved")
            self.db.update_booking_status(booking_id, "approved")
            self.db.add_review_action(booking_id=booking_id, guest_id=None, actor=actor, action="approve_booking")
            await self.beds24.trigger_checkin_instructions(
                external_booking_id=booking.external_booking_id,
                payload={"status": "approved"},
                hints=_beds24_lookup_hints_from_source_payload(booking.source_payload),
            )
            self.db.update_booking_status(booking_id, "approved_synced", instructions_sent=True)
            await self.sync_booking_to_sheet(booking_id)
            return "Booking approved and Beds24 updated."

        if action == "reject_booking":
            for guest in self.db.list_guests(booking_id):
                self.db.update_guest_fields(guest.id, review_status="rejected")
            self.db.update_booking_status(booking_id, "rejected")
            self.db.add_review_action(booking_id=booking_id, guest_id=None, actor=actor, action="reject_booking")
            await self.sync_booking_to_sheet(booking_id)
            return "Booking rejected."

        raise ValueError(f"Unsupported action: {action}")

    async def handle_telegram_command(self, *, text: str, actor: str) -> str:
        command_text = text.split()[0].split("@", 1)[0].lower()
        args = text.split()[1:]

        if command_text in {"/start", "/help"}:
            return (
                "Commands:\n"
                "/latest [n] - latest bookings\n"
                "/link <beds24_id> - create Jotform check-in link\n"
                "/note-link <beds24_id> - save Jotform check-in link to Beds24 notes\n"
                "/checkin-msg <id|beds24|submission> <door_code> - create guest check-in message\n"
                "/locks - list configured TTLock locks\n"
                "/door-code <id|beds24|submission> - create TTLock door code\n"
                "/status <id|beds24|submission> - booking summary\n"
                "/find <beds24_lookup_key> - find by Beds24 lookup key\n"
                "/refresh <id|beds24|submission> - retry Beds24 sync\n"
                "/sheet <id|beds24|submission> - push to Google Sheet\n"
                "/review <id|beds24|submission> - resend Telegram review\n"
                "/retry_unmatched - retry all unmatched bookings"
            )

        if command_text == "/latest":
            limit = 5
            if args:
                try:
                    limit = int(args[0])
                except ValueError:
                    return "Usage: /latest [n]"
            bookings = self.db.list_recent_bookings(limit)
            if not bookings:
                return "No bookings found."
            return "Latest bookings:\n" + "\n".join(self._booking_compact_line(booking) for booking in bookings)

        if command_text == "/retry_unmatched":
            result = await self.retry_unmatched_beds24_bookings()
            return (
                "Retry unmatched completed.\n"
                f"Checked: {result['checked']}\n"
                f"Matched: {result['matched']}\n"
                f"Still unmatched: {result['still_unmatched']}"
            )

        if command_text == "/link":
            if not args:
                return "Usage: /link <Beds24 booking id>"
            beds24_booking_id = args[0].strip()
            if not beds24_booking_id:
                return "Usage: /link <Beds24 booking id>"
            return (
                f"Check-in link for Beds24 booking {beds24_booking_id}:\n"
                f"{self.build_checkin_link(beds24_booking_id)}"
            )

        if command_text == "/note-link":
            if not args:
                return "Usage: /note-link <Beds24 booking id>"
            beds24_booking_id = args[0].strip()
            if not beds24_booking_id:
                return "Usage: /note-link <Beds24 booking id>"
            try:
                link = await self.save_checkin_link_to_beds24_notes(beds24_booking_id)
            except Exception as exc:
                return (
                    f"Could not update Beds24 note for {beds24_booking_id}.\n"
                    f"Error: {str(exc)[:300]}\n"
                    f"Manual link:\n{self.build_checkin_link(beds24_booking_id)}"
                )
            return (
                f"Beds24 note updated for {beds24_booking_id}.\n"
                f"Check-in link:\n{link}"
            )

        if command_text == "/locks":
            return await self._telegram_locks_text()

        if not args:
            return f"Usage: {command_text} <id|beds24|submission>"

        identifier = args[0]
        booking = self._resolve_booking(identifier)
        if booking is None:
            return f"Booking not found for: {identifier}"

        if command_text == "/find":
            return self._booking_compact_line(booking)

        if command_text == "/status":
            return self._telegram_status_text(booking.id)

        if command_text == "/checkin-msg":
            if len(args) < 2:
                return "Usage: /checkin-msg <id|beds24|submission> <door_code>"
            try:
                await self._sync_beds24_booking(booking.id, allow_status_update=False)
            except Exception as exc:
                diag_logger.warning("Beds24 sync before checkin-msg failed for booking_id=%s: %s", booking.id, exc)
            return self._build_guest_checkin_message(booking.id, door_code=" ".join(args[1:]).strip())

        if command_text == "/door-code":
            try:
                return await self._create_door_code_message(booking.id, actor=actor)
            except Exception as exc:
                diag_logger.exception("TTLock door-code failed for booking_id=%s", booking.id)
                return f"Could not create TTLock door code.\nError: {str(exc)[:300]}"

        if command_text == "/refresh":
            beds24_booking = await self.refresh_beds24_booking(booking.id)
            if beds24_booking is None:
                return f"Beds24 still not matched for {booking.external_booking_id}"
            return (
                f"Beds24 refresh done.\n"
                f"Internal ID: {booking.id}\n"
                f"Beds24 Lookup Key: {booking.external_booking_id}\n"
                f"Beds24 ID: {beds24_booking.get('id') or '-'}"
            )

        if command_text == "/sheet":
            await self.sync_booking_to_sheet(booking.id)
            return (
                f"Sheet sync completed.\n"
                f"Internal ID: {booking.id}\n"
                f"Submission ID: {booking.jotform_submission_id or '-'}"
            )

        if command_text == "/review":
            await self.send_review_message(booking.id)
            return f"Review resent for {booking.id}"

        return "Unknown command. Use /help"

    async def _telegram_locks_text(self) -> str:
        if not self.ttlock.enabled():
            return "TTLock is not configured."
        locks = await self.ttlock.list_locks()
        configured_ids = {str(lock_id) for lock_id in self.ttlock.configured_lock_ids()}
        if not locks:
            return "No TTLock locks returned by API."
        lines = ["TTLock locks:"]
        for lock in locks:
            lock_id = str(lock.get("lockId") or "-")
            marker = "configured" if lock_id in configured_ids else "-"
            name = lock.get("lockAlias") or lock.get("lockName") or "-"
            battery = lock.get("electricQuantity") or lock.get("battery") or "-"
            gateway = lock.get("hasGateway")
            lines.append(f"{lock_id} | {name} | battery={battery} | gateway={gateway} | {marker}")
        return "\n".join(lines)

    async def _create_door_code_message(self, booking_id: str, *, actor: str) -> str:
        try:
            await self._sync_beds24_booking(booking_id, allow_status_update=False)
        except Exception as exc:
            diag_logger.warning("Beds24 sync before door-code failed for booking_id=%s: %s", booking_id, exc)

        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            return "Booking not found."

        view = self.get_booking_view(booking_id)
        beds24_metadata = _beds24_metadata(booking.source_payload)
        booking_metadata = _booking_metadata(booking.source_payload)
        check_in = beds24_metadata.get("arrival") or booking_metadata.get("check_in")
        check_out = beds24_metadata.get("departure") or booking_metadata.get("check_out")
        if not check_in or not check_out:
            raise ValueError("Missing check-in/check-out dates for TTLock code.")

        target_lock_ids = self._ttlock_lock_ids_for_booking(beds24_metadata)
        start_ms = _local_datetime_ms(check_in, self.beds24.settings.ttlock_code_start_time)
        end_ms = _local_datetime_ms(check_out, self.beds24.settings.ttlock_code_end_time)
        first_guest = view.guests[0] if view.guests else {}
        guest_name = first_guest.get("full_name") or view.lead_guest_name or "Guest"

        result = await self.ttlock.add_booking_passcode_to_locks(
            booking_id=view.external_booking_id,
            guest_name=str(guest_name),
            start_ms=start_ms,
            end_ms=end_ms,
            lock_ids=target_lock_ids,
        )
        code = str(result["passcode"])
        successes = result.get("successes") or []
        failures = result.get("failures") or []

        self.db.add_review_action(
            booking_id=booking_id,
            guest_id=None,
            actor=actor,
            action="door_code_created",
            note=f"TTLock code created for {len(successes)} lock(s); failures={len(failures)}",
        )

        status_lines = [
            "TTLock door code created.",
            f"Booking: {view.external_booking_id}",
            f"Property: {beds24_metadata.get('property_id') or '-'}",
            f"Valid: {check_in} {self.beds24.settings.ttlock_code_start_time} -> {check_out} {self.beds24.settings.ttlock_code_end_time}",
            f"Code: {code}",
            f"Locks OK: {', '.join(str(item.get('lock_id')) for item in successes) or '-'}",
        ]
        if failures:
            status_lines.append(
                "Locks failed: "
                + ", ".join(f"{item.get('lock_id')} ({str(item.get('error'))[:80]})" for item in failures)
            )
        return "\n".join(status_lines) + "\n\n" + self._build_guest_checkin_message(booking_id, door_code=code)

    def _ttlock_lock_ids_for_booking(self, beds24_metadata: dict[str, Any]) -> tuple[int, ...]:
        property_id = str(beds24_metadata.get("property_id") or "").strip()
        mapping = self.beds24.settings.ttlock_property_lock_ids
        raw_lock_ids = mapping.get(property_id) if property_id else None
        if raw_lock_ids:
            return tuple(int(lock_id) for lock_id in raw_lock_ids)
        return self.ttlock.configured_lock_ids()

    def build_checkin_link(self, beds24_booking_id: str) -> str:
        base_url = self.beds24.settings.jotform_form_url.strip()
        if not base_url:
            return "JOTFORM_FORM_URL is not configured."
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}{urlencode({'BOOKINGID': beds24_booking_id})}"

    async def save_checkin_link_to_beds24_notes(self, beds24_booking_id: str) -> str:
        booking = await self.beds24.find_booking(beds24_booking_id)
        if booking is None:
            raise ValueError(f"Beds24 booking not found: {beds24_booking_id}")
        link = self.build_checkin_link(str(booking.get("id") or beds24_booking_id))
        await self.beds24.persist_checkin_link_note(booking, link)
        return link

    async def _sync_beds24_booking(self, booking_id: str, *, allow_status_update: bool) -> dict[str, Any] | None:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            raise ValueError("Booking not found.")

        hints = _beds24_lookup_hints_from_source_payload(booking.source_payload)
        beds24_booking = await self.beds24.find_booking(booking.external_booking_id, hints=hints)
        if beds24_booking is None:
            return None

        merged_payload = _merge_beds24_booking(
            booking.source_payload,
            beds24_booking,
            self.beds24.settings.beds24_room_labels,
        )
        self.db.update_booking_source_payload(booking_id, merged_payload)
        self.db.delete_review_actions(booking_id=booking_id, actor="system", actions=["beds24_unmatched"])
        if allow_status_update and booking.status.startswith("unmatched_"):
            next_status = booking.status.removeprefix("unmatched_")
            self.db.update_booking_status(booking_id, next_status)
        try:
            await self.beds24.persist_lookup_marker(
                beds24_booking,
                booking.external_booking_id,
                jotform_submission_id=booking.jotform_submission_id,
                hints=hints,
            )
        except Exception as exc:
            diag_logger.warning(
                f"Beds24 lookup marker sync failed during refresh for booking_id={booking_id}: {exc}"
            )
        return beds24_booking

    async def sync_booking_to_sheet(self, booking_id: str) -> None:
        view = self.get_booking_view(booking_id)
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            raise ValueError("Booking not found.")
        source_payload = booking.source_payload
        booking_metadata = _booking_metadata(source_payload)
        beds24_metadata = _beds24_metadata(source_payload)
        parse_debug = _parse_debug(source_payload)
        payload = view.model_dump(mode="json")
        review_notes = [action.get("note") for action in payload.get("review_actions", []) if action.get("note")]
        payload["source_payload_meta"] = {
            "booking_metadata": booking_metadata,
            "beds24_booking": beds24_metadata,
            "parse_debug": parse_debug,
        }
        payload["review_notes"] = review_notes
        payload["latest_note"] = review_notes[-1] if review_notes else None
        payload["sheet_sync_context"] = {
            "synced_at_utc": datetime.now(timezone.utc).isoformat(),
            "booking_created_at": booking.created_at,
            "booking_updated_at": booking.updated_at,
            "effective_lookup_key": booking_metadata.get("beds24_booking_id")
            or booking_metadata.get("beds24_api_reference")
            or booking_metadata.get("legacy_booking_id")
            or booking_metadata.get("booking_id")
            or booking.external_booking_id,
        }
        await self.sheet_mirror.sync_booking(payload)

    def _resolve_booking(self, identifier: str) -> BookingView | Any:
        booking = self.db.fetch_booking(identifier)
        if booking is not None:
            return booking
        booking = self.db.fetch_booking_by_submission_id(identifier)
        if booking is not None:
            return booking
        booking = self.db.fetch_booking_by_external_booking_id(identifier)
        if booking is not None:
            return booking
        return None

    def _booking_compact_line(self, booking) -> str:
        return (
            f"{booking.external_booking_id} | internal={booking.id} | "
            f"submission={booking.jotform_submission_id or '-'} | status={booking.status} | "
            f"guest={booking.lead_guest_name or '-'}"
        )

    def _telegram_status_text(self, booking_id: str) -> str:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            return "Booking not found."
        view = self.get_booking_view(booking_id)
        source_payload = booking.source_payload
        booking_metadata = _booking_metadata(source_payload)
        beds24_metadata = _beds24_metadata(source_payload)
        first_guest = view.guests[0] if view.guests else {}
        return (
            "Booking status\n"
            f"Internal ID: {view.id}\n"
            f"Beds24 Lookup Key: {view.external_booking_id}\n"
            f"Submission ID: {view.jotform_submission_id or '-'}\n"
            f"Status: {view.status}\n"
            f"Guest: {first_guest.get('full_name') or view.lead_guest_name or '-'}\n"
            f"Passport: {first_guest.get('passport_number') or '-'}\n"
            f"Room: {self._room_text_from_payload(source_payload)}\n"
            f"Stay: {(beds24_metadata.get('arrival') or booking_metadata.get('check_in') or '-')}"
            f" -> {(beds24_metadata.get('departure') or booking_metadata.get('check_out') or '-')}\n"
            f"Channel: {beds24_metadata.get('channel') or '-'}"
        )

    def _room_text_from_payload(self, source_payload: dict[str, Any]) -> str:
        booking_metadata = _booking_metadata(source_payload)
        beds24_metadata = _beds24_metadata(source_payload)
        room_id = beds24_metadata.get("room_id")
        unit_id = beds24_metadata.get("unit_id")
        room_labels = self.beds24.settings.beds24_room_labels
        for key in (
            f"{room_id}:{unit_id}" if room_id and unit_id not in (None, "") else "",
            str(room_id or ""),
        ):
            if key and room_labels.get(key):
                return str(room_labels[key]).strip()
        return booking_metadata.get("room_name") or beds24_metadata.get("room_label") or "-"

    def _build_guest_checkin_message(self, booking_id: str, *, door_code: str) -> str:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            return "Booking not found."
        view = self.get_booking_view(booking_id)
        source_payload = booking.source_payload
        booking_metadata = _booking_metadata(source_payload)
        beds24_metadata = _beds24_metadata(source_payload)
        first_guest = view.guests[0] if view.guests else {}
        guest_name = first_guest.get("full_name") or view.lead_guest_name or "Guest"
        room = self._room_text_from_payload(source_payload)
        room_number = _room_number_from_text(room)
        floor = _floor_from_room_number(room_number)
        check_in = beds24_metadata.get("arrival") or booking_metadata.get("check_in") or "-"
        check_out = beds24_metadata.get("departure") or booking_metadata.get("check_out") or "-"
        settings = self.beds24.settings
        wifi_name = settings.guest_message_wifi_name or "-"
        wifi_password = settings.guest_message_wifi_password or "-"
        directions_url = settings.guest_message_directions_url or "-"
        brand = settings.guest_message_brand or "Mandala Living"

        return (
            "COPY MESSAGE TO GUEST\n"
            "---------------------\n"
            f"Hello {guest_name},\n\n"
            "Your check-in has been confirmed. Please find the details below:\n\n"
            "Booking Details\n\n"
            f"Booking ID: {view.external_booking_id}\n"
            f"Unit: {room}\n"
            f"Check-in: {check_in} (from {settings.ttlock_code_start_time})\n"
            f"Check-out: {check_out} (before {settings.ttlock_code_end_time})\n\n"
            "Self Check-in Instructions\n\n"
            f"Main entrance code: {door_code or '-'}#\n"
            f"Your room: {room_number or room}, {floor or '-'}\n"
            "Please keep this code private and do not share it with anyone outside your booking.\n\n"
            f"Wifi: {wifi_name} / Password: {wifi_password}\n\n"
            f"Directions: {directions_url}\n\n"
            "Feel free to message us anytime during your stay if you need anything!\n\n"
            "Thank you,\n"
            f"{brand}"
        )

    def get_booking_view(self, booking_id: str) -> BookingView:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            raise ValueError("Booking not found.")
        guests_payload: list[dict[str, Any]] = []
        for guest in self.db.list_guests(booking_id):
            guests_payload.append(
                {
                    "id": guest.id,
                    "guest_index": guest.guest_index,
                    "full_name": guest.full_name,
                    "date_of_birth": guest.date_of_birth,
                    "gender": guest.gender,
                    "nationality": guest.nationality,
                    "passport_number": guest.passport_number,
                    "visa_number": guest.visa_number,
                    "review_status": guest.review_status,
                    "source_payload": guest.source_payload,
                    "documents": [
                        {
                            "id": document.id,
                            "doc_type": document.doc_type,
                            "source_url": document.source_url,
                            "ocr_status": document.ocr_status,
                            "last_error": document.last_error,
                        }
                        for document in self.db.list_documents_for_guest(guest.id)
                    ],
                }
            )
        review_actions = [
            {
                "id": action.id,
                "guest_id": action.guest_id,
                "actor": action.actor,
                "action": action.action,
                "note": action.note,
                "created_at": action.created_at,
            }
            for action in self.db.list_review_actions(booking_id)
        ]
        return BookingView(
            id=booking.id,
            external_booking_id=booking.external_booking_id,
            jotform_submission_id=booking.jotform_submission_id,
            lead_guest_name=booking.lead_guest_name,
            guest_count=booking.guest_count,
            status=booking.status,
            instructions_sent=booking.instructions_sent,
            guests=guests_payload,
            review_actions=review_actions,
        )

    def _build_booking_review_messages(self, booking_id: str) -> tuple[str, str | None]:
        view = self.get_booking_view(booking_id)
        booking = self.db.fetch_booking(booking_id)
        metadata = _booking_metadata(booking.source_payload if booking else {})
        beds24 = _beds24_metadata(booking.source_payload if booking else {})
        parse_debug = _parse_debug(booking.source_payload if booking else {})
        pretty = _pretty_payload(booking.source_payload if booking else {})
        stay_line = f"{beds24.get('arrival') or metadata.get('check_in') or '-'} -> {beds24.get('departure') or metadata.get('check_out') or '-'}"
        source_line = ", ".join(
            item
            for item in [
                f"parse={parse_debug.get('parse_source')}" if parse_debug.get("parse_source") else "",
                f"rawBody={parse_debug.get('raw_body_length')} chars" if parse_debug.get("raw_body_length") else "",
            ]
            if item
        ) or "-"
        lines = [
            "MANDALA CHECK-IN REVIEW",
            "",
            "----------------",
            "BOOKING INFO",
            "----------------",
            f"Beds24 lookup key: {view.external_booking_id}",
            f"Jotform submission: {view.jotform_submission_id or '-'}",
            f"Beds24 ID: {beds24.get('id') or '-'}",
            f"Room: {self._room_text_from_payload(booking.source_payload if booking else {})}",
            f"Stay: {stay_line}",
            f"Guest count: {view.guest_count}",
            f"Status: {view.status}",
            f"Channel: {beds24.get('channel') or '-'}",
            f"Email: {metadata.get('email') or '-'}",
            f"Source: {source_line}",
            "",
        ]
        for guest in view.guests:
            guest_source = guest.get("source_payload") or {}
            source_docs = [
                item
                for item in [
                    f"passport_url={_shorten_text(_guest_document_url(guest, 'passport'))}" if _guest_document_url(guest, "passport") else "",
                    f"visa_url={_shorten_text(_guest_document_url(guest, 'visa'))}" if _guest_document_url(guest, "visa") else "",
                ]
                if item
            ]
            phone_number = _guest_phone_number(guest)
            document_status = ", ".join(
                f"{document['doc_type']}={document['ocr_status']}"
                + (
                    f"({document['last_error'][:40]})"
                    if document.get("last_error")
                    else ""
                )
                for document in guest["documents"]
            ) or "-"
            extracted_ids = ", ".join(
                item
                for item in [
                    f"passport={guest['passport_number']}" if guest["passport_number"] else "",
                    f"visa={guest['visa_number']}" if guest["visa_number"] else "",
                ]
                if item
            ) or "-"
            lines.extend(
                [
                    "----------------",
                    f"GUEST INFO - GUEST {guest['guest_index']}",
                    "----------------",
                    f"Name: {guest['full_name'] or '-'}",
                    f"Phone: {phone_number or '-'}",
                    f"Passport/ID: {guest['passport_number'] or '-'}",
                    f"DOB: {guest['date_of_birth'] or '-'}",
                    f"Gender: {guest['gender'] or '-'}",
                    f"Nationality: {guest['nationality'] or '-'}",
                    f"Review: {guest['review_status']}",
                    "",
                    "OCR STATUS",
                    "----------------",
                    f"Document OCR: {document_status}",
                    f"Extracted IDs: {extracted_ids}",
                    f"Source docs: {', '.join(source_docs) or '-'}",
                    "",
                ]
            )
        beds24_booking_id_field = parse_debug.get("beds24_booking_id")
        beds24_api_reference_field = parse_debug.get("beds24_api_reference")
        legacy_booking_id_field = parse_debug.get("legacy_booking_id")
        booking_id_from_form = metadata.get("booking_id")
        q32_used_as_beds24_key = (
            booking_id_from_form
            and legacy_booking_id_field
            and booking_id_from_form == legacy_booking_id_field
            and not beds24_booking_id_field
            and not beds24_api_reference_field
        )
        detail_lines = [
            "MANDALA CHECK-IN DETAILS",
            "",
            "----------------",
            "SOURCE / PARSE",
            "----------------",
            f"Parse source: {parse_debug.get('parse_source') or '-'}",
            f"Raw body chars: {parse_debug.get('raw_body_length') or '-'}",
            f"Pretty recovered: {'yes' if pretty else 'no'}",
            f"Booking ID from form: {booking_id_from_form or '-'}",
            f"Beds24 booking id field: {beds24_booking_id_field or '-'}",
            f"Beds24 apiReference field: {beds24_api_reference_field or '-'}",
            f"Legacy q32_BOOKINGID received: {legacy_booking_id_field or '-'}",
            (
                f"Effective Beds24 lookup key: {legacy_booking_id_field} (from q32_BOOKINGID fallback)"
                if q32_used_as_beds24_key
                else (
                    f"Effective Beds24 lookup key: {beds24_booking_id_field} (from beds24_booking_id field)"
                    if beds24_booking_id_field
                    else (
                        f"Effective Beds24 lookup key: {beds24_api_reference_field} (from beds24_api_reference field)"
                        if beds24_api_reference_field
                        else f"Effective Beds24 lookup key: {booking_id_from_form or '-'}"
                    )
                )
            ),
            "Ops note: the value after BOOKINGID= in the form link should be a real Beds24 booking id.",
            f"Check-in: {metadata.get('check_in') or '-'}",
            f"Check-out: {metadata.get('check_out') or '-'}",
            f"Terms accepted: {metadata.get('terms_accepted') or '-'}",
            f"Signature: {_shorten_text(str(metadata.get('signature') or '-'))}",
            "",
        ]
        if pretty:
            guest_prefixes = tuple(f"guest_{idx}_" for idx in range(1, view.guest_count + 1))
            pretty_guest_keys = [
                key
                for key in sorted(pretty.keys())
                if key.startswith("guest_") and key.startswith(guest_prefixes)
            ]
            extra_guest_keys = [
                key
                for key in sorted(pretty.keys())
                if key.startswith("guest_") and key not in pretty_guest_keys
            ]
            detail_lines.extend(
                [
                    "PRETTY RAW BODY FIELDS",
                    "----------------",
                    f"Reservation code: {pretty.get('reservation_code') or pretty.get('BOOKINGID') or '-'}",
                    f"Guest count: {pretty.get('guest_count') or pretty.get('number_of_guests') or '-'}",
                    f"Email: {pretty.get('email') or '-'}",
                ]
            )
            for key in pretty_guest_keys[:20]:
                detail_lines.append(f"{key}: {_shorten_text(str(pretty.get(key) or '-'))}")
            if extra_guest_keys:
                detail_lines.append(
                    f"Extra guest fields: {', '.join(extra_guest_keys[:5])}"
                    + (" ..." if len(extra_guest_keys) > 5 else "")
                )
            detail_lines.append("")
        detail_lines.extend(
            [
                "REVIEW NOTES",
                "----------------",
                f"Latest note: {self._latest_review_note(view) or '-'}",
                "",
            ]
        )
        if len(detail_lines) <= 8:
            detail_summary = None
        else:
            detail_summary = "\n".join(detail_lines)
        return "\n".join(lines), detail_summary

    def _build_booking_summary(self, booking_id: str) -> str:
        main_summary, _ = self._build_booking_review_messages(booking_id)
        return main_summary

    def _build_booking_document_payloads(self, booking_id: str) -> list[dict[str, Any]]:
        booking = self.db.fetch_booking(booking_id)
        if booking is None:
            return []

        payloads: list[dict[str, Any]] = []
        for guest in self.db.list_guests(booking_id):
            for document in self.db.list_documents_for_guest(guest.id):
                payloads.append(
                    {
                        "external_booking_id": booking.external_booking_id,
                        "guest_index": guest.guest_index,
                        "guest_name": guest.full_name,
                        "doc_type": document.doc_type,
                        "source_url": document.source_url,
                    }
                )
        return payloads

    def _latest_review_note(self, view: BookingView) -> str | None:
        for action in reversed(view.review_actions):
            note = action.get("note")
            if note:
                return str(note)
        return None


def _guest_fields_from_document(doc_type: str, extracted: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if extracted.get("full_name"):
        fields["full_name"] = extracted["full_name"]
    if extracted.get("date_of_birth"):
        fields["date_of_birth"] = extracted["date_of_birth"]
    if extracted.get("gender"):
        fields["gender"] = extracted["gender"]
    if extracted.get("nationality"):
        fields["nationality"] = extracted["nationality"]
    if doc_type == "passport" and extracted.get("passport_number"):
        fields["passport_number"] = extracted["passport_number"]
    if doc_type == "visa" and extracted.get("visa_number"):
        fields["visa_number"] = extracted["visa_number"]
    return fields


def _booking_metadata(source_payload: dict[str, Any]) -> dict[str, Any]:
    metadata = source_payload.get("booking_metadata")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _beds24_metadata(source_payload: dict[str, Any]) -> dict[str, Any]:
    metadata = source_payload.get("beds24_booking")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _pretty_payload(source_payload: dict[str, Any]) -> dict[str, Any]:
    pretty = source_payload.get("pretty")
    if isinstance(pretty, dict):
        return pretty
    return {}


def _parse_debug(source_payload: dict[str, Any]) -> dict[str, Any]:
    parse_debug = source_payload.get("parse_debug")
    booking_metadata = _booking_metadata(source_payload)
    if isinstance(parse_debug, dict):
        merged = dict(parse_debug)
        merged.setdefault("parse_source", booking_metadata.get("parse_source"))
        merged.setdefault("raw_body_length", booking_metadata.get("raw_body_length"))
        merged.setdefault("beds24_booking_id", booking_metadata.get("beds24_booking_id"))
        merged.setdefault("beds24_api_reference", booking_metadata.get("beds24_api_reference"))
        merged.setdefault("legacy_booking_id", booking_metadata.get("legacy_booking_id"))
        return merged
    return {
        "parse_source": booking_metadata.get("parse_source"),
        "raw_body_length": booking_metadata.get("raw_body_length"),
        "beds24_booking_id": booking_metadata.get("beds24_booking_id"),
        "beds24_api_reference": booking_metadata.get("beds24_api_reference"),
        "legacy_booking_id": booking_metadata.get("legacy_booking_id"),
    }


def _room_number_from_text(room_text: str) -> str | None:
    match = re.search(r"\b(\d{3,4})\b", str(room_text or ""))
    return match.group(1) if match else None


def _floor_from_room_number(room_number: str | None) -> str | None:
    if not room_number or len(room_number) < 3:
        return None
    return f"Floor {room_number[0]}"


def _local_datetime_ms(date_text: str, time_text: str) -> int:
    date_value = datetime.strptime(str(date_text).strip()[:10], "%Y-%m-%d").date()
    hour_text, minute_text = (str(time_text or "13:00").strip().split(":", 1) + ["00"])[:2]
    local_dt = datetime.combine(
        date_value,
        dt_time(hour=int(hour_text), minute=int(minute_text[:2])),
        tzinfo=ZoneInfo("Asia/Bangkok"),
    )
    return int(local_dt.timestamp() * 1000)


def _merge_beds24_booking(
    source_payload: dict[str, Any],
    booking: dict[str, Any],
    room_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged = dict(source_payload)
    existing_metadata = dict(_booking_metadata(source_payload))
    room_label = _room_label(booking, room_labels)
    merged["beds24_booking"] = {
        "id": booking.get("id"),
        "master_id": booking.get("masterId"),
        "property_id": booking.get("propertyId"),
        "room_id": booking.get("roomId"),
        "unit_id": booking.get("unitId"),
        "room_label": room_label,
        "arrival": booking.get("arrival"),
        "departure": booking.get("departure"),
        "status": booking.get("status"),
        "channel": booking.get("channel"),
        "api_reference": booking.get("apiReference"),
        "reference": booking.get("reference"),
        "referer": booking.get("referer"),
        "first_name": booking.get("firstName"),
        "last_name": booking.get("lastName"),
        "allow_auto_action": booking.get("allowAutoAction"),
    }

    if not existing_metadata.get("room_name"):
        existing_metadata["room_name"] = room_label
    if not existing_metadata.get("check_in"):
        existing_metadata["check_in"] = booking.get("arrival")
    if not existing_metadata.get("check_out"):
        existing_metadata["check_out"] = booking.get("departure")
    merged["booking_metadata"] = existing_metadata
    return merged


def _room_label(booking: dict[str, Any], room_labels: dict[str, str] | None = None) -> str:
    room_id = booking.get("roomId")
    unit_id = booking.get("unitId")
    labels = room_labels or {}
    for key in (
        f"{room_id}:{unit_id}" if room_id and unit_id not in (None, "") else "",
        str(room_id or ""),
    ):
        if key and labels.get(key):
            return str(labels[key]).strip()

    for key in ("roomName", "roomLabel", "unitName", "name"):
        value = booking.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if room_id and unit_id not in (None, ""):
        return f"Room {room_id} / Unit {unit_id}"
    if room_id:
        return f"Room {room_id}"
    if unit_id not in (None, ""):
        return f"Unit {unit_id}"
    return ""


def _guest_phone_number(guest: dict[str, Any]) -> str | None:
    return guest.get("source_payload", {}).get("phone_number")


def _beds24_lookup_hints_from_booking_input(payload: BookingInput) -> dict[str, Any]:
    booking_metadata = _booking_metadata(payload.raw_payload)
    guest_names = [guest.full_name for guest in payload.guests if guest.full_name]
    phone_numbers = [guest.phone_number for guest in payload.guests if guest.phone_number]
    first_guest = payload.guests[0] if payload.guests else None
    return {
        "external_booking_id": payload.external_booking_id,
        "submission_id": payload.jotform_submission_id,
        "email": booking_metadata.get("email"),
        "lead_guest_name": payload.lead_guest_name or (first_guest.full_name if first_guest else None),
        "full_name": first_guest.full_name if first_guest and first_guest.full_name else None,
        "first_name": (first_guest.full_name.split()[0] if first_guest and first_guest.full_name else None),
        "last_name": (first_guest.full_name.split()[-1] if first_guest and first_guest.full_name and len(first_guest.full_name.split()) > 1 else None),
        "guest_names": guest_names,
        "phone_numbers": phone_numbers,
        "check_in": booking_metadata.get("check_in"),
        "check_out": booking_metadata.get("check_out"),
        "room_name": booking_metadata.get("room_name"),
        "room_label": booking_metadata.get("room_name"),
        "channel": booking_metadata.get("channel"),
    }


def _beds24_lookup_hints_from_source_payload(source_payload: dict[str, Any]) -> dict[str, Any]:
    booking_metadata = _booking_metadata(source_payload)
    pretty = _pretty_payload(source_payload)

    guest_names: list[str] = []
    phone_numbers: list[str] = []
    for key, value in pretty.items():
        if not value:
            continue
        if key.endswith("_full_name"):
            guest_names.append(str(value))
        elif key.endswith("_phone_number"):
            phone_numbers.append(str(value))

    lead_guest_name = booking_metadata.get("lead_guest_name") or pretty.get("guest_1_full_name")
    first_guest_name = pretty.get("guest_1_full_name") or lead_guest_name
    first_name = None
    last_name = None
    if isinstance(first_guest_name, str):
        parts = [part for part in first_guest_name.split() if part]
        if parts:
            first_name = parts[0]
            if len(parts) > 1:
                last_name = parts[-1]

    email = booking_metadata.get("email") or pretty.get("email")
    room_name = booking_metadata.get("room_name")
    return {
        "external_booking_id": booking_metadata.get("booking_id"),
        "submission_id": booking_metadata.get("submission_id"),
        "email": email,
        "lead_guest_name": lead_guest_name,
        "full_name": first_guest_name,
        "first_name": first_name,
        "last_name": last_name,
        "guest_names": guest_names,
        "phone_numbers": phone_numbers,
        "check_in": booking_metadata.get("check_in"),
        "check_out": booking_metadata.get("check_out"),
        "room_name": room_name,
        "room_label": room_name,
        "channel": None,
    }


def _guest_document_url(guest: dict[str, Any], doc_type: str) -> str | None:
    for document in guest.get("documents", []):
        if document.get("doc_type") == doc_type:
            return document.get("source_url")
    return None


def _shorten_text(value: str, *, limit: int = 120) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
