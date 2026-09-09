/**
 * @OnlyCurrentDoc
 */

var SHEET_NAME = "Earnings";
var AUTH_PROPERTY_NAME = "GOOGLE_SCRIPT_SECRET";
var LOCK_TIMEOUT_MS = 30000;
var RESERVED_PAYLOAD_FIELDS = {
  action: true,
  auth_token: true
};
var REQUIRED_FILL_HEADERS = [
  "Ticker", "Short Symbol", "Long Symbol", "Open Date",
  "Record ID", "Trade ID", "Parent Trade ID", "Broker Order ID", "Broker Fill ID",
  "Sync Type", "Fill Phase", "Ordered Quantity", "Filled Quantity", "Remaining Quantity",
  "Lifecycle Status", "Open Sync Status", "Close Sync Status", "Open Cash Flow",
  "Close Cash Flow", "Fees", "Realized P&L", "Close Method", "Close Reason",
  "Broker Mode", "Broker Account Fingerprint", "P&L Status"
];

function doGet() {
  return jsonResponse_(false, 405, { error: "GET data export is disabled" });
}

function doPost(e) {
  var payload;
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse_(false, 400, { error: "JSON request body is required" });
    }
    payload = JSON.parse(e.postData.contents);
  } catch (error) {
    return jsonResponse_(false, 400, { error: "Request body is not valid JSON" });
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return jsonResponse_(false, 400, { error: "Request body must be a JSON object" });
  }

  var authError = authorizeRequest_(payload.auth_token);
  if (authError) {
    return jsonResponse_(false, authError.status, { error: authError.error });
  }

  if (payload.action !== "upsert") {
    return jsonResponse_(false, 400, { error: "Unsupported action; expected 'upsert'" });
  }

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_TIMEOUT_MS)) {
    return jsonResponse_(false, 423, { error: "Sheet mutation lock could not be acquired" });
  }

  try {
    return upsertRecord_(getEarningsSheet_(), payload);
  } catch (error) {
    return jsonResponse_(false, 500, { error: safeErrorMessage_(error) });
  } finally {
    lock.releaseLock();
  }
}

function doOptions() {
  return jsonResponse_(false, 405, { error: "Only authenticated POST requests are supported" });
}

function upsertRecord_(sheet, payload) {
  var recordId = normalizedId_(payload["Record ID"]);
  var tradeId = normalizedId_(payload["Trade ID"]);
  var syncType = String(payload["Sync Type"] || "").toLowerCase();
  var validateSummary = false;

  if (!recordId && !tradeId) {
    return jsonResponse_(false, 400, { error: "Record ID or Trade ID is required" });
  }
  if (syncType === "fill" && !recordId) {
    return jsonResponse_(false, 400, { error: "Fill upserts require a unique Record ID" });
  }
  if (syncType === "fill") {
    var missingFields = REQUIRED_FILL_HEADERS.filter(function(header) {
      return !hasOwn_(payload, header);
    });
    if (missingFields.length) {
      return jsonResponse_(false, 400, {
        error: "Fill payload is incomplete", missing_payload_fields: missingFields
      });
    }
    // Only an authenticated fill request upgrades the connected Sheet. No
    // setup run, manual fill insertion, or second spreadsheet is needed.
    validateSummary = ensureFillSchema_(sheet);
  }

  var lastColumn = sheet.getLastColumn();
  if (lastColumn < 1) {
    return jsonResponse_(false, 500, { error: "Sheet has no header row" });
  }

  var lastRow = Math.max(sheet.getLastRow(), 1);
  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0].map(function(value) {
    return String(value).trim();
  });
  var headerMap = buildHeaderMap_(headers);
  var keyHeader = recordId ? "Record ID" : "Trade ID";
  var keyValue = recordId || tradeId;

  if (headerMap[keyHeader] === undefined) {
    return jsonResponse_(false, 409, { error: "Required sheet header is missing: " + keyHeader });
  }

  var matchingRows = findMatchingRows_(sheet, lastRow, headerMap[keyHeader], keyValue);
  if (matchingRows.length > 1) {
    return jsonResponse_(false, 409, {
      error: "Duplicate stable IDs already exist in the Sheet",
      key: keyHeader,
      record_id: keyValue
    });
  }

  var protectedColumns = findFormulaColumns_(sheet, lastRow, lastColumn);
  if (protectedColumns[headerMap[keyHeader]]) {
    return jsonResponse_(false, 409, { error: "Stable ID column is formula-managed and cannot be written: " + keyHeader });
  }

  if (syncType === "fill") {
    var missingHeaders = REQUIRED_FILL_HEADERS.filter(function(header) {
      return headerMap[header] === undefined;
    });
    var missingPayloadFields = REQUIRED_FILL_HEADERS.filter(function(header) {
      return !hasOwn_(payload, header);
    });
    var protectedRequiredHeaders = REQUIRED_FILL_HEADERS.filter(function(header) {
      return headerMap[header] !== undefined && protectedColumns[headerMap[header]];
    });
    if (missingHeaders.length || missingPayloadFields.length || protectedRequiredHeaders.length) {
      return jsonResponse_(false, 409, {
        error: "Sheet fill schema is incomplete or not writable",
        missing_headers: missingHeaders,
        missing_payload_fields: missingPayloadFields,
        protected_required_headers: protectedRequiredHeaders
      });
    }
  }

  var writableHeaders = headers.filter(function(header, columnIndex) {
    return header &&
      !RESERVED_PAYLOAD_FIELDS[header] &&
      hasOwn_(payload, header) &&
      !protectedColumns[columnIndex];
  });
  if (writableHeaders.length === 0) {
    return jsonResponse_(false, 400, { error: "Payload contains no writable Sheet headers" });
  }

  var operation = matchingRows.length === 1 ? "updated" : "inserted";
  var targetRow = matchingRows.length === 1
    ? matchingRows[0]
    : findEmptyDataRow_(sheet, lastRow, headerMap);
  var writeResult = writePayload_(
    sheet,
    targetRow,
    headers,
    protectedColumns,
    payload,
    operation === "inserted" ? keyHeader : ""
  );

  if (writeResult.writtenHeaders.indexOf(keyHeader) === -1 && operation === "inserted") {
    return jsonResponse_(false, 409, { error: "Stable ID was not written to the new row" });
  }
  if (syncType === "fill") {
    var unwrittenRequiredHeaders = REQUIRED_FILL_HEADERS.filter(function(header) {
      return writeResult.writtenHeaders.indexOf(header) === -1;
    });
    if (unwrittenRequiredHeaders.length) {
      return jsonResponse_(false, 409, {
        error: "Required fill fields were not written",
        unwritten_required_headers: unwrittenRequiredHeaders
      });
    }
  }

  SpreadsheetApp.flush();
  if (validateSummary) {
    validateFillSummary_(sheet, targetRow);
  }
  return jsonResponse_(true, 200, {
    operation: operation,
    row: targetRow,
    key: keyHeader,
    record_id: keyValue,
    written_headers: writeResult.writtenHeaders,
    ignored_formula_headers: writeResult.protectedHeaders
  });
}

function ensureFillSchema_(sheet) {
  var lastColumn = sheet.getLastColumn();
  if (lastColumn < 1) {
    throw new Error("Sheet has no header row");
  }
  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0].map(function(value) {
    return String(value).trim();
  });
  var headerMap = buildHeaderMap_(headers);
  REQUIRED_FILL_HEADERS.forEach(function(header) {
    if (headers.indexOf(header) !== headers.lastIndexOf(header)) {
      throw new Error("Duplicate required Sheet header: " + header);
    }
  });
  var missing = REQUIRED_FILL_HEADERS.filter(function(header) {
    return headerMap[header] === undefined;
  });
  var firstNewColumn = sheet.getMaxColumns() + 1;
  missing.forEach(function(header, index) {
    headerMap[header] = firstNewColumn + index - 1;
  });

  var legacyHeaders = [
    "Result", "Ticker", "Implied Move", "Structure", "Side", "Size",
    "Open Date", "Open Price", "Open Comm.", "Close Date", "Close Price",
    "Close Comm.", "$ Return", "% Return on Premium", "Cumulative Return $"
  ];
  // Formula anchors can temporarily display an error. Recognize the layout
  // from its input headers so retries cannot bypass summary validation.
  var isLegacy = legacyHeaders.every(function(header, index) {
    return index === 0 || index >= 12 || headers[index] === header;
  });
  if (!isLegacy && missing.length) {
    throw new Error("Unrecognized Sheet layout; required fill headers must be configured before syncing");
  }
  var formulas = isLegacy ? fillSummaryFormulas_(headerMap) : {};
  var originalFormulas = {
    "A1": "=ARRAYFORMULA({\"Result\";IF(B2:B<>\"\",IF( ISNUMBER(M2:M),IF(M2:M>0,\"WIN\",\"LOSS\"),\"OPEN\"),\"\")})",
    "M1": "=ARRAYFORMULA({\"$ Return\";\n  IF(\n    J2:J=\"\",\n    \"\",\n    F2:F * ((ABS(H2:H)-ABS(K2:K)) * 100)\n      * IF(E2:E=\"credit\", 1, -1)\n      - (I2:I + L2:L)\n  )}\n)",
    "N1": "=ARRAYFORMULA({\"% Return on Premium\";\n  IF(\n    M2:M=\"\",\n    \"\",\n    M2:M\n      / ( H2:H * 100 * F2:F )\n  )}\n)"
  };
  // Check every formula before making any change. Only the known template or
  // this migration's formulas are eligible; custom formulas are never replaced.
  Object.keys(formulas).forEach(function(cell) {
    var current = sheet.getRange(cell).getFormula();
    if (compactFormula_(current) !== compactFormula_(originalFormulas[cell]) &&
        compactFormula_(current) !== compactFormula_(formulas[cell])) {
      throw new Error("Custom Sheet formula needs review before fill sync: " + cell);
    }
  });
  var protectedColumns = findFormulaColumns_(sheet, Math.max(sheet.getLastRow(), 1), lastColumn);
  REQUIRED_FILL_HEADERS.forEach(function(header) {
    if (protectedColumns[headerMap[header]]) {
      throw new Error("Required fill column is formula-managed: " + header);
    }
  });

  if (isLegacy && compactFormula_(sheet.getRange("O1").getFormula()) !==
      compactFormula_('=ARRAYFORMULA({"Cumulative Return $";IF(M2:M="","",SUMIF(ROW(M2:M),"<="&ROW(M2:M),M2:M))})')) {
    throw new Error("Custom Sheet formula needs review before fill sync: O1");
  }

  if (missing.length) {
    sheet.insertColumnsAfter(firstNewColumn - 1, missing.length);
    var target = sheet.getRange(1, firstNewColumn, 1, missing.length);
    target.setValues([missing]);
    sheet.getRange(1, headerMap["Ticker"] + 1).copyFormatToRange(
      sheet, firstNewColumn, firstNewColumn + missing.length - 1, 1, 1
    );
  }
  (isLegacy ? ["M1", "N1", "A1"] : []).forEach(function(cell) {
    var range = sheet.getRange(cell);
    if (compactFormula_(range.getFormula()) !== compactFormula_(formulas[cell])) {
      range.setFormula(formulas[cell]);
    }
  });
  SpreadsheetApp.flush();
  return isLegacy;
}

function validateFillSummary_(sheet, rowNumber) {
  var columns = [0, 12, 13, 14];
  var expected = ["Result", "$ Return", "% Return on Premium", "Cumulative Return $"];
  var headers = sheet.getRange(1, 1, 1, 15).getDisplayValues()[0];
  var values = sheet.getRange(rowNumber, 1, 1, 15).getDisplayValues()[0];
  columns.forEach(function(column, index) {
    if (headers[column] !== expected[index] ||
        /^#(REF!|ERROR!|VALUE!|N\/A|DIV\/0!|NUM!|NAME\?)/.test(values[column])) {
      throw new Error("Sheet summary formula needs repair before acknowledging row " + rowNumber);
    }
  });
}

function compactFormula_(formula) {
  return String(formula).replace(/"(?:""|[^"])*"|\s+/g, function(value) {
    return value.charAt(0) === '"' ? value : "";
  });
}

function sheetColumn_(zeroBasedColumn) {
  var result = "";
  for (var value = zeroBasedColumn + 1; value > 0; value = Math.floor((value - 1) / 26)) {
    result = String.fromCharCode(65 + (value - 1) % 26) + result;
  }
  return result;
}

function fillSummaryFormulas_(headerMap) {
  var columns = {
    "AF": "Record ID",
    "AG": "Trade ID",
    "AL": "Fill Phase",
    "AN": "Filled Quantity",
    "AO": "Remaining Quantity",
    "AP": "Lifecycle Status",
    "AS": "Open Cash Flow",
    "AV": "Realized P&L"
  };
  // Legacy rows retain their calculation. Modern fills contribute one trade
  // result only after all opening/closing quantities and close P&L are present.
  // Unknown fees remain explicitly provisional in the P&L Status column.
  var formulas = {
    "A1": "=ARRAYFORMULA({\"Result\";IF(B2:B=\"\",\"\",IF(@AF@2:@AF@=\"\",IF(ISNUMBER(M2:M),IF(M2:M>0,\"WIN\",\"LOSS\"),\"OPEN\"),IF(ISNUMBER(M2:M),IF(M2:M>0,\"WIN\",\"LOSS\"),UPPER(@AL@2:@AL@)&\" FILL\")))})",
    "M1": "={\"$ Return\";MAP(B2:B,@AF@2:@AF@,@AG@2:@AG@,@AL@2:@AL@,@AO@2:@AO@,@AP@2:@AP@,SEQUENCE(ROWS(B2:B),1,2),F2:F,E2:E,H2:H,I2:I,J2:J,K2:K,L2:L,LAMBDA(ticker,record,trade,phase,remaining,lifecycle,rownum,qty,side,entry,entryfee,exitdate,exitprice,exitfee,IF(ticker=\"\",\"\",IF(record=\"\",IF(exitdate=\"\",\"\",qty*((ABS(entry)-ABS(exitprice))*100)*IF(side=\"credit\",1,-1)-(entryfee+exitfee)),IF(AND(phase=\"close\",lifecycle=\"CLOSED\",remaining=0,trade<>\"\"),LET(openqty,SUMIFS(@AN@$2:@AN@,@AG@$2:@AG@,trade,@AL@$2:@AL@,\"open\"),closeqty,SUMIFS(@AN@$2:@AN@,@AG@$2:@AG@,trade,@AL@$2:@AL@,\"close\"),lastrow,MAX(FILTER(SEQUENCE(ROWS(@AG@$2:@AG@),1,2),@AG@$2:@AG@=trade,@AL@$2:@AL@=\"close\",@AP@$2:@AP@=\"CLOSED\",@AO@$2:@AO@=0)),closepnl,FILTER(@AV@$2:@AV@,@AG@$2:@AG@=trade,@AL@$2:@AL@=\"close\"),IF(AND(rownum=lastrow,openqty>0,openqty=closeqty,COUNT(closepnl)=ROWS(closepnl)),SUM(closepnl),\"\")),\"\")))))}",
    "N1": "={\"% Return on Premium\";MAP(M2:M,@AF@2:@AF@,@AG@2:@AG@,H2:H,F2:F,LAMBDA(pnl,record,trade,entry,qty,IF(pnl=\"\",\"\",IF(record=\"\",pnl/(entry*100*qty),LET(premium,ABS(SUMIFS(@AS@$2:@AS@,@AG@$2:@AG@,trade,@AL@$2:@AL@,\"open\")),IF(premium=0,\"\",pnl/premium))))))}"
  };
  Object.keys(formulas).forEach(function(cell) {
    formulas[cell] = formulas[cell].replace(/@([A-Z]+)@/g, function(token, key) {
      return sheetColumn_(headerMap[columns[key]]);
    });
  });
  return formulas;
}

function writePayload_(sheet, rowNumber, headers, protectedColumns, payload, keyHeader) {
  var updates = [];
  var protectedHeaders = [];

  headers.forEach(function(header, columnIndex) {
    if (!header || RESERVED_PAYLOAD_FIELDS[header] || !hasOwn_(payload, header)) {
      return;
    }
    if (protectedColumns[columnIndex]) {
      protectedHeaders.push(header);
      return;
    }
    updates.push({
      column: columnIndex + 1,
      header: header,
      value: payload[header] === null ? "" : payload[header]
    });
  });

  updates.sort(function(left, right) { return left.column - right.column; });
  var keyWrittenHeader = "";
  if (keyHeader) {
    var keyUpdate = updates.filter(function(update) { return update.header === keyHeader; })[0];
    if (!keyUpdate) {
      throw new Error("Stable ID is not writable: " + keyHeader);
    }
    // Establish the idempotency key first. If a later range write fails, a retry
    // finds and repairs this same row instead of inserting an orphan duplicate.
    sheet.getRange(rowNumber, keyUpdate.column).setValue(keyUpdate.value);
    keyWrittenHeader = keyUpdate.header;
    updates = updates.filter(function(update) { return update.header !== keyHeader; });
  }
  var groups = [];
  updates.forEach(function(update) {
    var group = groups.length ? groups[groups.length - 1] : null;
    if (!group || update.column !== group.startColumn + group.values.length) {
      group = { startColumn: update.column, values: [], headers: [] };
      groups.push(group);
    }
    group.values.push(update.value);
    group.headers.push(update.header);
  });

  groups.forEach(function(group) {
    sheet.getRange(rowNumber, group.startColumn, 1, group.values.length).setValues([group.values]);
  });

  return {
    writtenHeaders: (keyWrittenHeader ? [keyWrittenHeader] : []).concat(
      updates.map(function(update) { return update.header; })
    ),
    protectedHeaders: protectedHeaders
  };
}

function findFormulaColumns_(sheet, lastRow, lastColumn) {
  var protectedColumns = {};

  // Treat a column as formula-managed if any populated row contains a formula.
  // This catches spill/formula anchors that were moved below the first two rows
  // without relying only on the current Sheet's fixed protected-column layout.
  var formulas = sheet.getRange(1, 1, Math.max(lastRow, 1), lastColumn).getFormulas();
  formulas.forEach(function(row) {
    row.forEach(function(formula, columnIndex) {
      if (formula) {
        protectedColumns[columnIndex] = true;
      }
    });
  });
  return protectedColumns;
}

function findMatchingRows_(sheet, lastRow, zeroBasedColumn, keyValue) {
  var matches = [];
  if (lastRow < 2) {
    return matches;
  }

  var keyValues = sheet.getRange(2, zeroBasedColumn + 1, lastRow - 1, 1).getValues();
  for (var rowIndex = 0; rowIndex < keyValues.length; rowIndex++) {
    if (normalizedId_(keyValues[rowIndex][0]) === keyValue) {
      matches.push(rowIndex + 2);
    }
  }
  return matches;
}

function findEmptyDataRow_(sheet, lastRow, headerMap) {
  var anchorHeaders = ["Record ID", "Trade ID", "Ticker"];
  var anchorColumns = anchorHeaders.filter(function(header) {
    return headerMap[header] !== undefined;
  }).map(function(header) {
    return headerMap[header];
  });

  if (lastRow < 2) {
    return 2;
  }

  var anchorValues = anchorColumns.map(function(columnIndex) {
    return sheet.getRange(2, columnIndex + 1, lastRow - 1, 1).getValues();
  });

  for (var rowOffset = 0; rowOffset < lastRow - 1; rowOffset++) {
    var isEmpty = anchorValues.every(function(columnValues) {
      return columnValues[rowOffset][0] === "" || columnValues[rowOffset][0] === null;
    });
    if (isEmpty) {
      return rowOffset + 2;
    }
  }

  if (lastRow < sheet.getMaxRows()) {
    return lastRow + 1;
  }
  sheet.insertRowsAfter(sheet.getMaxRows(), 1);
  return sheet.getMaxRows();
}

function buildHeaderMap_(headers) {
  var headerMap = {};
  headers.forEach(function(header, index) {
    if (header && headerMap[header] === undefined) {
      headerMap[header] = index;
    }
  });
  return headerMap;
}

function getEarningsSheet_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error("Sheet not found: " + SHEET_NAME);
  }
  return sheet;
}

function authorizeRequest_(providedToken) {
  var configuredToken = PropertiesService.getScriptProperties().getProperty(AUTH_PROPERTY_NAME);
  if (!configuredToken) {
    return { status: 503, error: "Sheet request authentication is not configured" };
  }
  if (!providedToken || !constantTimeEquals_(String(providedToken), String(configuredToken))) {
    return { status: 401, error: "Unauthorized request" };
  }
  return null;
}

function constantTimeEquals_(left, right) {
  var maximumLength = Math.max(left.length, right.length);
  var mismatch = left.length ^ right.length;
  for (var index = 0; index < maximumLength; index++) {
    var leftCode = index < left.length ? left.charCodeAt(index) : 0;
    var rightCode = index < right.length ? right.charCodeAt(index) : 0;
    mismatch |= leftCode ^ rightCode;
  }
  return mismatch === 0;
}

function normalizedId_(value) {
  if (value === undefined || value === null) {
    return "";
  }
  return String(value).trim();
}

function hasOwn_(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function safeErrorMessage_(error) {
  if (!error) {
    return "Unknown Apps Script error";
  }
  return String(error.message || error);
}

function jsonResponse_(ok, status, details) {
  var body = { ok: ok, status: status };
  Object.keys(details || {}).forEach(function(key) {
    body[key] = details[key];
  });
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
