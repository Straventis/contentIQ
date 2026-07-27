const fs = require("fs");
const path = require("path");

// Same quote-aware parser as posts.js -- daily_totals.csv is simple numeric
// data today so the naive split never actually broke here, but keeping both
// CSV readers in sync avoids the two files silently drifting apart again.
function parseCSVLine(line) {
  const values = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') { current += '"'; i++; }
        else { inQuotes = false; }
      } else {
        current += char;
      }
    } else {
      if (char === '"') { inQuotes = true; }
      else if (char === ",") { values.push(current); current = ""; }
      else { current += char; }
    }
  }
  values.push(current);
  return values;
}

function parseCSV(filePath) {
  const raw = fs.readFileSync(filePath, "utf-8").trim();
  const lines = raw.split(/\r?\n/);
  const headers = parseCSVLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCSVLine(line);
    const row = {};
    headers.forEach((h, i) => { row[h] = values[i] !== undefined ? values[i] : ""; });
    return row;
  });
}

module.exports = function () {
  const filePath = path.join(__dirname, "daily_totals.csv");
  const rows = parseCSV(filePath);
  return rows
    .map((r) => ({
      date: r.date,
      impressions: Number(r.impressions) || 0,
      engagements: Number(r.engagements) || 0,
      // Not previously exposed here even though the CSV has always had it --
      // needed now for the Followers mini-chart.
      newFollowers: Number(r.new_followers) || 0,
    }))
    .sort((a, b) => new Date(a.date) - new Date(b.date));
};
