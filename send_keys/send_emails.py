"""
Send each student their personalized lab_key via Gmail SMTP.

Setup:
  1. Generate a Gmail App Password:
     Google Account -> Security -> 2-Step Verification -> App Passwords
  2. Put GMAIL_ADDRESS and GMAIL_APP_PASSWORD in a .env file next to this script
     (or export them as environment variables).
  3. Put your CSV (columns: student_id,name,email,team_code,lab_key) next to this script.
  4. Run: python3 send_keys.py students.csv
     Dry run (no emails sent, writes preview.html): python3 send_keys.py students.csv --dry-run

Notes:
  - Sends one email at a time with a short delay to avoid Gmail rate limits.
  - Logs results to send_log.csv. Re-run with only the failed rows if needed.
"""

import csv
import html
import os
import smtplib
import ssl
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars still work

# ---- CONFIG ----
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "youraddress@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "your16charapppassword")
SUBJECT = "[CSE 4495 - STQA Assignment 1] Lab Credentials for System Testing Assignment"
BASE_URL = "https://stqa.mobaswir.live/api/stqa-test-wallet"
DOCUMENTATION = "All the details will be shared in a separate email."
SENDER_NAME = "Mobaswir"
DELAY_SECONDS = 2


def build_text_body(row):
    return f"""Dear {row['name']},

Here are your credentials for the CSE 4495 lab environment:

Student ID:  {row['student_id']}
Team Code:   {row['team_code']}
X-STQA-Key:  {row['lab_key']}
BASE_URL:    {BASE_URL}
Documentation: {DOCUMENTATION}

NOTE: DO NOT TAKE ANY SORT OF HELP FROM AI TO COMPLETE YOUR ASSIGNMENT.
 - Not to test
 - Not to plan tests
 - Not to write reports

Please keep this key confidential. Do not share it with other students,
not even with your team mates.

Best regards,
{SENDER_NAME}
"""


def build_html_body(row):
    e = lambda v: html.escape(str(v))
    rows = [
        ("Student ID", e(row["student_id"])),
        ("Team Code", e(row["team_code"])),
        ("BASE_URL", f"<code>{e(BASE_URL)}</code>"),
        ("Documentation", e(DOCUMENTATION)),
    ]
    cell = "padding:8px 12px;border:1px solid #ccc"
    trs = "".join(
        f"<tr><td style='{cell};background:#f5f5f5;font-weight:bold'>{k}</td>"
        f"<td style='{cell}'>{v}</td></tr>"
        for k, v in rows
    )
    # Full-width rows: key header, then the key itself
    trs += (
        f"<tr><td colspan='2' style='{cell};background:#f5f5f5;font-weight:bold;"
        f"text-align:center'>X-STQA-Key</td></tr>"
        f"<tr><td colspan='2' style='{cell};text-align:center'>"
        f"<code style='font-size:14px'>{e(row['lab_key'])}</code></td></tr>"
    )
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Dear {e(row['name'])},</p>
<p>Here are your credentials for the CSE 4495 lab environment:</p>
<table style="border-collapse:collapse">{trs}</table>
<p style="margin-top:16px;padding:10px 12px;border-left:4px solid #c0392b;background:#fdecea">
<b>NOTE: DO NOT TAKE ANY SORT OF HELP FROM AI TO COMPLETE YOUR ASSIGNMENT.</b><br>
&ndash; Not to test<br>&ndash; Not to plan tests<br>&ndash; Not to write reports</p>

<p style="margin-top:16px;padding:10px 12px;border-left:4px solid #c0392b;background:#fdecea">
<b>PLEASE KEEP THIS KEY CONFIDENTIAL. DO NOT SHARE IT WITH OTHER STUDENTS,
NOT EVEN WITH YOUR TEAM MATES.</b></p>
<p>Best regards,<br>{e(SENDER_NAME)}</p>
</body></html>"""


def build_message(to_addr, row):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SENDER_NAME} <{GMAIL_ADDRESS}>"
    msg["To"] = to_addr
    msg["Subject"] = SUBJECT
    # Plain text first, HTML last: clients show the last part they support.
    msg.attach(MIMEText(build_text_body(row), "plain", "utf-8"))
    msg.attach(MIMEText(build_html_body(row), "html", "utf-8"))
    return msg


def connect():
    smtp = smtplib.SMTP("smtp.gmail.com", 587)
    smtp.starttls(context=ssl.create_default_context())
    smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return smtp


def main(csv_path, dry_run=False):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("ERROR: CSV has no rows.")
        sys.exit(1)

    required = {"student_id", "name", "email", "team_code", "lab_key"}
    missing = required - set(rows[0].keys())
    if missing:
        print(f"ERROR: CSV is missing columns: {missing}")
        sys.exit(1)

    if dry_run:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(build_html_body(rows[0]))
        print(f"Dry run: {len(rows)} students loaded. Wrote preview.html for {rows[0]['email']}.")
        return

    print(f"Loaded {len(rows)} students. Connecting to Gmail...")
    smtp = connect()
    results = []

    try:
        for i, row in enumerate(rows, 1):
            to_addr = row["email"].strip()
            try:
                try:
                    smtp.sendmail(GMAIL_ADDRESS, to_addr, build_message(to_addr, row).as_string())
                except smtplib.SMTPServerDisconnected:
                    smtp = connect()  # reconnect once if Gmail dropped the session
                    smtp.sendmail(GMAIL_ADDRESS, to_addr, build_message(to_addr, row).as_string())
                print(f"[{i}/{len(rows)}] Sent to {to_addr}")
                results.append({**row, "status": "sent", "error": ""})
            except Exception as ex:
                print(f"[{i}/{len(rows)}] FAILED for {to_addr}: {ex}")
                results.append({**row, "status": "failed", "error": str(ex)})
            time.sleep(DELAY_SECONDS)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
        if results:
            with open("send_log.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
                w.writeheader()
                w.writerows(results)

    sent = sum(r["status"] == "sent" for r in results)
    print(f"\nDone. Sent: {sent}, Failed: {len(results) - sent}")
    print("Full log written to send_log.csv")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1:
        print("Usage: python3 send_keys.py students.csv [--dry-run]")
        sys.exit(1)
    main(args[0], dry_run="--dry-run" in sys.argv)