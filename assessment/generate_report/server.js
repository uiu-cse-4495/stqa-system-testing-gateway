/**
 * Minimal backend for the Activity Report.
 *
 * - Reads the CSV export from disk (data/activity.csv by default).
 * - Normalizes it into the flat JSON shape the frontend expects.
 * - Serves it at GET /api/activity.
 * - Also serves the static frontend (public/) so the whole thing
 *   runs as one process: `node server.js` then open http://localhost:3000
 *
 * Swap `readCsvActivities()` for a real database query later —
 * the frontend only depends on the /api/activity JSON shape, not
 * on how it was produced (see spec section 20, CSV -> API swap).
 */

const express = require("express");
const fs = require("fs");
const path = require("path");
const Papa = require("papaparse");

const PORT = process.env.PORT || 3002;
const CSV_PATH = process.env.ACTIVITY_CSV || path.join(__dirname, ".", "activity.csv");

console.log(`Reading CSV from: ${CSV_PATH}`);

const app = express();
app.use(express.static(path.join(__dirname, "public")));

function normalizeStartedAt(raw) {
  if (!raw) return raw;
  let s = raw.trim().replace(" ", "T");
  const tz = s.match(/([+-]\d{2})$/);
  if (tz) s = s + ":00";
  return s;
}

// Cache the parsed file in memory; re-read if the file's mtime changes.
let cache = { mtimeMs: 0, data: [] };

function readCsvActivities() {
  const stat = fs.statSync(CSV_PATH);
  if (stat.mtimeMs === cache.mtimeMs) return cache.data;

  const csvText = fs.readFileSync(CSV_PATH, "utf8");
  const { data, errors } = Papa.parse(csvText, { header: true, skipEmptyLines: true });
  if (errors && errors.length) {
    console.warn(`CSV parse warnings in ${CSV_PATH}:`, errors.slice(0, 5));
  }

  const normalized = data
    .filter((row) => row.request_id)
    .map((row) => ({
      ...row,
      started_at: normalizeStartedAt(row.started_at),
    }));

  cache = { mtimeMs: stat.mtimeMs, data: normalized };
  return normalized;
}

app.get("/api/activity", (req, res) => {
  try {
    const activities = readCsvActivities();
    res.json(activities);
  } catch (err) {
    console.error("Failed to read activity CSV:", err.message);
    res.status(500).json({ error: "Could not read activity data. Check ACTIVITY_CSV path on the server." });
  }
});

app.listen(PORT, () => {
  console.log(`Activity Report running at http://localhost:${PORT}`);
  console.log(`Reading CSV from: ${CSV_PATH}`);
});