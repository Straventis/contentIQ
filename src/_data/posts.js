const fs = require("fs");
const path = require("path");

// Proper quote-aware CSV parsing. A plain line.split(",") breaks the moment
// any field (e.g. a post title) contains a comma inside quotes -- which
// happens routinely with real post titles ("Output, Inference, and
// Prediction: Why Engineers...") and silently shifts every column after it,
// corrupting pillar/content_type/impressions for that row. This parser
// respects quoted fields per standard CSV rules, including escaped "" for a
// literal quote character inside a quoted field.
function parseCSVLine(line) {
  const values = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') { current += '"'; i++; } // escaped quote
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

function slugify(text) {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "")
    .slice(0, 60);
}

function postIdFromUrl(url) {
  const match = url.match(/(?:share|ugcPost)-(\d+)/);
  return match ? match[1] : url;
}

module.exports = function () {
  const masterPath = path.join(__dirname, "master.csv");
  const snapshotPath = path.join(__dirname, "post_snapshots.csv");

  const master = parseCSV(masterPath);
  const snapshots = parseCSV(snapshotPath);

  // Only include posts that have been manually tagged with a content_type,
  // untagged historical posts stay out of the dashboard for now.
  const tagged = master.filter((r) => r.content_type && r.content_type.trim() !== "");

  const usedSlugs = new Set();

  const posts = tagged.map((r) => {
    const pid = postIdFromUrl(r.post_url);
    const history = snapshots
      .filter((s) => s.post_id === pid)
      .map((s) => ({
        pulled_at: s.pulled_at,
        impressions: Number(s.impressions) || 0,
        engagements: Number(s.total_engagements) || 0,
      }))
      .sort((a, b) => new Date(a.pulled_at) - new Date(b.pulled_at));

    // Two posts (e.g. an Article and its teaser) can share identical topic
    // text, which would otherwise generate the same slug and the same
    // output path, crashing the build. Only actual collisions get a short
    // disambiguating suffix appended, every other post's URL is untouched.
    let slug = slugify(r.post_topic || pid);
    if (usedSlugs.has(slug)) {
      slug = slug + "-" + pid.slice(-6);
    }
    usedSlugs.add(slug);

    return {
      id: pid,
      slug,
      date: r.date,
      url: r.post_url,
      topic: r.post_topic,
      contentType: r.content_type,
      pillar: r.pillar,
      impressions: Number(r.impressions) || 0,
      engagements: Number(r.total_engagements) || 0,
      engagementRate: Number(r.engagement_rate) || 0,
      history,
    };
  });

  // Newest first
  posts.sort((a, b) => new Date(b.date) - new Date(a.date));

  return posts;
};
