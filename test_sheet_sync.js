const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_SOURCE = fs.readFileSync("code.gs", "utf8");
const LEGACY_HEADERS = [
  "Result", "Ticker", "Implied Move", "Structure", "Side", "Size",
  "Open Date", "Open Price", "Open Comm.", "Close Date", "Close Price",
  "Close Comm.", "$ Return", "% Return on Premium", "Cumulative Return $"
];
const ORIGINAL_FORMULAS = {
  A1: '=ARRAYFORMULA({"Result";IF(B2:B<>"",IF( ISNUMBER(M2:M),IF(M2:M>0,"WIN","LOSS"),"OPEN"),"")})',
  M1: '=ARRAYFORMULA({"$ Return";IF(J2:J="","",F2:F*((ABS(H2:H)-ABS(K2:K))*100)*IF(E2:E="credit",1,-1)-(I2:I+L2:L))})',
  N1: '=ARRAYFORMULA({"% Return on Premium";IF(M2:M="","",M2:M/(H2:H*100*F2:F))})',
  O1: '=ARRAYFORMULA({"Cumulative Return $";IF(M2:M="","",SUMIF(ROW(M2:M),"<="&ROW(M2:M),M2:M))})'
};
// NIO on 2026-09-02: one trade, opened and closed in full.
const CLOSED_TRADE = {
  "Close Date": "2026-09-02", "Close Price": -0.04, "Remaining Quantity": 0,
  "Lifecycle Status": "CLOSED", "Close Sync Status": "synced", "Close Cash Flow": 712,
  "Realized P&L": -1424, "Close Method": "calendar", "Close Reason": "scheduled_exit",
  "P&L Status": "CONFIRMED", "Broker Order ID": "order-1, order-2"
};

function columnLabel(column) {
  let result = "";
  for (let value = column; value > 0; value = Math.floor((value - 1) / 26)) {
    result = String.fromCharCode(65 + ((value - 1) % 26)) + result;
  }
  return result;
}

function parseA1(a1) {
  const match = /^([A-Z]+)([1-9][0-9]*)$/.exec(a1);
  assert.ok(match, `Unsupported A1 reference: ${a1}`);
  let column = 0;
  for (const character of match[1]) column = column * 26 + character.charCodeAt(0) - 64;
  return { row: Number(match[2]), column };
}

class FakeRange {
  constructor(sheet, row, column, rows = 1, columns = 1) {
    this.sheet = sheet;
    this.row = row;
    this.column = column;
    this.rows = rows;
    this.columns = columns;
  }

  getValues() {
    return this.sheet.read(this.sheet.values, this);
  }

  getValue() {
    assert.equal(this.rows * this.columns, 1);
    return this.getValues()[0][0];
  }

  getFormulas() {
    return this.sheet.read(this.sheet.formulas, this);
  }

  getDisplayValues() {
    return this.getValues().map((row, rowOffset) => row.map((value, columnOffset) => {
      const key = `${this.row + rowOffset},${this.column + columnOffset}`;
      return this.sheet.displayOverrides.has(key)
        ? this.sheet.displayOverrides.get(key)
        : String(value === null || value === undefined ? "" : value);
    }));
  }

  getFormula() {
    assert.equal(this.rows * this.columns, 1);
    return this.getFormulas()[0][0];
  }

  setValues(values) {
    assert.equal(values.length, this.rows);
    values.forEach(row => assert.equal(row.length, this.columns));
    if (this.sheet.failSetValues && this.sheet.failSetValues(this, values)) {
      this.sheet.failSetValues = null;
      this.sheet.mutations.push({ type: "failedSetValues", row: this.row, column: this.column });
      throw new Error("injected range write failure");
    }
    this.sheet.write(this.sheet.values, this, values);
    this.sheet.mutations.push({ type: "setValues", row: this.row, column: this.column });
    return this;
  }

  setValue(value) {
    this.sheet.write(this.sheet.values, this, [[value]]);
    this.sheet.mutations.push({ type: "setValue", row: this.row, column: this.column });
    return this;
  }

  setFormula(formula) {
    this.sheet.write(this.sheet.formulas, this, [[formula]]);
    this.sheet.mutations.push({
      type: "setFormula",
      cell: `${columnLabel(this.column)}${this.row}`
    });
    return this;
  }

  copyFormatToRange(_sheet, firstColumn, lastColumn, firstRow, lastRow) {
    this.sheet.mutations.push({
      type: "copyFormat", firstColumn, lastColumn, firstRow, lastRow
    });
    return this;
  }
}

class FakeSheet {
  constructor(maxRows, maxColumns) {
    this.maxRows = maxRows;
    this.maxColumns = maxColumns;
    this.values = Array.from({ length: maxRows }, () => Array(maxColumns).fill(""));
    this.formulas = Array.from({ length: maxRows }, () => Array(maxColumns).fill(""));
    this.mutations = [];
    this.failSetValues = null;
    this.displayOverrides = new Map();
  }

  getRange(rowOrA1, column, rows, columns) {
    if (typeof rowOrA1 === "string") {
      const parsed = parseA1(rowOrA1);
      return new FakeRange(this, parsed.row, parsed.column);
    }
    return new FakeRange(this, rowOrA1, column, rows, columns);
  }

  read(grid, range) {
    return Array.from({ length: range.rows }, (_, rowOffset) =>
      Array.from({ length: range.columns }, (_, columnOffset) =>
        grid[range.row - 1 + rowOffset]?.[range.column - 1 + columnOffset] ?? ""
      )
    );
  }

  write(grid, range, values) {
    for (let rowOffset = 0; rowOffset < range.rows; rowOffset++) {
      for (let columnOffset = 0; columnOffset < range.columns; columnOffset++) {
        grid[range.row - 1 + rowOffset][range.column - 1 + columnOffset] =
          values[rowOffset][columnOffset];
      }
    }
  }

  getLastRow() {
    for (let row = this.maxRows; row >= 1; row--) {
      if (this.values[row - 1].some(value => value !== "" && value !== null) ||
          this.formulas[row - 1].some(Boolean)) return row;
    }
    return 0;
  }

  getLastColumn() {
    for (let column = this.maxColumns; column >= 1; column--) {
      if (this.values.some(row => row[column - 1] !== "" && row[column - 1] !== null) ||
          this.formulas.some(row => Boolean(row[column - 1]))) return column;
    }
    return 0;
  }

  getMaxRows() { return this.maxRows; }
  getMaxColumns() { return this.maxColumns; }

  insertColumnsAfter(afterColumn, howMany) {
    assert.equal(afterColumn, this.maxColumns);
    for (const row of this.values) row.splice(afterColumn, 0, ...Array(howMany).fill(""));
    for (const row of this.formulas) row.splice(afterColumn, 0, ...Array(howMany).fill(""));
    this.maxColumns += howMany;
    this.mutations.push({ type: "insertColumns", afterColumn, howMany });
  }

  insertRowsAfter(afterRow, howMany) {
    assert.equal(afterRow, this.maxRows);
    this.values.splice(afterRow, 0,
      ...Array.from({ length: howMany }, () => Array(this.maxColumns).fill("")));
    this.formulas.splice(afterRow, 0,
      ...Array.from({ length: howMany }, () => Array(this.maxColumns).fill("")));
    this.maxRows += howMany;
    this.mutations.push({ type: "insertRows", afterRow, howMany });
  }

  deleteRow(row) {
    assert.ok(row >= 2 && row <= this.maxRows, `Cannot delete row ${row}`);
    this.values.splice(row - 1, 1);
    this.formulas.splice(row - 1, 1);
    this.maxRows -= 1;
    this.mutations.push({ type: "deleteRow", row });
  }

  setFixtureValue(row, column, value) { this.values[row - 1][column - 1] = value; }
  setFixtureFormula(a1, formula) {
    const { row, column } = parseA1(a1);
    this.formulas[row - 1][column - 1] = formula;
  }
  valueAt(row, column) { return this.values[row - 1][column - 1]; }
  formulaAt(a1) {
    const { row, column } = parseA1(a1);
    return this.formulas[row - 1][column - 1];
  }
  resetMutations() { this.mutations = []; }
}

function legacyFixture(maxColumns = 29, maxRows = 58, lastTradeRow = 58) {
  const sheet = new FakeSheet(maxRows, maxColumns);
  LEGACY_HEADERS.forEach((header, index) => sheet.setFixtureValue(1, index + 1, header));
  for (let row = 2; row <= lastTradeRow; row++) {
    const values = [
      "WIN", `OLD${row}`, "4%", "Calendar Spread", "debit", row,
      "2025-01-01", 1.25, 0, "2025-01-02", 0.75, 0, 50, 0.4, row * 50
    ];
    values.forEach((value, index) => sheet.setFixtureValue(row, index + 1, value));
  }
  sheet.setFixtureValue(2, 16, "legacy-stat-label");
  sheet.setFixtureValue(2, 17, "legacy-stat-value");
  sheet.setFixtureValue(lastTradeRow, maxColumns, "far-edge-sentinel");
  Object.entries(ORIGINAL_FORMULAS).forEach(([cell, formula]) =>
    sheet.setFixtureFormula(cell, formula));
  sheet.resetMutations();
  return sheet;
}

// The Sheet as the per-fill version left it: tracking columns appended, its
// summary formulas installed, and one row per fill below the legacy trades.
function addFillEraRows(sheet, context, rows) {
  const appended = context.TRACKING_HEADERS.filter(header =>
    !["Ticker", "Open Date"].includes(header));
  const firstNewColumn = sheet.getMaxColumns() + 1;
  sheet.insertColumnsAfter(sheet.getMaxColumns(), appended.length);
  appended.forEach((header, index) => sheet.setFixtureValue(1, firstNewColumn + index, header));
  const map = headerMap(sheet);
  Object.entries(expectedFillFormulas(formulaColumns(map))).forEach(([cell, formula]) =>
    sheet.setFixtureFormula(cell, formula));
  Object.entries(rows).forEach(([row, values]) => {
    Object.entries(values).forEach(([header, value]) =>
      sheet.setFixtureValue(Number(row), map[header], value));
  });
  sheet.resetMutations();
  return map;
}

function fillRow(tradeId, ticker, recordId, phase, values = {}) {
  return Object.assign({
    "Ticker": ticker, "Size": 89, "Open Date": "2026-08-31",
    "Record ID": recordId, "Trade ID": tradeId, "Parent Trade ID": tradeId,
    "Sync Type": "fill", "Fill Phase": phase, "Filled Quantity": 89,
    "Remaining Quantity": 178, "Lifecycle Status": "OPEN"
  }, values);
}

function loadScript(sheet, { secret = "shared-secret", lockAvailable = true } = {}) {
  let flushCount = 0;
  const lock = {
    released: false,
    tryLock() { return lockAvailable; },
    releaseLock() { this.released = true; }
  };
  const context = {
    console,
    ContentService: {
      MimeType: { JSON: "application/json" },
      createTextOutput(text) {
        return {
          text,
          mimeType: null,
          setMimeType(mimeType) { this.mimeType = mimeType; return this; }
        };
      }
    },
    LockService: { getScriptLock: () => lock },
    PropertiesService: {
      getScriptProperties: () => ({ getProperty: () => secret })
    },
    SpreadsheetApp: {
      getActiveSpreadsheet: () => ({
        getSheetByName: name => name === "Earnings" ? sheet : null
      }),
      flush: () => { flushCount++; }
    }
  };
  vm.createContext(context);
  vm.runInContext(SCRIPT_SOURCE, context, { filename: "code.gs" });
  return { context, lock, getFlushCount: () => flushCount };
}

function responseBody(output) { return JSON.parse(output.text); }
function post(context, payload) {
  return responseBody(context.doPost({ postData: { contents: JSON.stringify(payload) } }));
}

function tradePayload(context, overrides = {}) {
  const values = {
    "Ticker": "NIO", "Implied Move": "8%", "Structure": "Calendar Spread",
    "Side": "debit", "When": "BMO", "Size": 178, "Open Date": "2026-08-31",
    "Open Price": 0.12, "Open Comm.": 0, "Close Date": "", "Close Price": "",
    "Close Comm.": 0, "Short Symbol": "NIO-SHORT", "Long Symbol": "NIO-LONG",
    "Record ID": "", "Trade ID": "trade-1", "Parent Trade ID": "trade-1",
    "Broker Order ID": "order-1", "Broker Fill ID": "", "Sync Type": "trade",
    "Fill Phase": "", "Ordered Quantity": 178, "Filled Quantity": 178,
    "Remaining Quantity": 178, "Lifecycle Status": "OPEN", "Open Sync Status": "synced",
    "Close Sync Status": "not_applicable", "Open Cash Flow": -2136,
    "Close Cash Flow": 0, "Fees": 0, "Realized P&L": "",
    "Close Method": "", "Close Reason": "", "Broker Mode": "PAPER",
    "Broker Account Fingerprint": "fingerprint", "P&L Status": "NOT_REALIZED"
  };
  for (const header of context.REQUIRED_TRADE_HEADERS) assert.ok(header in values);
  return Object.assign({ action: "upsert", auth_token: "shared-secret" }, values, overrides);
}

function headerMap(sheet) {
  const result = {};
  sheet.values[0].forEach((value, index) => { if (value) result[value] = index + 1; });
  return result;
}

function rowsWith(sheet, column, value) {
  return sheet.values.filter(row => row[column - 1] === value).length;
}

function assertRowMatches(sheet, row, payload) {
  const map = headerMap(sheet);
  for (const header of Object.keys(payload)) {
    if (map[header] && !["action", "auth_token"].includes(header)) {
      assert.equal(sheet.valueAt(row, map[header]), payload[header], header);
    }
  }
}

function expectedFillFormulas(columns) {
  const c = columns;
  return {
    A1: `=ARRAYFORMULA({"Result";IF(B2:B="","",IF(${c.record}2:${c.record}="",IF(ISNUMBER(M2:M),IF(M2:M>0,"WIN","LOSS"),"OPEN"),IF(ISNUMBER(M2:M),IF(M2:M>0,"WIN","LOSS"),UPPER(${c.phase}2:${c.phase})&" FILL")))})`,
    M1: `={"$ Return";MAP(B2:B,${c.record}2:${c.record},${c.trade}2:${c.trade},${c.phase}2:${c.phase},${c.remaining}2:${c.remaining},${c.lifecycle}2:${c.lifecycle},SEQUENCE(ROWS(B2:B),1,2),F2:F,E2:E,H2:H,I2:I,J2:J,K2:K,L2:L,LAMBDA(ticker,record,trade,phase,remaining,lifecycle,rownum,qty,side,entry,entryfee,exitdate,exitprice,exitfee,IF(ticker="","",IF(record="",IF(exitdate="","",qty*((ABS(entry)-ABS(exitprice))*100)*IF(side="credit",1,-1)-(entryfee+exitfee)),IF(AND(phase="close",lifecycle="CLOSED",remaining=0,trade<>""),LET(openqty,SUMIFS(${c.filled}$2:${c.filled},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"open"),closeqty,SUMIFS(${c.filled}$2:${c.filled},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"close"),lastrow,MAX(FILTER(SEQUENCE(ROWS(${c.trade}$2:${c.trade}),1,2),${c.trade}$2:${c.trade}=trade,${c.phase}$2:${c.phase}="close",${c.lifecycle}$2:${c.lifecycle}="CLOSED",${c.remaining}$2:${c.remaining}=0)),closepnl,FILTER(${c.realized}$2:${c.realized},${c.trade}$2:${c.trade}=trade,${c.phase}$2:${c.phase}="close"),IF(AND(rownum=lastrow,openqty>0,openqty=closeqty,COUNT(closepnl)=ROWS(closepnl)),SUM(closepnl),"")),"")))))}`,
    N1: `={"% Return on Premium";MAP(M2:M,${c.record}2:${c.record},${c.trade}2:${c.trade},H2:H,F2:F,LAMBDA(pnl,record,trade,entry,qty,IF(pnl="","",IF(record="",pnl/(entry*100*qty),LET(premium,ABS(SUMIFS(${c.openCash}$2:${c.openCash},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"open")),IF(premium=0,"",pnl/premium))))))}`
  };
}

function expectedTradeFormulas(columns) {
  const c = columns;
  return {
    A1: expectedFillFormulas(columns).A1,
    M1: `={"$ Return";MAP(B2:B,${c.record}2:${c.record},${c.syncType}2:${c.syncType},${c.remaining}2:${c.remaining},${c.lifecycle}2:${c.lifecycle},${c.realized}2:${c.realized},F2:F,E2:E,H2:H,I2:I,J2:J,K2:K,L2:L,LAMBDA(ticker,record,synctype,remaining,lifecycle,pnl,qty,side,entry,entryfee,exitdate,exitprice,exitfee,IF(ticker="","",IF(synctype="trade",IF(AND(lifecycle="CLOSED",remaining=0,ISNUMBER(pnl)),pnl,""),IF(record="",IF(exitdate="","",qty*((ABS(entry)-ABS(exitprice))*100)*IF(side="credit",1,-1)-(entryfee+exitfee)),"")))))}`,
    N1: `={"% Return on Premium";MAP(M2:M,${c.syncType}2:${c.syncType},${c.openCash}2:${c.openCash},H2:H,F2:F,LAMBDA(pnl,synctype,opencash,entry,qty,IF(pnl="","",IF(synctype="trade",IF(opencash=0,"",pnl/ABS(opencash)),pnl/(entry*100*qty)))))}`
  };
}

function formulaColumns(map) {
  return {
    record: columnLabel(map["Record ID"]), trade: columnLabel(map["Trade ID"]),
    syncType: columnLabel(map["Sync Type"]),
    phase: columnLabel(map["Fill Phase"]), filled: columnLabel(map["Filled Quantity"]),
    remaining: columnLabel(map["Remaining Quantity"]),
    lifecycle: columnLabel(map["Lifecycle Status"]),
    openCash: columnLabel(map["Open Cash Flow"]),
    realized: columnLabel(map["Realized P&L"])
  };
}

test("canonical legacy Sheet upgrades in place and inserts one row for the trade", () => {
  const sheet = legacyFixture();
  const before = sheet.values.slice(0, 58).map(row => row.slice(0, 29));
  const { context } = loadScript(sheet);
  const payload = tradePayload(context);
  const response = post(context, payload);

  assert.equal(response.status, 200);
  assert.equal(response.operation, "inserted");
  assert.equal(response.row, 59);
  assert.equal(response.key, "Trade ID");
  assert.equal(response.layout, "trade-rows");
  assert.equal(response.merged_rows, 0);
  assert.equal(sheet.getMaxColumns(), 53);
  assert.equal(sheet.getMaxRows(), 59);
  assert.deepEqual(sheet.values.slice(0, 58).map(row => row.slice(0, 29)), before);
  assert.equal(sheet.formulaAt("O1"), ORIGINAL_FORMULAS.O1);

  const missing = context.TRACKING_HEADERS.filter(header =>
    !["Ticker", "Open Date"].includes(header));
  assert.equal(missing.length, 24);
  assert.deepEqual(sheet.values[0].slice(29, 53), Array.from(missing));

  const map = headerMap(sheet);
  const expected = expectedTradeFormulas(formulaColumns(map));
  assert.equal(sheet.formulaAt("A1"), expected.A1);
  assert.equal(sheet.formulaAt("M1"), expected.M1);
  assert.equal(sheet.formulaAt("N1"), expected.N1);
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "setFormula").map(item => item.cell),
    ["M1", "N1", "A1"]
  );
  const sheetHeaders = Object.keys(payload).filter(header => map[header]);
  assert.deepEqual(new Set(response.written_headers), new Set(sheetHeaders));
  for (const header of context.REQUIRED_TRADE_HEADERS) {
    assert.ok(response.written_headers.includes(header), header);
  }
  assertRowMatches(sheet, 59, payload);
});

test("summary formulas use dynamically appended columns", () => {
  const sheet = legacyFixture(31);
  const { context } = loadScript(sheet);
  assert.equal(post(context, tradePayload(context)).status, 200);
  const map = headerMap(sheet);
  const columns = formulaColumns(map);
  const expected = expectedTradeFormulas(columns);
  assert.equal(columns.record, "AH");
  assert.equal(columns.syncType, "AM");
  assert.equal(columns.realized, "AX");
  assert.equal(sheet.formulaAt("A1"), expected.A1);
  assert.equal(sheet.formulaAt("M1"), expected.M1);
  assert.equal(sheet.formulaAt("N1"), expected.N1);
  assert.ok(!sheet.formulaAt("M1").includes("AK2:AK"));
  assert.ok(!sheet.formulaAt("M1").includes("@"));
});

test("closing a trade updates its one row in place", () => {
  const sheet = legacyFixture();
  const { context } = loadScript(sheet);
  assert.equal(post(context, tradePayload(context)).operation, "inserted");
  const closed = tradePayload(context, CLOSED_TRADE);
  const replay = post(context, closed);
  assert.equal(replay.operation, "updated");
  assert.equal(replay.row, 59);
  assert.equal(replay.merged_rows, 0);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(rowsWith(sheet, headerMap(sheet)["Trade ID"], "trade-1"), 1);
  assertRowMatches(sheet, 59, closed);
});

test("a trade's per-fill rows fold into its first row", () => {
  const sheet = legacyFixture(29, 62, 58);
  const before = sheet.values.slice(0, 58).map(row => row.slice(0, 29));
  const { context } = loadScript(sheet);
  const map = addFillEraRows(sheet, context, {
    59: fillRow("trade-1", "NIO", "fill-open-1", "open", { "Open Price": 0.12 }),
    60: fillRow("trade-1", "NIO", "fill-open-2", "open", { "Open Price": 0.12 }),
    61: fillRow("trade-1", "NIO", "fill-close", "close", {
      "Filled Quantity": 178, "Remaining Quantity": 0, "Lifecycle Status": "CLOSED",
      "Close Date": "2026-09-02", "Close Price": -0.04, "Realized P&L": -1424
    }),
    62: fillRow("trade-2", "STZ", "fill-stz-open", "open")
  });

  const closed = tradePayload(context, CLOSED_TRADE);
  const response = post(context, closed);
  assert.equal(response.status, 200);
  assert.equal(response.operation, "updated");
  assert.equal(response.row, 59);
  assert.equal(response.layout, "trade-rows");
  assert.equal(response.merged_rows, 2);
  // Later rows go first so the remaining row numbers stay valid.
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "deleteRow").map(item => item.row),
    [61, 60]
  );
  assert.equal(sheet.getMaxRows(), 60);
  assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 1);
  assertRowMatches(sheet, 59, closed);
  assert.equal(sheet.valueAt(60, map["Record ID"]), "fill-stz-open");
  assert.deepEqual(sheet.values.slice(0, 58).map(row => row.slice(0, 29)), before);

  // The per-fill Result formula already suits trade rows; only the returns change.
  const expected = expectedTradeFormulas(formulaColumns(map));
  assert.equal(sheet.formulaAt("A1"), expected.A1);
  assert.equal(sheet.formulaAt("M1"), expected.M1);
  assert.equal(sheet.formulaAt("N1"), expected.N1);
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "setFormula").map(item => item.cell),
    ["M1", "N1"]
  );

  // A trade with a single fill row is converted where it stands.
  const stz = tradePayload(context, {
    "Ticker": "STZ", "Trade ID": "trade-2", "Parent Trade ID": "trade-2"
  });
  const second = post(context, stz);
  assert.equal(second.operation, "updated");
  assert.equal(second.row, 60);
  assert.equal(second.merged_rows, 0);
  assert.equal(sheet.getMaxRows(), 60);
  assertRowMatches(sheet, 60, stz);
  assert.equal(rowsWith(sheet, map["Sync Type"], "fill"), 0);
});

test("duplicate Trade IDs that are not per-fill rows are left for review", () => {
  const cases = [
    // A hand-made copy of a fill row has no Record ID.
    { 60: { "Record ID": "", "Sync Type": "" } },
    // Two trade rows for one trade.
    { 59: { "Record ID": "", "Sync Type": "trade" }, 60: { "Record ID": "", "Sync Type": "trade" } }
  ];
  for (const overrides of cases) {
    const sheet = legacyFixture(29, 60, 58);
    const { context } = loadScript(sheet);
    const map = addFillEraRows(sheet, context, {
      59: fillRow("trade-1", "NIO", "fill-open", "open", overrides[59]),
      60: fillRow("trade-1", "NIO", "fill-close", "close", overrides[60])
    });
    const rowsBefore = sheet.values.slice(58).map(row => row.slice());
    const response = post(context, tradePayload(context, CLOSED_TRADE));

    assert.equal(response.status, 409);
    assert.match(response.error, /Duplicate stable IDs/);
    assert.equal(sheet.getMaxRows(), 60);
    assert.deepEqual(sheet.values.slice(58), rowsBefore);
    assert.deepEqual(
      sheet.mutations.filter(item => item.type !== "setFormula"),
      []
    );
    assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 2);
  }
});

test("per-fill payloads are refused without touching the Sheet", () => {
  const sheet = legacyFixture();
  const { context } = loadScript(sheet);
  const response = post(context, tradePayload(context, {
    "Record ID": "fill-1", "Sync Type": "fill", "Fill Phase": "open"
  }));
  assert.equal(response.status, 409);
  assert.match(response.error, /One row per fill is retired/);
  assert.deepEqual(sheet.mutations, []);
});

test("malformed, unauthenticated, and incomplete requests never mutate the Sheet", () => {
  const cases = [
    env => responseBody(env.context.doPost()),
    env => responseBody(env.context.doPost({ postData: { contents: "{" } })),
    env => responseBody(env.context.doPost({ postData: { contents: "[]" } })),
    env => post(env.context, { action: "upsert" }),
    env => post(env.context, { action: "wrong", auth_token: "shared-secret" }),
    env => post(env.context, {
      action: "upsert", auth_token: "shared-secret", "Trade ID": "trade-1", "Sync Type": "trade"
    }),
    env => post(env.context, tradePayload(env.context, { "Trade ID": "" }))
  ];
  for (const invoke of cases) {
    const sheet = legacyFixture();
    const env = loadScript(sheet);
    const response = invoke(env);
    assert.ok(response.status >= 400);
    assert.deepEqual(sheet.mutations, []);
  }
  const sheet = legacyFixture();
  const env = loadScript(sheet, { secret: null });
  assert.equal(post(env.context, { action: "upsert", auth_token: "anything" }).status, 503);
  assert.deepEqual(sheet.mutations, []);
});

test("duplicate headers, custom summaries, and formula-managed required columns fail before mutation", () => {
  const cases = [
    sheet => sheet.setFixtureValue(1, 16, "Ticker"),
    sheet => sheet.setFixtureValue(1, 16, "Close Price"),
    sheet => sheet.setFixtureFormula("A1", "=CUSTOM_RESULT()"),
    sheet => sheet.setFixtureFormula("M1", "=CUSTOM_RETURN()"),
    sheet => sheet.setFixtureFormula("N1", "=CUSTOM_PERCENT()"),
    sheet => sheet.setFixtureFormula("O1", "=CUSTOM_CUMULATIVE()"),
    sheet => sheet.setFixtureFormula("B2", "=CUSTOM_TICKER()"),
    sheet => sheet.setFixtureFormula("F2", "=CUSTOM_SIZE()")
  ];
  for (const arrange of cases) {
    const sheet = legacyFixture();
    arrange(sheet);
    sheet.resetMutations();
    const { context } = loadScript(sheet);
    const response = post(context, tradePayload(context));
    assert.equal(response.status, 500);
    assert.deepEqual(sheet.mutations, []);
    assert.equal(sheet.getMaxColumns(), 29);
  }
});

test("a retry repairs the keyed row after a post-migration range failure", () => {
  const sheet = legacyFixture();
  sheet.failSetValues = range => range.row > 1;
  const { context } = loadScript(sheet);
  const payload = tradePayload(context);
  const first = post(context, payload);
  assert.equal(first.status, 500);
  assert.equal(sheet.getMaxColumns(), 53);
  assert.equal(sheet.getMaxRows(), 59);
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "setFormula").map(item => item.cell),
    ["M1", "N1", "A1"]
  );
  let map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Trade ID"]), "trade-1");

  const second = post(context, payload);
  assert.equal(second.status, 200);
  assert.equal(second.operation, "updated");
  assert.equal(second.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  map = headerMap(sheet);
  assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 1);
  assertRowMatches(sheet, 59, payload);
});

test("a summary formula error is not acknowledged and its keyed row is retryable", () => {
  const sheet = legacyFixture();
  sheet.displayOverrides.set("59,13", "#REF!");
  const { context } = loadScript(sheet);
  const payload = tradePayload(context);
  const first = post(context, payload);
  assert.equal(first.ok, false);
  assert.equal(first.status, 500);
  assert.match(first.error, /summary formula needs repair/);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Trade ID"]), "trade-1");
  assert.equal(sheet.getMaxRows(), 59);

  sheet.displayOverrides.clear();
  const second = post(context, payload);
  assert.equal(second.status, 200);
  assert.equal(second.operation, "updated");
  assert.equal(second.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 1);
});

test("a persistent summary header error cannot bypass validation on replay", () => {
  const sheet = legacyFixture();
  sheet.displayOverrides.set("1,13", "#ERROR!");
  const { context } = loadScript(sheet);
  const payload = tradePayload(context);

  const first = post(context, payload);
  assert.equal(first.ok, false);
  assert.equal(first.status, 500);
  assert.match(first.error, /summary formula needs repair/);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Trade ID"]), "trade-1");

  // A real formula error is returned by both getValues() and getDisplayValues().
  // This recreates the state seen by the second request after recalculation.
  sheet.setFixtureValue(1, 13, "#ERROR!");
  const second = post(context, payload);
  assert.equal(second.ok, false);
  assert.equal(second.status, 500);
  assert.match(second.error, /summary formula needs repair/);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 1);

  sheet.setFixtureValue(1, 13, "$ Return");
  sheet.displayOverrides.clear();
  const third = post(context, payload);
  assert.equal(third.status, 200);
  assert.equal(third.operation, "updated");
  assert.equal(third.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(rowsWith(sheet, map["Trade ID"], "trade-1"), 1);
});

test("a preallocated 7012-row Sheet places the first new trade directly after 58 trades", () => {
  const sheet = legacyFixture(29, 7012, 59);
  const { context } = loadScript(sheet);
  const response = post(context, tradePayload(context));

  assert.equal(response.status, 200);
  assert.equal(response.operation, "inserted");
  assert.equal(response.row, 60);
  assert.equal(sheet.getMaxRows(), 7012);
  assert.equal(sheet.mutations.some(item => item.type === "insertRows"), false);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(60, map["Trade ID"]), "trade-1");
  assert.equal(sheet.valueAt(59, map["Ticker"]), "OLD59");
});
