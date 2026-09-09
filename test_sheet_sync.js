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

function fillPayload(context, overrides = {}) {
  const values = {
    "Ticker": "NIO", "Short Symbol": "NIO-SHORT", "Long Symbol": "NIO-LONG",
    "Open Date": "2026-08-31", "Record ID": "fill-1", "Trade ID": "trade-1",
    "Parent Trade ID": "trade-1", "Broker Order ID": "order-1",
    "Broker Fill ID": "activity-1", "Sync Type": "fill", "Fill Phase": "open",
    "Ordered Quantity": 178, "Filled Quantity": 178, "Remaining Quantity": 178,
    "Lifecycle Status": "OPEN", "Open Sync Status": "synced",
    "Close Sync Status": "pending", "Open Cash Flow": -2136,
    "Close Cash Flow": 0, "Fees": "", "Realized P&L": "",
    "Close Method": "", "Close Reason": "", "Broker Mode": "PAPER",
    "Broker Account Fingerprint": "fingerprint", "P&L Status": "NOT_REALIZED"
  };
  for (const header of context.REQUIRED_FILL_HEADERS) assert.ok(header in values);
  return Object.assign({ action: "upsert", auth_token: "shared-secret" }, values, overrides);
}

function headerMap(sheet) {
  const result = {};
  sheet.values[0].forEach((value, index) => { if (value) result[value] = index + 1; });
  return result;
}

function expectedSummaryFormulas(columns) {
  const c = columns;
  return {
    A1: `=ARRAYFORMULA({"Result";IF(B2:B="","",IF(${c.record}2:${c.record}="",IF(ISNUMBER(M2:M),IF(M2:M>0,"WIN","LOSS"),"OPEN"),IF(ISNUMBER(M2:M),IF(M2:M>0,"WIN","LOSS"),UPPER(${c.phase}2:${c.phase})&" FILL")))})`,
    M1: `={"$ Return";MAP(B2:B,${c.record}2:${c.record},${c.trade}2:${c.trade},${c.phase}2:${c.phase},${c.remaining}2:${c.remaining},${c.lifecycle}2:${c.lifecycle},SEQUENCE(ROWS(B2:B),1,2),F2:F,E2:E,H2:H,I2:I,J2:J,K2:K,L2:L,LAMBDA(ticker,record,trade,phase,remaining,lifecycle,rownum,qty,side,entry,entryfee,exitdate,exitprice,exitfee,IF(ticker="","",IF(record="",IF(exitdate="","",qty*((ABS(entry)-ABS(exitprice))*100)*IF(side="credit",1,-1)-(entryfee+exitfee)),IF(AND(phase="close",lifecycle="CLOSED",remaining=0,trade<>""),LET(openqty,SUMIFS(${c.filled}$2:${c.filled},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"open"),closeqty,SUMIFS(${c.filled}$2:${c.filled},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"close"),lastrow,MAX(FILTER(SEQUENCE(ROWS(${c.trade}$2:${c.trade}),1,2),${c.trade}$2:${c.trade}=trade,${c.phase}$2:${c.phase}="close",${c.lifecycle}$2:${c.lifecycle}="CLOSED",${c.remaining}$2:${c.remaining}=0)),closepnl,FILTER(${c.realized}$2:${c.realized},${c.trade}$2:${c.trade}=trade,${c.phase}$2:${c.phase}="close"),IF(AND(rownum=lastrow,openqty>0,openqty=closeqty,COUNT(closepnl)=ROWS(closepnl)),SUM(closepnl),"")),"")))))}`,
    N1: `={"% Return on Premium";MAP(M2:M,${c.record}2:${c.record},${c.trade}2:${c.trade},H2:H,F2:F,LAMBDA(pnl,record,trade,entry,qty,IF(pnl="","",IF(record="",pnl/(entry*100*qty),LET(premium,ABS(SUMIFS(${c.openCash}$2:${c.openCash},${c.trade}$2:${c.trade},trade,${c.phase}$2:${c.phase},"open")),IF(premium=0,"",pnl/premium))))))}`
  };
}

function formulaColumns(map) {
  return {
    record: columnLabel(map["Record ID"]), trade: columnLabel(map["Trade ID"]),
    phase: columnLabel(map["Fill Phase"]), filled: columnLabel(map["Filled Quantity"]),
    remaining: columnLabel(map["Remaining Quantity"]),
    lifecycle: columnLabel(map["Lifecycle Status"]),
    openCash: columnLabel(map["Open Cash Flow"]),
    realized: columnLabel(map["Realized P&L"])
  };
}

test("canonical legacy Sheet upgrades in place and inserts a complete fill", () => {
  const sheet = legacyFixture();
  const before = sheet.values.slice(0, 58).map(row => row.slice(0, 29));
  const { context } = loadScript(sheet);
  const payload = fillPayload(context);
  const response = post(context, payload);

  assert.equal(response.status, 200);
  assert.equal(response.operation, "inserted");
  assert.equal(response.row, 59);
  assert.equal(sheet.getMaxColumns(), 53);
  assert.equal(sheet.getMaxRows(), 59);
  assert.deepEqual(sheet.values.slice(0, 58).map(row => row.slice(0, 29)), before);
  assert.equal(sheet.formulaAt("O1"), ORIGINAL_FORMULAS.O1);

  const missing = context.REQUIRED_FILL_HEADERS.filter(header =>
    !["Ticker", "Open Date"].includes(header));
  assert.equal(missing.length, 24);
  assert.deepEqual(sheet.values[0].slice(29, 53), Array.from(missing));

  const map = headerMap(sheet);
  const expected = expectedSummaryFormulas(formulaColumns(map));
  assert.equal(sheet.formulaAt("A1"), expected.A1);
  assert.equal(sheet.formulaAt("M1"), expected.M1);
  assert.equal(sheet.formulaAt("N1"), expected.N1);
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "setFormula").map(item => item.cell),
    ["M1", "N1", "A1"]
  );
  assert.deepEqual(new Set(response.written_headers), new Set(context.REQUIRED_FILL_HEADERS));
  for (const header of context.REQUIRED_FILL_HEADERS) {
    assert.equal(sheet.valueAt(59, map[header]), payload[header]);
  }
});

test("summary formulas use dynamically appended columns", () => {
  const sheet = legacyFixture(31);
  const { context } = loadScript(sheet);
  assert.equal(post(context, fillPayload(context)).status, 200);
  const map = headerMap(sheet);
  const columns = formulaColumns(map);
  const expected = expectedSummaryFormulas(columns);
  assert.equal(columns.record, "AH");
  assert.equal(columns.realized, "AX");
  assert.equal(sheet.formulaAt("A1"), expected.A1);
  assert.equal(sheet.formulaAt("M1"), expected.M1);
  assert.equal(sheet.formulaAt("N1"), expected.N1);
  assert.ok(!sheet.formulaAt("M1").includes("AF2:AF"));
  assert.ok(!sheet.formulaAt("M1").includes("@"));
});

test("replaying one Record ID updates the same row", () => {
  const sheet = legacyFixture();
  const { context } = loadScript(sheet);
  const payload = fillPayload(context);
  assert.equal(post(context, payload).operation, "inserted");
  const replay = post(context, Object.assign({}, payload, { "P&L Status": "CONFIRMED" }));
  assert.equal(replay.operation, "updated");
  assert.equal(replay.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  const recordColumn = headerMap(sheet)["Record ID"];
  assert.equal(sheet.values.filter(row => row[recordColumn - 1] === "fill-1").length, 1);
  assert.equal(sheet.valueAt(59, headerMap(sheet)["P&L Status"]), "CONFIRMED");
});

test("malformed, unauthenticated, and incomplete requests never mutate the Sheet", () => {
  const cases = [
    env => responseBody(env.context.doPost()),
    env => responseBody(env.context.doPost({ postData: { contents: "{" } })),
    env => responseBody(env.context.doPost({ postData: { contents: "[]" } })),
    env => post(env.context, { action: "upsert" }),
    env => post(env.context, { action: "wrong", auth_token: "shared-secret" }),
    env => post(env.context, {
      action: "upsert", auth_token: "shared-secret", "Record ID": "fill-1",
      "Trade ID": "trade-1", "Sync Type": "fill"
    })
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
    sheet => sheet.setFixtureFormula("A1", "=CUSTOM_RESULT()"),
    sheet => sheet.setFixtureFormula("M1", "=CUSTOM_RETURN()"),
    sheet => sheet.setFixtureFormula("N1", "=CUSTOM_PERCENT()"),
    sheet => sheet.setFixtureFormula("O1", "=CUSTOM_CUMULATIVE()"),
    sheet => sheet.setFixtureFormula("B2", "=CUSTOM_TICKER()")
  ];
  for (const arrange of cases) {
    const sheet = legacyFixture();
    arrange(sheet);
    sheet.resetMutations();
    const { context } = loadScript(sheet);
    const response = post(context, fillPayload(context));
    assert.equal(response.status, 500);
    assert.deepEqual(sheet.mutations, []);
    assert.equal(sheet.getMaxColumns(), 29);
  }
});

test("a retry repairs the keyed row after a post-migration range failure", () => {
  const sheet = legacyFixture();
  sheet.failSetValues = range => range.row > 1;
  const { context } = loadScript(sheet);
  const payload = fillPayload(context);
  const first = post(context, payload);
  assert.equal(first.status, 500);
  assert.equal(sheet.getMaxColumns(), 53);
  assert.equal(sheet.getMaxRows(), 59);
  assert.deepEqual(
    sheet.mutations.filter(item => item.type === "setFormula").map(item => item.cell),
    ["M1", "N1", "A1"]
  );
  let map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Record ID"]), "fill-1");

  const second = post(context, payload);
  assert.equal(second.status, 200);
  assert.equal(second.operation, "updated");
  assert.equal(second.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  map = headerMap(sheet);
  assert.equal(sheet.values.filter(row => row[map["Record ID"] - 1] === "fill-1").length, 1);
  for (const header of context.REQUIRED_FILL_HEADERS) {
    assert.equal(sheet.valueAt(59, map[header]), payload[header]);
  }
});

test("a summary formula error is not acknowledged and its keyed row is retryable", () => {
  const sheet = legacyFixture();
  sheet.displayOverrides.set("59,13", "#REF!");
  const { context } = loadScript(sheet);
  const payload = fillPayload(context);
  const first = post(context, payload);
  assert.equal(first.ok, false);
  assert.equal(first.status, 500);
  assert.match(first.error, /summary formula needs repair/);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Record ID"]), "fill-1");
  assert.equal(sheet.getMaxRows(), 59);

  sheet.displayOverrides.clear();
  const second = post(context, payload);
  assert.equal(second.status, 200);
  assert.equal(second.operation, "updated");
  assert.equal(second.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(sheet.values.filter(row => row[map["Record ID"] - 1] === "fill-1").length, 1);
});

test("a persistent summary header error cannot bypass validation on replay", () => {
  const sheet = legacyFixture();
  sheet.displayOverrides.set("1,13", "#ERROR!");
  const { context } = loadScript(sheet);
  const payload = fillPayload(context);

  const first = post(context, payload);
  assert.equal(first.ok, false);
  assert.equal(first.status, 500);
  assert.match(first.error, /summary formula needs repair/);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(59, map["Record ID"]), "fill-1");

  // A real formula error is returned by both getValues() and getDisplayValues().
  // This recreates the state seen by the second request after recalculation.
  sheet.setFixtureValue(1, 13, "#ERROR!");
  const second = post(context, payload);
  assert.equal(second.ok, false);
  assert.equal(second.status, 500);
  assert.match(second.error, /summary formula needs repair/);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(sheet.values.filter(row => row[map["Record ID"] - 1] === "fill-1").length, 1);

  sheet.setFixtureValue(1, 13, "$ Return");
  sheet.displayOverrides.clear();
  const third = post(context, payload);
  assert.equal(third.status, 200);
  assert.equal(third.operation, "updated");
  assert.equal(third.row, 59);
  assert.equal(sheet.getMaxRows(), 59);
  assert.equal(sheet.values.filter(row => row[map["Record ID"] - 1] === "fill-1").length, 1);
});

test("a preallocated 7012-row Sheet places the first new fill directly after 58 trades", () => {
  const sheet = legacyFixture(29, 7012, 59);
  const { context } = loadScript(sheet);
  const response = post(context, fillPayload(context));

  assert.equal(response.status, 200);
  assert.equal(response.operation, "inserted");
  assert.equal(response.row, 60);
  assert.equal(sheet.getMaxRows(), 7012);
  assert.equal(sheet.mutations.some(item => item.type === "insertRows"), false);
  const map = headerMap(sheet);
  assert.equal(sheet.valueAt(60, map["Record ID"]), "fill-1");
  assert.equal(sheet.valueAt(59, map["Ticker"]), "OLD59");
});
