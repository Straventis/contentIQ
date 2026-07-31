const fs = require("fs");
const path = require("path");
const { parseCSVFile } = require("../lib/csvParser.js");

module.exports = function () {
  const filePath = path.join(__dirname, "daily_totals.csv");
  const rows = parseCSVFile(filePath);
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
