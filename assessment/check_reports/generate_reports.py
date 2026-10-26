#!/usr/bin/env python3
"""
Generate per-team defect verification reports from defect_list.csv
by cross-checking claimed request_id hashes against request_logs.

Usage:
    python3 generate_reports.py \
        --csv check_reports/course_files/262/defect_list.csv \
        --outdir check_reports/course_files/262/assessments

Also generates two aggregate CSVs in --outdir:
    - bug_findings.csv        (all teams' verified bug claims)
    - individual_contribution.csv (per-student stats across all teams,
      with one column per distinct route, e.g. route:/orders)

Assumptions:
- Fixed columns: team_id, member1..4, susp, indiv_contrib, specification,
  tests_with_id, defects_with_id, report_structure, report_marks
- All columns after report_marks are bug titles.
- Cell value = substring of a request_logs.request_id (uuid).
- Matching: WHERE request_id::text LIKE '%<value>%'. No offering/team scope.
- A bug is credited ONLY to the student_id on the matched log row — not
  the whole team. If that student_id isn't one of the team's members,
  it's flagged (could mean shared credentials / wrong team mapping).
- If a hash fragment matches multiple request_ids, we try to resolve the
  ambiguity by narrowing to matches sent by one of the team's own members.
  If exactly one such match remains, it's auto-resolved and flagged as
  "(resolved: unique match among team members)". Otherwise it's reported
  in the Ambiguous table (narrowed down if possible).
- `dedup_hash` (md5 of method|path|query|body) is used ONLY to find the
  first time this exact same request (by content) was ever sent by the
  claiming student — NOT to determine "first to find" across teams.
- All timestamps displayed in Bangladesh time (UTC+6).
- "Unique requests sent" = distinct dedup_hash values (not raw row count).
- Route-wise counts collapse to top-level resource (/auth, /restaurants,
  /menu-items, ...), stripping api/offering prefix.
- Method+Path counts normalize dynamic segments (uuid/numeric/param-like)
  to `:id` so different literal ids/uuids aggregate together.
"""

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get("STQA_DB_URL", "postgres://stqa:stqa@localhost:5433/stqa_logging")

BD_TZ = timezone(timedelta(hours=6))

FIXED_COLS = [
    "team_id", "member1", "member2", "member3", "member4",
    "susp", "indiv_contrib", "specification", "tests_with_id",
    "defects_with_id", "report_structure", "report_marks",
]

HASH_RE = re.compile(r"^[0-9a-fA-F\.\-eE\+]{4,}$")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
NUMERIC_RE = re.compile(r"^\d+$")


def looks_like_hash(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if re.search(r"[A-Za-z]{3,}", value) and not re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    return bool(HASH_RE.match(value))


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames
    bug_cols = [c for c in fieldnames if c not in FIXED_COLS]
    return rows, bug_cols


def find_matches(cur, value):
    cur.execute(
        """
        SELECT request_id, offering_id, team_id, student_id, started_at,
               completed_at, method, path, query_string, status_code,
               request_body, request_body_hash, response_body_hash,
               dedup_hash, application_user_id, application_role,
               application_authenticated
        FROM request_logs
        WHERE request_id::text LIKE %s
        ORDER BY started_at ASC
        """,
        (f"%{value}%",),
    )
    return cur.fetchall()


def resolve_ambiguous_matches(matches, members):
    """
    If multiple request_id matches exist, try to resolve by checking which
    ones were sent by a student in this team's member list.
    Returns (resolved_match_or_None, remaining_ambiguous_matches_or_None).
    """
    team_matches = [m for m in matches if m["student_id"] in members]
    if len(team_matches) == 1:
        return team_matches[0], None
    if len(team_matches) > 1:
        return None, team_matches
    return None, matches


def first_sent_by_student(cur, dedup_hash, student_id):
    """First time THIS student sent this exact same request (by content)."""
    cur.execute(
        """
        SELECT started_at
        FROM request_logs
        WHERE dedup_hash = %s AND student_id = %s
        ORDER BY started_at ASC
        LIMIT 1
        """,
        (dedup_hash, student_id),
    )
    return cur.fetchone()


def fmt_bd(ts):
    if not ts:
        return "N/A"
    return ts.astimezone(BD_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fmt_td(td):
    if td is None:
        return ""
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def build_team_members(row):
    members = []
    for key in ("member1", "member2", "member3", "member4"):
        v = (row.get(key) or "").strip()
        if v:
            v = "0" + v
            members.append(v)
    return members


def normalize_path_segment(seg):
    """Replace uuid/numeric/param-like segments with a placeholder."""
    if UUID_RE.match(seg):
        return ":id"
    if NUMERIC_RE.match(seg):
        return ":id"
    if seg.startswith(":") or seg.startswith("%7B") or seg.startswith("{"):
        return ":id"
    return seg


def route_group(path):
    """Collapse to top-level resource, e.g. /api/panda-262/auth/register -> /auth"""
    segs = [s for s in path.split("/") if s]
    filtered = [s for s in segs if s != "api" and not re.match(r"^panda-\w+$", s)]
    if not filtered:
        return path
    return "/" + filtered[0]


def normalize_method_path(path):
    """Normalize full path: strip api/offering prefix, replace dynamic segments with :id."""
    segs = [s for s in path.split("/") if s]
    filtered = [s for s in segs if s != "api" and not re.match(r"^panda-\w+$", s)]
    normalized = [normalize_path_segment(s) for s in filtered]
    return "/" + "/".join(normalized) if normalized else path


def student_activity(cur, student_id, credited_bugs):
    cur.execute(
        """
        SELECT request_id, started_at, method, path, status_code, dedup_hash
        FROM request_logs
        WHERE student_id = %s
        ORDER BY started_at ASC
        """,
        (student_id,),
    )
    rows = cur.fetchall()

    route_counts = defaultdict(int)
    method_path_counts = defaultdict(int)
    hour_buckets = defaultdict(int)
    unique_dedup_hashes = set()

    for r in rows:
        if r["dedup_hash"]:
            unique_dedup_hashes.add(r["dedup_hash"])
        route_counts[route_group(r["path"])] += 1
        method_path_counts[f"{r['method']} {normalize_method_path(r['path'])}"] += 1
        if r["started_at"]:
            hour_key = r["started_at"].astimezone(BD_TZ).replace(minute=0, second=0, microsecond=0)
            hour_buckets[hour_key] += 1

    active_hours = sorted(h for h, c in hour_buckets.items() if c >= 5)
    all_hours = sorted(hour_buckets.keys())

    return {
        "unique_requests": len(unique_dedup_hashes),
        "bugs_found": len(credited_bugs),
        "bug_titles": credited_bugs,
        "route_counts": dict(sorted(route_counts.items(), key=lambda x: -x[1])),
        "method_path_counts": dict(sorted(method_path_counts.items(), key=lambda x: -x[1])),
        "active_hours": active_hours,
        "all_hours": all_hours,
    }


def md_table(headers, rows):
    """rows: list of list[str]"""
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def generate_report(cur, row, bug_cols, global_bug_rows, global_indiv_rows):
    """
    Generates the markdown report for one team AND appends structured rows
    into the shared global_bug_rows / global_indiv_rows lists (used later
    to write the aggregate CSVs across all teams).
    """
    team_id = row["team_id"]
    members = build_team_members(row)

    lines = []
    lines.append(f"# Team {team_id}")
    lines.append("")
    lines.append("## Members")
    for m in members:
        lines.append(f"- {m}")
    lines.append("")

    lines.append("## Metadata")
    for key in ("susp", "indiv_contrib", "specification", "tests_with_id",
                "defects_with_id", "report_structure", "report_marks"):
        val = (row.get(key) or "").strip()
        lines.append(f"- **{key}**: {val if val else '_(blank)_'}")
    lines.append("")

    team_claims = []          # for timeline table
    credited_by_student = defaultdict(list)
    unverifiable_rows = []
    not_found_rows = []
    ambiguous_rows = []
    verified_rows = []
    first_sent_rows = []      # for "first time sent" table

    for bug in bug_cols:
        raw = (row.get(bug) or "").strip()
        if not raw:
            continue

        if not looks_like_hash(raw):
            unverifiable_rows.append([bug, raw])
            continue

        matches = find_matches(cur, raw)

        if not matches:
            not_found_rows.append([bug, raw])
            continue

        note = ""
        if len(matches) > 1:
            resolved, remaining = resolve_ambiguous_matches(matches, members)
            if resolved:
                m = resolved
                note = " (resolved: unique match among team members)"
            else:
                detail = "; ".join(
                    f"`{mm['request_id']}` @ {fmt_bd(mm['started_at'])} "
                    f"{mm['method']} {mm['path']} -> {mm['status_code']} (student: {mm['student_id']})"
                    for mm in remaining
                )
                narrowed_note = (
                    " (narrowed to team members)" if len(remaining) < len(matches) else ""
                )
                ambiguous_rows.append([bug, raw, len(remaining), detail + narrowed_note])
                continue
        else:
            m = matches[0]

        claimant_student = m["student_id"]
        member_flag = note if note else ("" if claimant_student in members else "⚠️ not listed member")

        body = (m["request_body"] or "")[:200]

        verified_rows.append([
            bug,
            f"`{m['request_id']}`",
            claimant_student or "N/A",
            member_flag,
            fmt_bd(m["started_at"]),
            m["method"],
            m["path"],
            m["status_code"],
            f"`{body}`",
        ])

        # accumulate for global bug_findings.csv (raw values, no markdown formatting)
        global_bug_rows.append({
            "team_id": team_id,
            "bug": bug,
            "request_id": str(m["request_id"]),
            "found_by": claimant_student or "N/A",
            "flag": member_flag,
            "started_at_bd": fmt_bd(m["started_at"]),
            "method": m["method"],
            "path": m["path"],
            "status": m["status_code"],
            "body": (m["request_body"] or ""),
        })

        first = None
        if claimant_student:
            first = first_sent_by_student(cur, m["dedup_hash"], claimant_student)

        if first and first["started_at"]:
            first_sent_rows.append({
                "bug": bug,
                "student_id": claimant_student,
                "first_sent_at": first["started_at"],
                "claimed_at": m["started_at"],
            })

        if claimant_student:
            credited_by_student[claimant_student].append(bug)

        team_claims.append({
            "bug": bug,
            "started_at": m["started_at"],
            "student_id": claimant_student,
        })

    lines.append("## Claimed Defects — Verified")
    if verified_rows:
        lines.append(md_table(
            ["Bug", "request_id", "found_by", "flag", "started_at (BD)", "method", "path", "status", "body"],
            verified_rows,
        ))
    else:
        lines.append("_None verified._")
    lines.append("")

    if ambiguous_rows:
        lines.append("## Claimed Defects — Ambiguous")
        lines.append(md_table(["Bug", "Claim value", "# matches", "Details"], ambiguous_rows))
        lines.append("")

    if not_found_rows:
        lines.append("## Claimed Defects — Not Found")
        lines.append(md_table(["Bug", "Claim value"], not_found_rows))
        lines.append("")

    if unverifiable_rows:
        lines.append("## Claimed Defects — Unverifiable (non-hash value)")
        lines.append(md_table(["Bug", "Claim value"], unverifiable_rows))
        lines.append("")

    if first_sent_rows:
        lines.append("## First Time Each Claimed Request Was Sent (by claiming student)")
        first_sent_rows.sort(key=lambda r: r["first_sent_at"])
        table_rows = []
        prev_ts = None
        for r in first_sent_rows:
            diff = fmt_td(r["first_sent_at"] - prev_ts) if prev_ts else ""
            table_rows.append([
                fmt_bd(r["first_sent_at"]),
                diff,
                r["bug"],
                r["student_id"],
            ])
            prev_ts = r["first_sent_at"]
        lines.append(md_table(["First sent at (BD)", "Δ since previous", "Bug", "Student"], table_rows))
        lines.append("")

    if team_claims:
        lines.append("## Team Claim Timeline (sorted by started_at)")
        team_claims.sort(key=lambda c: c["started_at"] or datetime.min.replace(tzinfo=timezone.utc))
        table_rows = []
        prev = None
        for c in team_claims:
            diff = fmt_td(c["started_at"] - prev["started_at"]) if prev and c["started_at"] and prev["started_at"] else ""
            table_rows.append([fmt_bd(c["started_at"]), diff, c["bug"], c["student_id"] or "N/A"])
            prev = c
        lines.append(md_table(["Date/Time (BD)", "Δ since previous", "Bug", "Student"], table_rows))
        lines.append("")

    lines.append("## Individual Contribution")
    for student_id in members:
        credited_bugs = credited_by_student.get(student_id, [])
        stats = student_activity(cur, student_id, credited_bugs)
        lines.append(f"### {student_id}")
        lines.append(f"- Bugs found (credited via request logs): {stats['bugs_found']}")
        if stats["bug_titles"]:
            for bt in stats["bug_titles"]:
                lines.append(f"  - {bt}")
        lines.append(f"- Unique requests sent (distinct dedup_hash): {stats['unique_requests']}")
        lines.append("- Route-wise request counts:")
        for route, cnt in list(stats["route_counts"].items())[:20]:
            lines.append(f"  - `{route}`: {cnt}")
        lines.append("- Method+Path-wise request counts:")
        for mp, cnt in list(stats["method_path_counts"].items())[:20]:
            lines.append(f"  - `{mp}`: {cnt}")
        lines.append(f"- Active hours (≥5 req/hour): {len(stats['active_hours'])}")
        lines.append(f"- Total distinct hours active: {len(stats['all_hours'])}")
        lines.append("")

        # accumulate for global individual_contribution.csv
        # (store raw dict; CSV writer expands into one column per route)
        global_indiv_rows.append({
            "team_id": team_id,
            "student_id": student_id,
            "bugs_found": stats["bugs_found"],
            "unique_requests_sent": stats["unique_requests"],
            "route_counts": stats["route_counts"],  # dict: {route: count}
            "active_hours_count": len(stats["active_hours"]),
            "total_distinct_hours_active": len(stats["all_hours"]),
        })

    return "\n".join(lines)


def write_bug_findings_csv(path, global_bug_rows):
    fieldnames = [
        "team_id", "bug", "request_id", "found_by", "flag",
        "started_at_bd", "method", "path", "status", "body",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in global_bug_rows:
            writer.writerow(row)


def write_individual_contribution_csv(path, global_indiv_rows):
    # collect every distinct route seen across all students, sorted for
    # stable column ordering
    all_routes = set()
    for row in global_indiv_rows:
        all_routes.update(row["route_counts"].keys())
    all_routes = sorted(all_routes)

    fixed_fieldnames = [
        "team_id", "student_id", "bugs_found", "unique_requests_sent",
    ]
    route_fieldnames = [f"route:{r}" for r in all_routes]
    tail_fieldnames = ["active_hours_count", "total_distinct_hours_active"]
    fieldnames = fixed_fieldnames + route_fieldnames + tail_fieldnames

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in global_indiv_rows:
            out = {
                "team_id": row["team_id"],
                "student_id": row["student_id"],
                "bugs_found": row["bugs_found"],
                "unique_requests_sent": row["unique_requests_sent"],
                "active_hours_count": row["active_hours_count"],
                "total_distinct_hours_active": row["total_distinct_hours_active"],
            }
            for r in all_routes:
                out[f"route:{r}"] = row["route_counts"].get(r, 0)
            writer.writerow(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    rows, bug_cols = load_csv(args.csv)

    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()

    global_bug_rows = []
    global_indiv_rows = []

    for row in rows:
        team_id = row["team_id"]
        members = build_team_members(row)
        filename = "_".join(members) if members else team_id
        filename = re.sub(r"[^\w\-]", "_", filename) + ".md"
        out_path = os.path.join(args.outdir, filename)

        report = generate_report(cur, row, bug_cols, global_bug_rows, global_indiv_rows)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Wrote {out_path}")

    bug_csv_path = os.path.join(args.outdir, "bug_findings.csv")
    indiv_csv_path = os.path.join(args.outdir, "individual_contribution.csv")

    write_bug_findings_csv(bug_csv_path, global_bug_rows)
    write_individual_contribution_csv(indiv_csv_path, global_indiv_rows)

    print(f"Wrote {bug_csv_path}")
    print(f"Wrote {indiv_csv_path}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()