const fs = require("fs");
const path = require("path");

// Same quote-aware parser as posts.js -- demographic values shouldn't ever
// contain commas, but keeping every CSV reader in this repo consistent
// avoids the exact class of bug that broke pillar values earlier.
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
  const filePath = path.join(__dirname, "demographics.csv");
  if (!fs.existsSync(filePath)) {
    return { pulledAt: null, categories: {} };
  }

  const rows = parseCSV(filePath);
  if (!rows.length) {
    return { pulledAt: null, categories: {} };
  }

  const categories = {};
  rows.forEach((r) => {
    if (!categories[r.category]) categories[r.category] = [];
    // Percentage arrives as a string like "8%" or "< 1%" -- keep the raw
    // label for display, but also parse a sortable number ("< 1%" becomes
    // 0.5 so it sorts near the bottom rather than crashing the sort).
    const numeric = r.percentage && r.percentage.indexOf("<") > -1
      ? 0.5
      : parseFloat(r.percentage) || 0;
    categories[r.category].push({ value: r.value, percentage: r.percentage, percentageValue: numeric });
  });

  Object.keys(categories).forEach((cat) => {
    categories[cat].sort((a, b) => b.percentageValue - a.percentageValue);
  });

  return {
    pulledAt: rows[0].pulled_at,
    categories,
  };
};
