#!/usr/bin/env python3
"""Interactive grader for team assessment reports.

Walks through every not-yet-checked report in assessments_updated/, shows the
report and the team's logged activity, asks you the review questions, and
writes the results into STA.csv.

Prompt conventions
  yes/no questions : type y or n, optionally followed by a comment
                     (Enter = the default shown in capitals, no comment)
  numbers          : type a number; Enter accepts the default when one is shown
  individual marks : "90" or "90%" = percent of 7, "6.3/7" = raw marks
  any prompt       : ":skip" skips this team, ":q" saves and quits

Usage
  python grade_reports.py                 # all pending teams
  python grade_reports.py --team 31       # one team
  python grade_reports.py --redo --team 39
"""
import argparse
import os
import platform
import pydoc
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

BASE = Path("check_reports/course_files/262")
DONE_PREFIXES = ("checked", "cheched")  # "cheched" is a typo present in the folder
C_BUG = "bug_finding_marks_out_of_10"
C_REPORT = "report_marks_out_of_3"
C_INDIV = "individual_marks_out_of_7"
C_TOTAL = "total_marks_out_of_20"
C_TEAMBUGS = "team_bug_count_provisional"
C_NOTES = "assessment_notes"


class Quit(Exception):
    pass


class Skip(Exception):
    pass


# ----------------------------------------------------------------- prompts
def ask(msg):
    try:
        s = input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit
    if s.lower() == ":q":
        raise Quit
    if s.lower() == ":skip":
        raise Skip
    return s


def ask_yn(question, default):
    hint = "Y/n" if default else "y/N"
    while True:
        s = ask(f"{question} [{hint}] (+comment): ")
        if not s:
            return default, ""
        head, _, rest = s.partition(" ")
        if head.lower() in ("y", "yes"):
            return True, rest.strip()
        if head.lower() in ("n", "no"):
            return False, rest.strip()
        print("  Start with y or n, then an optional comment.")


def ask_number(question, lo, hi, default=None):
    d = f" [Enter = {default:g}]" if default is not None else ""
    while True:
        s = ask(f"{question} ({lo:g}-{hi:g}){d}: ")
        if not s and default is not None:
            return float(default)
        try:
            v = float(s)
        except ValueError:
            print("  Enter a number.")
            continue
        if lo <= v <= hi:
            return v
        print(f"  Must be between {lo:g} and {hi:g}.")


def parse_marks(s, out_of):
    """'90' / '90%' -> percent of out_of; '6.3/7' -> raw marks. None if invalid."""
    try:
        if "/" in s:
            v = float(s.split("/")[0])
            return round(v, 2) if 0 <= v <= out_of else None
        v = float(s.rstrip("%"))
        return round(v / 100 * out_of, 2) if 0 <= v <= 100 else None
    except ValueError:
        return None


def ask_marks(question, out_of, default=None):
    d = f" [Enter = {default:g}]" if default is not None else ""
    while True:
        s = ask(f"{question}{d}: ")
        if not s and default is not None:
            return float(default)
        v = parse_marks(s, out_of) if s else None
        if v is not None:
            return v
        print(f"  Use a percent (90 or 90%) or marks like 6.3/{out_of}.")


# -------------------------------------------------------------------- data
def parse_ids(path):
    return [int(x) for x in re.findall(r"\d+", path.stem)]


def is_done(path):
    return path.name.lower().startswith(DONE_PREFIXES)


def load_sta(path):
    df = pd.read_csv(path)
    df["student_id"] = pd.to_numeric(df["student_id"], errors="coerce").astype("Int64")
    for c in (C_BUG, C_REPORT, C_INDIV, C_TOTAL):
        if c not in df.columns:
            df[c] = pd.NA
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in (C_TEAMBUGS, C_NOTES):
        if c not in df.columns:
            df[c] = pd.NA
        df[c] = df[c].astype("object")
    return df


def save_sta(df, path):
    tmp = Path(str(path) + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def recompute_totals(df, mask):
    """total = bug + report + individual, only when all three are present."""
    for i in df.index[mask]:
        parts = [df.at[i, C_BUG], df.at[i, C_REPORT], df.at[i, C_INDIV]]
        if all(pd.notna(p) for p in parts):
            df.at[i, C_TOTAL] = round(sum(parts), 2)


def team_filled(rows):
    return rows[C_REPORT].notna().all() and rows[C_INDIV].notna().all()


def show_extra_csv(path, ids):
    """Show matching rows from bug_findings.csv if it has a student id column."""
    if not path.exists():
        return
    try:
        x = pd.read_csv(path)
        col = next((c for c in x.columns if "student" in c.lower()), None)
        if col is None:
            return
        ids_s = {str(i) for i in ids}
        hit = x[x[col].astype(str).str.lstrip("0").isin(ids_s)]
        if len(hit):
            print(f"\n--- {path.name} ---")
            print(hit.iloc[:, :12].to_string(index=False))
    except Exception as e:  # purely informational
        print(f"(could not read {path.name}: {e})")


def show_report(path, open_external, title=None):
    text = path.read_text(encoding="utf-8", errors="replace")
    print("\n" + "=" * 78)
    print(f"{title or 'REPORT: ' + path.name}  ({len(text.splitlines())} lines)")
    print("=" * 78)
    if open_external:
        opener = {"Darwin": "open", "Windows": "start"}.get(platform.system(), "xdg-open")
        try:
            subprocess.Popen([opener, str(path)], shell=(opener == "start"))
        except Exception as e:
            print(f"(could not open externally: {e})")
    # if len(text.splitlines()) > 50:
    #     pydoc.pager(text)  # q to leave the pager
    # else:
    #     print(text)

    # code -r path
    subprocess.run(["code", "-r", str(path)])


def find_submission_dirs(sub_dir, ids):
    """Folders in submissions2 whose student-ID set matches `ids`.

    Folder names list the IDs in a different order than the assessment files and
    sometimes contain typos or leading-zero differences, so match on ID sets:
      exact : same set of IDs
      strong: share at least 2 IDs (typo in one ID, team listed by fewer students)
      weak  : share a single ID (only offered, never opened automatically)
    """
    target = set(ids)
    exact, strong, weak = [], [], []
    for d in sorted(p for p in sub_dir.iterdir() if p.is_dir()):
        dids = set(parse_ids(d))
        k = len(dids & target)
        if not k:
            continue
        if dids == target:
            exact.append(d)
        elif k >= 2:
            strong.append((k, d))
        else:
            weak.append(d)
    strong = [d for _, d in sorted(strong, key=lambda t: -t[0])]
    return exact, strong, weak


def md_files_for(d):
    """report.md of a submission; if it has none, the .md files in its drive_folder."""
    report = d / "report.md"
    if report.exists():
        return [report]
    drive = d / "drive_folder"
    return sorted(drive.glob("*.md")) if drive.is_dir() else []


def show_submission_reports(sub_dir, ids, open_external):
    """Show the students' own submitted report (.md only) before the assessment report."""
    if not sub_dir.is_dir():
        print(f"\n(submissions folder not found: {sub_dir})")
        return
    exact, strong, weak = find_submission_dirs(sub_dir, ids)
    chosen = exact or strong
    for d in chosen:
        note = "" if d in exact else "  [partial ID match]"
        files = md_files_for(d)
        if not files:
            print(f"\n(submissions: {d.name} has no .md report){note}")
        for f in files:
            show_report(f, open_external, title=f"SUBMISSION: {d.name}/{f.relative_to(d)}{note}")
    if not chosen:
        if not weak:
            print("\n(no matching folder in submissions)")
        for d in weak:
            if ask(f"\nPossible submission (1 shared ID): {d.name}. Open its report? [y/N]: ").lower() in ("y", "yes"):
                for f in md_files_for(d):
                    show_report(f, open_external, title=f"SUBMISSION: {d.name}/{f.relative_to(d)}  [weak match]")


# ------------------------------------------------------------- one team
def review_team(df, team_id, rows_idx, path, args):
    rows = df.loc[rows_idx]
    ids = list(dict.fromkeys(int(s) for s in rows["student_id"]))  # unique, in order

    # One line per student: the STA file may hold several rows per student.
    cols = ["bugs_found", "unique_requests_sent", "active_hours_count",
            "total_distinct_hours_active"]
    per_student = (rows.groupby("student_id", sort=False)[cols].max().reset_index())

    print("\n" + "#" * 78)
    print(f"# {team_id}   ({len(ids)} students)   file: {path.name}")
    print("#" * 78)
    dup_ids = sorted({int(s) for s in rows.loc[rows["student_id"].duplicated(), "student_id"]})
    if dup_ids:
        print(f"WARNING: {len(rows)} rows in the CSV for {len(ids)} students; duplicate rows "
              f"for {dup_ids}. Showing the max per student; marks are written to every row.")
    print(per_student.to_string(index=False))
    show_extra_csv(path.parent / "bug_findings.csv", ids)
    if not args.no_sub:
        show_submission_reports(args.sub, parse_ids(path), args.open)  # students' own report first
    show_report(path, args.open, title=f"ASSESSMENT REPORT: {path.name}")

    print("\n--- Team-level questions ---")
    spec, c1 = ask_yn("Report consistent with the specification?", True)
    ai, c2 = ask_yn("AI-generated content suspected?", False)
    tcs, c3 = ask_yn("Proper test cases provided?", True)
    dids, c4 = ask_yn("Defect IDs / defect reports provided?", True)

    print("\n--- Numbers ---")
    csv_sum = int(per_student["bugs_found"].sum())
    team_bugs = int(ask_number("Total verified bugs for the team", 0, 999, csv_sum))
    report_mark = ask_number("Report mark", 0, 3)

    print("\n--- Individual marks (out of 7) ---")
    order = per_student.sort_values(["bugs_found", "unique_requests_sent"], ascending=False)
    group_default = None
    s = ask("Default for everyone (90 / 90% / 6.3/7), Enter = no default: ")
    if s:
        group_default = parse_marks(s, 7)
        if group_default is None:
            print("  Not understood, no default set.")
    indiv = {}
    for _, r in order.iterrows():
        sid = int(r["student_id"])
        label = (f"  {sid}  bugs={int(r['bugs_found'])} "
                 f"req={int(r['unique_requests_sent'])} hrs={int(r['active_hours_count'])}")
        indiv[sid] = ask_marks(label, 7, group_default)

    # build note
    parts = ["Report follows specification" if spec else "Report does not follow specification",
             "AI-generated content suspected" if ai else "not AI-generated",
             "test cases provided" if tcs else "no test cases provided",
             "defect IDs provided" if dids else "no defect IDs provided"]
    for label, c in (("spec", c1), ("AI", c2), ("test cases", c3), ("defect IDs", c4)):
        if c:
            parts.append(f"{label}: {c}")
    notes = "; ".join(parts) + "."

    # confirm
    print("\n--- Summary ---")
    print(f"{team_id}: bugs={team_bugs}  report={report_mark:g}/3")
    for sid, m in indiv.items():
        print(f"  {sid}: individual {m:g}/7")
    print(f"notes: {notes}")
    ok = ask("Save? [Y/n]: ").lower()
    if ok in ("n", "no"):
        print("Discarded.")
        return False

    df.loc[rows_idx, C_TEAMBUGS] = str(team_bugs)
    df.loc[rows_idx, C_REPORT] = report_mark
    df.loc[rows_idx, C_NOTES] = notes
    for i in rows_idx:
        df.at[i, C_INDIV] = indiv[int(df.at[i, "student_id"])]
    recompute_totals(df, df.index.isin(rows_idx))
    return True


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=BASE / "assessments")
    ap.add_argument("--sta", type=Path, default=BASE / "SystemTestingMarks.csv")
    ap.add_argument("--sub", type=Path, default=BASE / "submissions",
                    help="folder with the students' own submissions (report.md per team)")
    ap.add_argument("--no-sub", action="store_true", help="do not show the submitted report first")
    ap.add_argument("--team", help="only this team number, e.g. 31")
    ap.add_argument("--redo", action="store_true", help="include checked reports / filled teams")
    ap.add_argument("--open", action="store_true", help="also open each report in the default app")
    ap.add_argument("--no-rename", action="store_true", help="do not prefix finished files with 'checked '")
    args = ap.parse_args()

    df = load_sta(args.sta)
    backup = args.sta.with_name(f"{args.sta.stem}.{time.strftime('%Y%m%d-%H%M%S')}.bak.csv")
    shutil.copy2(args.sta, backup)
    print(f"Backup: {backup}")

    wanted = f"team_{int(args.team):02d}" if args.team else None
    queue = []
    # Student-ID sets of reports already marked "checked", so a second file for the
    # same team (IDs in a different order) is still recognised as done.
    done_sets = [frozenset(parse_ids(p)) for p in args.dir.glob("*.md") if is_done(p)]
    for p in sorted(args.dir.glob("*.md")):
        if is_done(p) and not args.redo:
            continue
        ids = parse_ids(p)
        if not args.redo and frozenset(ids) in done_sets:
            print(f"{p.name}: same students as an already-checked report; skipped (use --redo)")
            continue
        idx = df.index[df["student_id"].isin(ids)]
        if len(idx) == 0:
            print(f"WARN: no students from {p.name} found in {args.sta.name}; skipped")
            continue
        missing = set(ids) - {int(x) for x in df.loc[idx, "student_id"]}
        if missing:
            print(f"WARN: {p.name}: IDs not in {args.sta.name}: {sorted(missing)}")
        teams = df.loc[idx, "team_id"].unique()
        team_id = teams[0]
        if len(teams) > 1:
            print(f"WARN: {p.name} spans several teams {list(teams)}; using {team_id}")
        if wanted and team_id != wanted:
            continue
        if not args.redo and team_filled(df.loc[idx]):
            print(f"{team_id}: already filled in {args.sta.name}; skipped (use --redo)")
            continue
        queue.append((team_id, idx, p))

    queue.sort(key=lambda t: t[0])
    print(f"{len(queue)} team(s) to review. Type :skip to skip a team, :q to save and quit.")

    done = 0
    try:
        for n, (team_id, idx, p) in enumerate(queue, 1):
            print(f"\n[{n}/{len(queue)}]")
            try:
                saved = review_team(df, team_id, idx, p, args)
            except Skip:
                print("Skipped.")
                continue
            if saved:
                save_sta(df, args.sta)  # save after every team
                done += 1
                if not args.no_rename and not is_done(p):
                    p.rename(p.with_name("checked " + p.name))
                print(f"Saved {team_id} -> {args.sta}")
    except Quit:
        print("\nQuitting.")
    finally:
        save_sta(df, args.sta)
        left = sum(1 for t in queue) - done
        print(f"Done: {done} saved, {left} not saved. Bug-finding marks (/10) left blank for later.")


if __name__ == "__main__":
    sys.exit(main())