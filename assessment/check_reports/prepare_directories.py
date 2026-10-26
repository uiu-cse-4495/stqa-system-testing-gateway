#!/usr/bin/env python3
"""Download report files and Drive folders from a CSV and organize them by team."""

from __future__ import annotations

import argparse
import codecs
import csv
import html
import http.cookiejar
import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, build_opener


DRIVE_ID_PATTERN = re.compile(r"[-\w]{20,}")
CONFIRM_PATTERN = re.compile(
    r"(?:confirm=|name=[\"']confirm[\"'][^>]*value=[\"'])([0-9A-Za-z_-]+)"
)
DRIVE_METADATA_PATTERN = re.compile(r"window\['_DRIVE_ivd'\]\s*=\s*'([^']*)'")


def drive_file_id(url: str) -> str | None:
    """Extract a Google Drive file ID from common Drive URL formats."""
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and DRIVE_ID_PATTERN.fullmatch(query_id):
        return query_id

    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if match and DRIVE_ID_PATTERN.fullmatch(match.group(1)):
        return match.group(1)

    return None


def drive_folder_id(url: str) -> str | None:
    """Extract a Google Drive folder ID from common folder URL formats."""
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and DRIVE_ID_PATTERN.fullmatch(query_id):
        return query_id

    match = re.search(r"/folders/([^/?]+)", parsed.path)
    if match and DRIVE_ID_PATTERN.fullmatch(match.group(1)):
        return match.group(1)

    return None


def download_drive_content(
    url: str,
    drive_id: str,
    cookies: http.cookiejar.CookieJar | None = None,
    request_url: str | None = None,
) -> bytes:
    """Download Drive content, including files requiring a confirmation token."""
    opener = build_opener(
        http.cookiejar.HTTPCookieProcessor(cookies)
    ) if cookies is not None else build_opener()
    download_url = request_url or (
        "https://drive.google.com/uc?"
        + urlencode({"id": drive_id, "export": "download"})
    )
    response = opener.open(Request(download_url, headers={"User-Agent": "Mozilla/5.0"}))
    content = response.read()

    if urlparse(response.geturl()).netloc.endswith("google.com") and (
        "accounts.google.com" in response.geturl()
    ):
        raise RuntimeError(
            "Google Drive requires authentication; make the file public or use --cookies"
        )

    if "text/html" in response.headers.get_content_type():
        page = content.decode("utf-8", errors="replace")
        token_match = CONFIRM_PATTERN.search(html.unescape(page))
        if not token_match:
            raise RuntimeError(
                "Google Drive returned an access page without a download token; "
                "make the file accessible to the account used for --cookies"
            )
        download_url += "&" + urlencode({"confirm": token_match.group(1)})
        response = opener.open(
            Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
        )
        content = response.read()
        if "text/html" in response.headers.get_content_type():
            raise RuntimeError("Google Drive returned HTML instead of the requested file")

    return content


def drive_opener(cookies: http.cookiejar.CookieJar | None):
    return (
        build_opener(http.cookiejar.HTTPCookieProcessor(cookies))
        if cookies is not None
        else build_opener()
    )


def drive_folder_entries(
    url: str, cookies: http.cookiejar.CookieJar | None = None
) -> list[tuple[str, str, str]]:
    """Read immediate file entries from a shared Drive folder page."""
    response = drive_opener(cookies).open(
        Request(url, headers={"User-Agent": "Mozilla/5.0"})
    )
    page = response.read().decode("utf-8", errors="replace")
    metadata_match = DRIVE_METADATA_PATTERN.search(page)
    if not metadata_match:
        raise RuntimeError("Google Drive folder metadata was not found")

    try:
        metadata = json.loads(codecs.decode(metadata_match.group(1), "unicode_escape"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Google Drive folder metadata could not be parsed") from error

    entries = []
    for group in metadata:
        if not isinstance(group, list):
            continue
        for entry in group:
            if (
                isinstance(entry, list)
                and len(entry) >= 4
                and isinstance(entry[0], str)
                and isinstance(entry[2], str)
                and isinstance(entry[3], str)
            ):
                entries.append((entry[0], entry[2].strip(), entry[3]))
    return entries


def drive_export_url(file_id: str, mime_type: str) -> str | None:
    """Return an export URL for native Google Workspace files."""
    export_formats = {
        "application/vnd.google-apps.document": ("docs.google.com", "txt"),
        "application/vnd.google-apps.spreadsheet": ("docs.google.com", "xlsx"),
        "application/vnd.google-apps.presentation": ("docs.google.com", "pptx"),
    }
    target = export_formats.get(mime_type)
    if not target:
        return None
    host, extension = target
    path = "document" if mime_type.endswith(".document") else (
        "spreadsheets" if mime_type.endswith(".spreadsheet") else "presentation"
    )
    return f"https://{host}/{path}/d/{file_id}/export?format={extension}"


def download_drive_folder_entries(
    url: str, destination: Path, cookies: http.cookiejar.CookieJar | None = None
) -> None:
    """Download shared-folder children when Google cannot create a folder ZIP."""
    destination.mkdir(parents=True, exist_ok=True)
    used_names: dict[str, int] = {}
    for file_id, name, mime_type in drive_folder_entries(url, cookies):
        if mime_type == "application/vnd.google-apps.folder":
            continue
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or file_id
        count = used_names.get(filename, 0) + 1
        used_names[filename] = count
        if count > 1:
            path = Path(filename)
            filename = f"{path.stem}_{count}{path.suffix}"
        target = destination / filename
        if target.exists():
            continue
        native_export = drive_export_url(file_id, mime_type)
        if native_export:
            filename = f"{filename}.txt" if mime_type.endswith(".document") else filename
            target = destination / filename
            if target.exists():
                continue
            content = download_drive_content(
                native_export, file_id, cookies, request_url=native_export
            )
        else:
            content = download_drive_content(
                f"https://drive.google.com/file/d/{file_id}/view", file_id, cookies
            )
        target.write_bytes(content)


def download_drive_file(
    url: str, destination: Path, cookies: http.cookiejar.CookieJar | None = None
) -> None:
    """Download a Drive file to a local path."""
    if destination.exists():
        return
    file_id = drive_file_id(url)
    if not file_id:
        raise ValueError(f"not a Google Drive file URL: {url}")
    content = download_drive_content(url, file_id, cookies)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(destination)


def extract_drive_folder(
    url: str, destination: Path, cookies: http.cookiejar.CookieJar | None = None
) -> None:
    """Download a Drive folder archive and extract it without allowing path traversal."""
    if destination.exists():
        return
    folder_id = drive_folder_id(url)
    if not folder_id:
        raise ValueError(f"not a Google Drive folder URL: {url}")

    try:
        content = download_drive_content(url, folder_id, cookies)
    except HTTPError as error:
        if error.code != 500:
            raise
        download_drive_folder_entries(url, destination, cookies)
        return
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if destination_root != member_path and destination_root not in member_path.parents:
                raise RuntimeError(
                    f"Google Drive folder contains an unsafe path: {member.filename}"
                )
        archive.extractall(destination)


def team_directory_name(row: dict[str, str], row_number: int) -> str:
    ids = []
    for index in range(1, 5):
        value = row.get(f"Student ID {index}", "").strip()
        value = re.sub(r"^\s*ID\s*:\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"[^A-Za-z0-9_.-]", "", value)
        if value:
            ids.append(value)
    return "_".join(ids) or f"row_{row_number}"


def process_csv(
    csv_path: Path,
    output_root: Path,
    cookies: http.cookiejar.CookieJar | None = None,
) -> int:
    failures = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        rows = csv.DictReader(csv_file)
        required_columns = {"Report", "Student ID 1"}
        missing = required_columns - set(rows.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row_number, row in enumerate(rows, start=2):
            team_path = output_root / team_directory_name(row, row_number)
            team_path.mkdir(parents=True, exist_ok=True)
            files = [("report.md", row.get("Report", ""))]
            collection_number = 1
            for column in (rows.fieldnames or []):
                if column.startswith("Collection") and row.get(column, "").strip():
                    files.append(
                        (f"collection{collection_number}.json", row[column].strip())
                    )
                    collection_number += 1

            for filename, url in files:
                if not url.strip():
                    continue
                try:
                    destination = team_path / filename
                    already_exists = destination.exists()
                    download_drive_file(url.strip(), team_path / filename, cookies)
                    status = "Skipped" if already_exists else "Downloaded"
                    print(f"{status} {team_path.name}/{filename}")
                except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
                    failures += 1
                    print(
                        f"Failed to download {team_path.name}/{filename}: {error}",
                        file=sys.stderr,
                    )

            drive_link = row.get("Drive link", "").strip()
            if drive_link:
                try:
                    destination = team_path / "drive_folder"
                    already_exists = destination.exists()
                    extract_drive_folder(
                        drive_link, destination, cookies
                    )
                    status = "Skipped" if already_exists else "Downloaded"
                    print(f"{status} {team_path.name}/drive_folder")
                except (
                    HTTPError,
                    URLError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    zipfile.BadZipFile,
                ) as error:
                    failures += 1
                    print(
                        f"Failed to download {team_path.name}/drive_folder: {error}",
                        file=sys.stderr,
                    )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create per-team directories and download reports, collections, "
            "and Drive folders."
        )
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "262" / "262.csv",
        help="CSV file to process (default: check_reports/262/262.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: <CSV directory>/per_team)",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        help="Netscape-format Google cookies file for private Drive files",
    )
    args = parser.parse_args()
    output_root = args.output or args.csv_path.parent / "per_team"
    cookies = None
    if args.cookies:
        cookies = http.cookiejar.MozillaCookieJar(str(args.cookies))
        try:
            cookies.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as error:
            parser.error(f"could not load cookies file: {error}")

    try:
        failures = process_csv(args.csv_path, output_root, cookies)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
