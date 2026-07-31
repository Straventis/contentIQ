const fs = require("fs");
const path = require("path");
const { parseCSVFile } = require("../lib/csvParser.js");

module.exports = function () {
  const filePath = path.join(__dirname, "demographics.csv");
  if (!fs.existsSync(filePath)) {
    return { pulledAt: null, categories: {} };
  }

  const rows = parseCSVFile(filePath);
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
