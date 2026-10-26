import React, { useMemo, useState, useRef, useCallback } from "react";
import Papa from "papaparse";

/* ============================================================
   Activity Report Dashboard — V1
   Self-contained module. Drop <ActivityReport activities={...} />
   into any host page. All CSS is scoped under .ar-root so it
   cannot leak into / be affected by the parent site's styles.
   ============================================================ */

const DHAKA_TZ = "Asia/Dhaka";
const PX_PER_HOUR = 44;
const CHART_HEIGHT = 200;

/* ---------- timezone-safe helpers (Asia/Dhaka) ---------- */

function toDhakaParts(isoString) {
  const d = new Date(isoString);
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: DHAKA_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
  });
  const parts = {};
  fmt.formatToParts(d).forEach((p) => (parts[p.type] = p.value));
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
  };
}

function dateKeyOf(p) {
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}
function bucketKeyOf(p) {
  return `${dateKeyOf(p)}T${String(p.hour).padStart(2, "0")}`;
}
function addDaysToKey(dateKey, days) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, "0")}-${String(
    dt.getUTCDate()
  ).padStart(2, "0")}`;
}
function formatDateLabel(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}
function formatDateShort(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.toLocaleDateString("en-US", { day: "numeric", month: "short", timeZone: "UTC" });
}
function formatHourLabel(hour) {
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  const suffix = hour < 12 ? "AM" : "PM";
  return `${h12} ${suffix}`;
}

/* Build every hourly bucket between start and end date (inclusive), in order */
function buildBuckets(startDate, endDate) {
  const buckets = [];
  let cursor = startDate;
  let guard = 0;
  while (cursor <= endDate && guard < 4000) {
    for (let hour = 0; hour < 24; hour++) {
      buckets.push({ dateKey: cursor, hour, key: `${cursor}T${String(hour).padStart(2, "0")}` });
    }
    if (cursor === endDate) break;
    cursor = addDaysToKey(cursor, 1);
    guard++;
  }
  return buckets;
}

/* ---------- mock data (stand-in for parent-supplied `activities`) ---------- */

function generateMockData() {
  const students = [
    { student_id: "101701", team_id: "E1" },
    { student_id: "101702", team_id: "E1" },
    { student_id: "101801", team_id: "E2" },
  ];
  const rows = [];
  let idCounter = 1;
  for (let dayOffset = 0; dayOffset < 10; dayOffset++) {
    const day = addDaysToKey("2026-08-05", dayOffset);
    students.forEach((s) => {
      const activeHoursToday = 4 + Math.floor(Math.random() * 6);
      for (let i = 0; i < activeHoursToday; i++) {
        const hour = 8 + Math.floor(Math.random() * 12); // 8am - 8pm-ish Dhaka
        const reqCount = 1 + Math.floor(Math.random() * 6);
        for (let r = 0; r < reqCount; r++) {
          const minute = Math.floor(Math.random() * 60);
          const iso = `${day}T${String(hour).padStart(2, "0")}:${String(minute).padStart(
            2,
            "0"
          )}:00.000Z`; // stored as UTC in the CSV
          rows.push({
            request_id: `mock-${idCounter++}`,
            team_id: s.team_id,
            student_id: s.student_id,
            started_at: iso,
          });
        }
      }
    });
  }
  return rows;
}

/* ================= Sub-components ================= */

function SearchSelect({ placeholder, options, value, onChange }) {
  const [text, setText] = useState(value || "");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);

  const filtered = useMemo(() => {
    if (!text) return options.slice(0, 8);
    return options.filter((o) => o.toLowerCase().includes(text.toLowerCase())).slice(0, 8);
  }, [text, options]);

  return (
    <div className="ar-field ar-search-wrap" ref={wrapRef}>
      <span className="ar-label">Search</span>
      <div className="ar-search-box">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="7" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="text"
          placeholder={placeholder}
          value={text}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setText(e.target.value);
            onChange(null);
            setOpen(true);
          }}
          onBlur={() => setTimeout(() => setOpen(false), 120)}
        />
      </div>
      {open && filtered.length > 0 && (
        <div className="ar-dropdown">
          {filtered.map((id) => (
            <div
              key={id}
              className="ar-dropdown-item"
              onMouseDown={() => {
                setText(id);
                onChange(id);
                setOpen(false);
              }}
            >
              {id}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SummaryCard({ label, value }) {
  return (
    <div className="ar-summary-card">
      <span className="ar-summary-label">{label}</span>
      <span className="ar-summary-value">{value}</span>
    </div>
  );
}

function HourlyChart({ buckets, countMap }) {
  const [tooltip, setTooltip] = useState(null); // { left, top, bucket, count }
  const scrollRef = useRef(null);

  const maxCount = useMemo(
    () => buckets.reduce((m, b) => Math.max(m, countMap.get(b.key) || 0), 0) || 1,
    [buckets, countMap]
  );

  const labelEvery = buckets.length <= 30 ? 1 : buckets.length <= 100 ? 3 : buckets.length <= 300 ? 6 : 12;

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(maxCount * f));

  const handleEnter = useCallback(
    (e, bucket, count) => {
      const barRect = e.currentTarget.getBoundingClientRect();
      const wrapRect = scrollRef.current.getBoundingClientRect();
      setTooltip({
        left: barRect.left - wrapRect.left + scrollRef.current.scrollLeft + barRect.width / 2,
        top: barRect.top - wrapRect.top,
        bucket,
        count,
      });
    },
    []
  );

  return (
    <div className="ar-chart-card">
      <h3 className="ar-chart-title">Requests by hour</h3>
      <div className="ar-chart-body">
        <div className="ar-y-axis">
          {yTicks
            .slice()
            .reverse()
            .map((t, i) => (
              <span key={i} className="ar-y-tick">
                {t}
              </span>
            ))}
        </div>
        <div className="ar-chart-scroll" ref={scrollRef}>
          <div className="ar-chart-inner" style={{ width: buckets.length * PX_PER_HOUR }}>
            <div className="ar-gridlines">
              {yTicks.map((_, i) => (
                <div key={i} className="ar-gridline" />
              ))}
            </div>
            <div className="ar-bars">
              {buckets.map((b, i) => {
                const count = countMap.get(b.key) || 0;
                const isNewDay = i === 0 || buckets[i - 1].dateKey !== b.dateKey;
                const barH = count === 0 ? 0 : Math.max(2, (count / maxCount) * CHART_HEIGHT);
                const showLabel = i % labelEvery === 0;
                return (
                  <div key={b.key} className={"ar-bar-col" + (isNewDay ? " ar-newday" : "")}>
                    {isNewDay && <span className="ar-day-label">{formatDateShort(b.dateKey)}</span>}
                    <div className="ar-bar-track">
                      <div
                        className="ar-bar"
                        style={{ height: barH }}
                        onMouseEnter={(e) => handleEnter(e, b, count)}
                        onMouseLeave={() => setTooltip(null)}
                      />
                    </div>
                    {showLabel && <span className="ar-hour-label">{formatHourLabel(b.hour)}</span>}
                  </div>
                );
              })}
            </div>
            {tooltip && (
              <div
                className="ar-tooltip"
                style={{ left: tooltip.left, top: Math.max(0, tooltip.top - 54) }}
              >
                <div className="ar-tooltip-date">{formatDateLabel(tooltip.bucket.dateKey)}</div>
                <div className="ar-tooltip-range">
                  {formatHourLabel(tooltip.bucket.hour)} – {formatHourLabel((tooltip.bucket.hour + 1) % 24)}
                </div>
                <div className="ar-tooltip-count">Requests: {tooltip.count}</div>
              </div>
            )}
          </div>
        </div>
      </div>
      <p className="ar-scroll-hint">← scroll horizontally →</p>
    </div>
  );
}

/* ================= Main component ================= */

export function ActivityReport({ activities = [] }) {
  const [reportType, setReportType] = useState("student");
  const [selectedId, setSelectedId] = useState(null);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [error, setError] = useState("");
  const [report, setReport] = useState(null);

  const studentIds = useMemo(
    () => Array.from(new Set(activities.map((a) => String(a.student_id)))).sort(),
    [activities]
  );
  const teamIds = useMemo(
    () => Array.from(new Set(activities.map((a) => String(a.team_id)))).sort(),
    [activities]
  );

  const handleTypeChange = (type) => {
    setReportType(type);
    setSelectedId(null);
  };

  const generateReport = () => {
    if (!selectedId) {
      setError(`Select a ${reportType} ID before generating a report.`);
      return;
    }
    if (!startDate || !endDate) {
      setError("Choose both a start date and an end date.");
      return;
    }
    if (startDate > endDate) {
      setError("Start date must be before the end date.");
      return;
    }
    setError("");

    const matchField = reportType === "student" ? "student_id" : "team_id";
    const filtered = activities.filter((a) => {
      if (String(a[matchField]) !== String(selectedId)) return false;
      const p = toDhakaParts(a.started_at);
      const dk = dateKeyOf(p);
      return dk >= startDate && dk <= endDate;
    });

    if (filtered.length === 0) {
      setReport({ noData: true, type: reportType, id: selectedId, startDate, endDate });
      return;
    }

    const countMap = new Map();
    let teamOfStudent = null;
    filtered.forEach((a) => {
      const p = toDhakaParts(a.started_at);
      const key = bucketKeyOf(p);
      countMap.set(key, (countMap.get(key) || 0) + 1);
      if (reportType === "student" && !teamOfStudent) teamOfStudent = a.team_id;
    });

    const buckets = buildBuckets(startDate, endDate);

    let peakBucket = null;
    let peakCount = 0;
    let activeHours = 0;
    buckets.forEach((b) => {
      const c = countMap.get(b.key) || 0;
      if (c > 0) activeHours++;
      if (c > peakCount) {
        peakCount = c;
        peakBucket = b;
      }
    });

    setReport({
      noData: false,
      type: reportType,
      id: selectedId,
      teamOfStudent,
      startDate,
      endDate,
      total: filtered.length,
      activeHours,
      peakBucket,
      peakCount,
      buckets,
      countMap,
    });
  };

  return (
    <div className="ar-root">
      <style>{`
        .ar-root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1f2430; background: #f7f8fa; padding: 20px; border-radius: 10px; box-sizing: border-box; }
        .ar-root * { box-sizing: border-box; }
        .ar-title { font-size: 18px; font-weight: 600; margin: 0 0 16px; color: #12151c; }
        .ar-filters { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; background: #fff; border: 1px solid #e4e6eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 18px; }
        .ar-field { display: flex; flex-direction: column; gap: 5px; min-width: 140px; }
        .ar-label { font-size: 11px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; color: #6b7280; }
        .ar-select, .ar-date-input { border: 1px solid #d5d8de; border-radius: 6px; padding: 7px 10px; font-size: 13px; background: #fff; color: #1f2430; }
        .ar-search-wrap { position: relative; flex: 1; min-width: 200px; }
        .ar-search-box { display: flex; align-items: center; gap: 6px; border: 1px solid #d5d8de; border-radius: 6px; padding: 7px 10px; background: #fff; color: #9aa0ab; }
        .ar-search-box input { border: none; outline: none; font-size: 13px; flex: 1; color: #1f2430; background: transparent; }
        .ar-dropdown { position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px; background: #fff; border: 1px solid #e4e6eb; border-radius: 6px; box-shadow: 0 4px 12px rgba(20,20,30,0.08); z-index: 20; max-height: 200px; overflow-y: auto; }
        .ar-dropdown-item { padding: 8px 12px; font-size: 13px; cursor: pointer; }
        .ar-dropdown-item:hover { background: #f2f4f7; }
        .ar-date-row { display: flex; align-items: center; gap: 6px; }
        .ar-date-arrow { color: #9aa0ab; font-size: 12px; }
        .ar-generate-btn { background: #0f6e56; color: #fff; border: none; border-radius: 6px; padding: 9px 18px; font-size: 13px; font-weight: 600; cursor: pointer; margin-left: auto; }
        .ar-generate-btn:hover { background: #0c5945; }
        .ar-error { color: #b3261e; font-size: 12.5px; margin: -6px 0 14px; }
        .ar-empty-state { text-align: center; padding: 48px 20px; color: #6b7280; font-size: 14px; background: #fff; border: 1px solid #e4e6eb; border-radius: 8px; }
        .ar-empty-state strong { display: block; font-size: 15px; color: #1f2430; margin-bottom: 6px; }
        .ar-report-header { background: #fff; border: 1px solid #e4e6eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }
        .ar-report-title { font-size: 13px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin: 0 0 8px; }
        .ar-report-meta { display: flex; flex-wrap: wrap; gap: 20px; font-size: 14px; }
        .ar-report-meta b { color: #12151c; }
        .ar-report-range { font-size: 12.5px; color: #6b7280; margin-top: 6px; }
        .ar-summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
        .ar-summary-card { background: #fff; border: 1px solid #e4e6eb; border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; }
        .ar-summary-label { font-size: 11.5px; color: #6b7280; font-weight: 600; }
        .ar-summary-value { font-size: 20px; font-weight: 700; color: #12151c; }
        .ar-chart-card { background: #fff; border: 1px solid #e4e6eb; border-radius: 8px; padding: 16px; }
        .ar-chart-title { font-size: 14px; font-weight: 600; margin: 0 0 14px; color: #12151c; }
        .ar-chart-body { display: flex; }
        .ar-y-axis { display: flex; flex-direction: column; justify-content: space-between; height: ${CHART_HEIGHT}px; padding-right: 8px; font-size: 10.5px; color: #9aa0ab; text-align: right; }
        .ar-chart-scroll { overflow-x: auto; flex: 1; padding-bottom: 4px; }
        .ar-chart-inner { position: relative; min-width: 100%; }
        .ar-gridlines { position: absolute; top: 0; left: 0; right: 0; height: ${CHART_HEIGHT}px; display: flex; flex-direction: column; justify-content: space-between; pointer-events: none; }
        .ar-gridline { border-top: 1px solid #eef0f3; }
        .ar-bars { display: flex; align-items: flex-end; height: ${CHART_HEIGHT}px; position: relative; }
        .ar-bar-col { width: ${PX_PER_HOUR}px; flex-shrink: 0; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; position: relative; }
        .ar-newday { border-left: 1px dashed #d5d8de; }
        .ar-day-label { position: absolute; top: -18px; left: 4px; font-size: 10.5px; font-weight: 600; color: #4b5262; white-space: nowrap; }
        .ar-bar-track { width: 100%; height: 100%; display: flex; align-items: flex-end; justify-content: center; }
        .ar-bar { width: 60%; background: #0f6e56; border-radius: 3px 3px 0 0; min-height: 0; cursor: pointer; transition: background 0.1s; }
        .ar-bar:hover { background: #085041; }
        .ar-hour-label { position: absolute; bottom: -18px; font-size: 9.5px; color: #9aa0ab; white-space: nowrap; }
        .ar-tooltip { position: absolute; transform: translateX(-50%); background: #12151c; color: #fff; font-size: 11.5px; padding: 8px 10px; border-radius: 6px; z-index: 30; pointer-events: none; white-space: nowrap; }
        .ar-tooltip-date { font-weight: 600; margin-bottom: 2px; }
        .ar-tooltip-range { color: #c7cad1; margin-bottom: 4px; }
        .ar-tooltip-count { font-weight: 600; }
        .ar-scroll-hint { text-align: center; font-size: 11px; color: #9aa0ab; margin: 24px 0 0; }
        .ar-select-toggle { display: flex; border: 1px solid #d5d8de; border-radius: 6px; overflow: hidden; }
        @media (max-width: 640px) {
          .ar-filters { flex-direction: column; align-items: stretch; }
          .ar-generate-btn { margin-left: 0; width: 100%; }
          .ar-summary-grid { grid-template-columns: repeat(2, 1fr); }
        }
      `}</style>

      <h2 className="ar-title">Activity report</h2>

      <div className="ar-filters">
        <div className="ar-field">
          <span className="ar-label">Type</span>
          <select className="ar-select" value={reportType} onChange={(e) => handleTypeChange(e.target.value)}>
            <option value="student">Student</option>
            <option value="team">Team</option>
          </select>
        </div>

        <SearchSelect
          key={reportType}
          placeholder={reportType === "student" ? "Search student ID..." : "Search team ID..."}
          options={reportType === "student" ? studentIds : teamIds}
          value={selectedId}
          onChange={setSelectedId}
        />

        <div className="ar-field">
          <span className="ar-label">Start date</span>
          <input type="date" className="ar-date-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        </div>
        <div className="ar-field">
          <span className="ar-label">End date</span>
          <input type="date" className="ar-date-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        </div>

        <button className="ar-generate-btn" onClick={generateReport}>
          Generate report
        </button>
      </div>

      {error && <p className="ar-error">{error}</p>}

      {!report && !error && (
        <div className="ar-empty-state">
          Select a student or team, choose a date range, and generate a report.
        </div>
      )}

      {report && report.noData && (
        <div className="ar-empty-state">
          <strong>No activity found</strong>
          {report.type === "student" ? "Student" : "Team"} {report.id} has no recorded requests during the
          selected period.
        </div>
      )}

      {report && !report.noData && (
        <>
          <div className="ar-report-header">
            <p className="ar-report-title">
              {report.type === "student" ? "Student activity report" : "Team activity report"}
            </p>
            <div className="ar-report-meta">
              <span>
                {report.type === "student" ? "Student" : "Team"}: <b>{report.id}</b>
              </span>
              {report.type === "student" && report.teamOfStudent && (
                <span>
                  Team: <b>{report.teamOfStudent}</b>
                </span>
              )}
            </div>
            <p className="ar-report-range">
              {formatDateLabel(report.startDate)} — {formatDateLabel(report.endDate)}
            </p>
          </div>

          <div className="ar-summary-grid">
            <SummaryCard label="Total requests" value={report.total} />
            <SummaryCard label="Active hours" value={report.activeHours} />
            <SummaryCard
              label="Peak hour"
              value={
                report.peakBucket
                  ? `${formatDateShort(report.peakBucket.dateKey)} · ${formatHourLabel(report.peakBucket.hour)}`
                  : "—"
              }
            />
            <SummaryCard label="Peak requests" value={report.peakCount} />
          </div>

          <HourlyChart buckets={report.buckets} countMap={report.countMap} />
        </>
      )}
    </div>
  );
}

/* ================= Demo wrapper (remove when embedding) ================= */

export default function App() {
  const activities = useMemo(() => generateMockData(), []);
  return (
    <div style={{ maxWidth: 980, margin: "0 auto", padding: "24px 12px" }}>
      <ActivityReport activities={activities} />
    </div>
  );
}