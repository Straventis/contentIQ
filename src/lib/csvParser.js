// csvParser.js
//
// A real, correct CSV parser -- processes the whole file as one character
// stream and only treats a newline as a row boundary when it's NOT inside
// a quoted field. The previous parser (copy-pasted into posts.js,
// dailyTotals.js, and demographics.js) split the raw file into lines
// FIRST via raw.split(/\r?\n/), before any quote-awareness ran. That's
// correct for commas inside quotes, but breaks the moment a quoted field
// contains an actual line break -- which real LinkedIn post text does
// routinely. A post with an embedded newline got torn into multiple
// fragments, each parsed as its own (garbled, column-shifted) row.
// Confirmed against real data: 127 real rows were being parsed as 144
// fragments, silently corrupting 14 of them -- including making
// content_type empty for rows that had it, which then made those posts
// vanish from the dashboard entirely (posts.js filters on content_type
// being non-empty).
//
// This parses the ENTIRE file at once, character by character, so a
// newline inside an open quote is treated as literal content, not a row
// separator.

function parseCSV(raw) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  const len = raw.length;

  while (i < len) {
    const char = raw[i];

    if (inQuotes) {
      if (char === '"') {
        if (raw[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i++; continue;
      }
      field += char; i++; continue;
    }

    if (char === '"') { inQuotes = true; i++; continue; }
    if (char === ",") { row.push(field); field = ""; i++; continue; }
    if (char === "\r") { i++; continue; } // normalize CRLF, bare \r
    if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      i++;
      continue;
    }
    field += char; i++;
  }

  // Final field/row if the file doesn't end with a trailing newline.
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }

  return rows;
}

function parseCSVFile(filePath) {
  const fs = require("fs");
  const raw = fs.readFileSync(filePath, "utf-8");
  const rows = parseCSV(raw).filter((r) => !(r.length === 1 && r[0] === ""));
  if (!rows.length) return [];
  const headers = rows[0];
  return rows.slice(1).map((values) => {
    const obj = {};
    headers.forEach((h, idx) => { obj[h] = values[idx] !== undefined ? values[idx] : ""; });
    return obj;
  });
}

module.exports = { parseCSV, parseCSVFile };
