const SHEET_NAME = 'Bot Sync';
const DRIVE_ROOT_FOLDER_ID = '13NroK4UJiuIJSwgbbLjqjxMW9zOeeeKp';

function doGet() {
  return _jsonResponse({ status: 'ok', sheet: SHEET_NAME });
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _jsonResponse({ status: 'error', message: 'Missing JSON body' }, 400);
    }

    const payload = JSON.parse(e.postData.contents);
    const sheet = _getOrCreateSheet_();
    const headers = _buildHeaders_();
    _ensureHeaders_(sheet, headers);

    const rowData = _buildRowData_(payload, headers);
    const rowIndex = _findExistingRow_(sheet, payload);

    if (rowIndex > 1) {
      sheet.getRange(rowIndex, 1, 1, headers.length).setValues([rowData]);
      return _jsonResponse({ status: 'updated', row: rowIndex });
    }

    sheet.appendRow(rowData);
    return _jsonResponse({ status: 'inserted', row: sheet.getLastRow() });
  } catch (error) {
    return _jsonResponse(
      {
        status: 'error',
        message: String(error && error.message ? error.message : error),
        stack: String(error && error.stack ? error.stack : ''),
      },
      500
    );
  }
}

function _getOrCreateSheet_() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  return spreadsheet.getSheetByName(SHEET_NAME) || spreadsheet.insertSheet(SHEET_NAME);
}

function _buildHeaders_() {
  const headers = [
    'Synced At UTC',
    'Internal Booking ID',
    'Beds24 Lookup Key',
    'Jotform Submission ID',
    'Google Drive Folder',
    'Beds24 ID',
    'Beds24 API Reference',
    'Booking Status',
    'Instructions Sent',
    'Lead Guest Name',
    'Guest Count',
    'Email',
    'Phone',
    'Check In',
    'Check Out',
    'Room',
    'Channel',
    'Parse Source',
    'Raw Body Length',
    'Latest Note',
    'Review Notes',
  ];

  for (let guestIndex = 1; guestIndex <= 4; guestIndex += 1) {
    headers.push(
      `Guest ${guestIndex} Name`,
      `Guest ${guestIndex} Review Status`,
      `Guest ${guestIndex} Phone`,
      `Guest ${guestIndex} Passport`,
      `Guest ${guestIndex} Visa`,
      `Guest ${guestIndex} DOB`,
      `Guest ${guestIndex} Gender`,
      `Guest ${guestIndex} Nationality`,
      `Guest ${guestIndex} Passport OCR`,
      `Guest ${guestIndex} Visa OCR`
    );
  }

  return headers;
}

function _ensureHeaders_(sheet, headers) {
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
    return;
  }

  const currentHeaders = sheet
    .getRange(1, 1, 1, Math.max(sheet.getLastColumn(), headers.length))
    .getValues()[0];

  const changed =
    currentHeaders.length < headers.length ||
    headers.some((header, index) => String(currentHeaders[index] || '') !== header);

  if (changed) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }
}

function _findExistingRow_(sheet, payload) {
  const bookingId = String(payload.id || '').trim();
  const submissionId = String(payload.jotform_submission_id || '').trim();
  if (!bookingId && !submissionId) {
    return -1;
  }

  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return -1;
  }

  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const internalBookingIndex = headers.indexOf('Internal Booking ID');
  const submissionIndex = headers.indexOf('Jotform Submission ID');
  if (internalBookingIndex < 0 && submissionIndex < 0) {
    return -1;
  }

  const values = sheet.getRange(2, 1, lastRow - 1, sheet.getLastColumn()).getValues();

  for (let rowOffset = 0; rowOffset < values.length; rowOffset += 1) {
    const row = values[rowOffset];
    const existingBookingId = internalBookingIndex >= 0 ? String(row[internalBookingIndex] || '').trim() : '';
    const existingSubmissionId = submissionIndex >= 0 ? String(row[submissionIndex] || '').trim() : '';

    // Submission ID is the most stable per-form-submit key.
    // A guest can resubmit the same Beds24 booking later, and that should create a new row.
    if (submissionId && existingSubmissionId === submissionId) {
      return rowOffset + 2;
    }
    if (!submissionId && bookingId && existingBookingId === bookingId) {
      return rowOffset + 2;
    }
  }

  return -1;
}

function _buildRowData_(payload, headers) {
  const meta = payload.source_payload_meta || {};
  const bookingMeta = meta.booking_metadata || {};
  const beds24 = meta.beds24_booking || {};
  const parseDebug = meta.parse_debug || {};
  const syncContext = payload.sheet_sync_context || {};
  const guests = Array.isArray(payload.guests) ? payload.guests : [];
  const guest1 = guests[0] || {};
  const guest1Source = guest1.source_payload || {};

  const rowMap = {
    'Synced At UTC': syncContext.synced_at_utc || new Date().toISOString(),
    'Internal Booking ID': payload.id || '',
    'Beds24 Lookup Key': syncContext.effective_lookup_key || payload.external_booking_id || '',
    'Jotform Submission ID': payload.jotform_submission_id || '',
    'Google Drive Folder': _findDriveFolderLink_(payload.jotform_submission_id),
    'Beds24 ID': beds24.id || '',
    'Beds24 API Reference': beds24.apiReference || '',
    'Booking Status': payload.status || '',
    'Instructions Sent': payload.instructions_sent ? 'yes' : 'no',
    'Lead Guest Name': payload.lead_guest_name || '',
    'Guest Count': payload.guest_count || '',
    'Email': bookingMeta.email || '',
    'Phone': guest1Source.phone_number || '',
    'Check In': beds24.arrival || bookingMeta.check_in || '',
    'Check Out': beds24.departure || bookingMeta.check_out || '',
    'Room': _friendlyRoom_(bookingMeta, beds24),
    'Channel': beds24.channel || bookingMeta.channel || '',
    'Parse Source': parseDebug.parse_source || '',
    'Raw Body Length': parseDebug.raw_body_length || '',
    'Latest Note': payload.latest_note || '',
    'Review Notes': Array.isArray(payload.review_notes) ? payload.review_notes.join(' | ') : '',
  };

  for (let guestIndex = 1; guestIndex <= 4; guestIndex += 1) {
    const guest = guests.find(function (item) {
      return Number(item.guest_index) === guestIndex;
    }) || {};
    const source = guest.source_payload || {};
    const documents = Array.isArray(guest.documents) ? guest.documents : [];
    const passportDoc = documents.find(function (doc) {
      return doc.doc_type === 'passport';
    }) || {};
    const visaDoc = documents.find(function (doc) {
      return doc.doc_type === 'visa';
    }) || {};

    rowMap[`Guest ${guestIndex} Name`] = guest.full_name || source.full_name || '';
    rowMap[`Guest ${guestIndex} Review Status`] = guest.review_status || '';
    rowMap[`Guest ${guestIndex} Phone`] = source.phone_number || '';
    rowMap[`Guest ${guestIndex} Passport`] = guest.passport_number || source.passport_number || '';
    rowMap[`Guest ${guestIndex} Visa`] = guest.visa_number || source.visa_number || '';
    rowMap[`Guest ${guestIndex} DOB`] = guest.date_of_birth || source.date_of_birth || '';
    rowMap[`Guest ${guestIndex} Gender`] = guest.gender || source.gender || '';
    rowMap[`Guest ${guestIndex} Nationality`] = guest.nationality || source.nationality || '';
    rowMap[`Guest ${guestIndex} Passport OCR`] = passportDoc.ocr_status || '';
    rowMap[`Guest ${guestIndex} Visa OCR`] = visaDoc.ocr_status || '';
  }

  return headers.map(function (header) {
    return rowMap[header] != null ? rowMap[header] : '';
  });
}

function _friendlyRoom_(bookingMeta, beds24) {
  if (bookingMeta && bookingMeta.room_name) {
    return _normalizeRoomText_(bookingMeta.room_name);
  }
  if (beds24 && beds24.roomName) {
    return _normalizeRoomText_(beds24.roomName);
  }
  if (beds24 && beds24.roomLabel) {
    return _normalizeRoomText_(beds24.roomLabel);
  }
  if (beds24 && beds24.roomId && beds24.unitId) {
    return `Room ${beds24.roomId} / Unit ${beds24.unitId}`;
  }
  if (beds24 && beds24.roomId) {
    return `Room ${beds24.roomId}`;
  }
  return '';
}

function _normalizeRoomText_(value) {
  return String(value || '')
    .replace(/^room:/i, 'Room ')
    .replace(/\/unit:/i, ' / Unit ')
    .trim();
}

function _findDriveFolderLink_(submissionId) {
  const id = String(submissionId || '').trim();
  if (!id || !DRIVE_ROOT_FOLDER_ID) {
    return '';
  }

  const cache = CacheService.getScriptCache();
  const cacheKey = 'drive-folder-' + id;
  const cached = cache.get(cacheKey);
  if (cached !== null) {
    return cached;
  }

  try {
    const root = DriveApp.getFolderById(DRIVE_ROOT_FOLDER_ID);
    const folders = root.getFolders();
    while (folders.hasNext()) {
      const folder = folders.next();
      if (String(folder.getName() || '').trim().indexOf(id) === 0) {
        const url = folder.getUrl();
        cache.put(cacheKey, url, 21600);
        return url;
      }
    }
  } catch (error) {
    console.log('Drive folder lookup failed for submission ' + id + ': ' + error);
  }

  cache.put(cacheKey, '', 600);
  return '';
}

function _jsonResponse(payload, statusCode) {
  const output = ContentService.createTextOutput(JSON.stringify(payload));
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}
