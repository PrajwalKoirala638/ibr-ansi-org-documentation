import argparse  # Read command-line options such as --report and --categories.
import asyncio  # Run Playwright's asynchronous API.
import hashlib  # Fingerprint each saved file so duplicates can be recognised.
import json  # Store lists of links inside the database.
import logging  # Print detailed log lines to the console.
import os  # Set environment variables for Playwright's own debug output.
import re  # Use regular expressions to clean text and match URLs.
import shutil  # Move finished downloads into the PDFs folder.
import sqlite3  # Remember progress so no page or file is handled twice.
import subprocess  # Start Chromium as a separate program.
import sys  # Send log lines to the console (standard output).
import time  # Pause between attempts to connect to Chromium.
from datetime import datetime, timezone  # Timestamp database rows.
from pathlib import Path  # Work with folders and files in a cross-platform way.
from urllib.parse import urljoin  # Turn relative links into full URLs.

os.environ.setdefault("DEBUG", "pw:api")  # Ask Playwright to print every action it performs.

from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError  # Browser automation tools.

logging.basicConfig(  # Configure how log lines look.
    level=logging.DEBUG,  # Show everything, including debug details.
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(funcName)s:%(lineno)d | %(message)s",  # Time, level, code location, message.
    datefmt="%H:%M:%S",  # Short clock format.
    stream=sys.stdout,  # Print to the console.
)  # End of the logging configuration.
logger = logging.getLogger("ibr")  # The logger this script uses.

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

PORTAL_BASE_URL = "https://ibr.ansi.org/"  # Home page of the ANSI IBR portal.
PROFILE_FOLDER = Path("chrome_profile")  # Everything Chromium and SQLite need lives in this one folder.
DATABASE_PATH = PROFILE_FOLDER / "ibr_progress.db"  # SQLite file that remembers what is already done.
LEGACY_DATABASE_PATH = Path("ibr_progress.db")  # Old database location, moved into the profile folder if found.
RAW_DOWNLOAD_FOLDER = PROFILE_FOLDER / "downloads_in_progress"  # Chromium writes downloads here before we verify them.
OUTPUT_FOLDER = Path("PDFs")  # Folder that receives the verified files.
CHROMIUM_DEBUG_PORT = 9222  # Port that lets this script control Chromium.
CATEGORY_CODES = ["iec", "iso", "aham", "amca", "ampp", "asa", "aws", "bsi", "hfes", "isa", "mss", "nema"]  # The 12 ANSI-hosted categories.
PAUSE_BETWEEN_DOCUMENTS_SECONDS = 3  # Wait between documents to avoid hammering the server.
MAXIMUM_DOWNLOAD_ATTEMPTS = 3  # How many times to retry an incomplete download.
DOWNLOAD_START_TIMEOUT_SECONDS = 60  # How long to wait for a download to begin after clicking.
DOWNLOAD_STALL_TIMEOUT_SECONDS = 30  # A download that receives no new bytes for this long counts as stalled.
RETRY_DELAY_SECONDS = 5  # Base delay before retrying a failed download (grows with each attempt).

begun_download_queue = None  # Queue of download IDs reported by Chromium (created in main).
download_states = {}  # Progress of every download Chromium reports, keyed by download ID.
response_headers_by_url = {}  # Remembers the headers of every response, so file sizes can be compared.
chromium_process = None  # The Chromium program started by this script (if we started it).

REGISTRATION_FIELDS = {  # Registration values keyed by the form field IDs on Register.aspx.
    "#ContentPlaceHolder_body_tbemail": "prajwalkoirala638@gmail.com",  # Email address.
    "#ContentPlaceHolder_body_tbFirstName": "Prajwal",  # First name.
    "#ContentPlaceHolder_body_tbLastName": "Koirala",  # Last name.
    "#ContentPlaceHolder_body_tbOrganization": "C-O",  # Organization or company.
    "#ContentPlaceHolder_body_tbAddress": "72-09 Northern Blvd, Jackson Heights",  # Street address.
    "#ContentPlaceHolder_body_tbCity": "New York City",  # City.
    "#ContentPlaceHolder_body_tbZIP": "11372",  # ZIP code.
    "#ContentPlaceHolder_body_tbPhone": "+17185338434",  # Telephone number.
}  # End of the registration values.

NEW_DOCUMENT_COLUMNS = {  # Columns added after the first version of the database.
    "verdict": "TEXT",  # What the file check concluded (ok, fileopen-protected, ...).
    "content_type": "TEXT",  # Content type the server announced.
    "expected_size_bytes": "INTEGER",  # Size the server or browser announced.
    "first_bytes_hex": "TEXT",  # First bytes of the saved file, for diagnosis.
}  # End of the new columns.


# ----------------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------------


def get_current_timestamp():  # Return the current time as text.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # Example: 2026-10-30T12:00:00+00:00.


def open_database():  # Open (and create if needed) the progress database inside the profile folder.
    PROFILE_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the profile folder exists.
    if LEGACY_DATABASE_PATH.exists() and not DATABASE_PATH.exists():  # An old database sits next to the script.
        shutil.move(str(LEGACY_DATABASE_PATH), str(DATABASE_PATH))  # Move it into the profile folder.
        logger.info("Moved the old database into %s", DATABASE_PATH)  # Report.
    database = sqlite3.connect(DATABASE_PATH)  # Connect to the SQLite file.
    database.row_factory = sqlite3.Row  # Let us read columns by name.
    database.execute(  # Create the table that remembers scanned category pages.
        """CREATE TABLE IF NOT EXISTS pages (
            page_url TEXT PRIMARY KEY,  -- Address of the category page.
            category_code TEXT NOT NULL,  -- Category such as iec.
            pagination_links TEXT NOT NULL,  -- JSON list of the other pages found on this page.
            scanned_at TEXT NOT NULL  -- When the page was scanned.
        )"""  # End of the pages table definition.
    )  # End of the statement.
    database.execute(  # Create the table that remembers every standard and its download result.
        """CREATE TABLE IF NOT EXISTS documents (
            document_url TEXT PRIMARY KEY,  -- Address of the View Only page.
            category_code TEXT NOT NULL,  -- Category such as iec.
            page_url TEXT NOT NULL,  -- Category page that listed it.
            position INTEGER NOT NULL,  -- Order within the category.
            title TEXT NOT NULL,  -- Title shown on the portal.
            file_name_stem TEXT NOT NULL,  -- File name without extension.
            download_url TEXT,  -- Link behind the DOWNLOAD button.
            status TEXT NOT NULL,  -- pending, saved..., or a failure reason.
            file_path TEXT,  -- Where the file was saved.
            file_size_bytes INTEGER,  -- Size of the saved file.
            sha256 TEXT,  -- Fingerprint of the saved file.
            updated_at TEXT NOT NULL  -- Last change.
        )"""  # End of the documents table definition.
    )  # End of the statement.
    existing_columns = {row["name"] for row in database.execute("PRAGMA table_info(documents)")}  # Columns the table already has.
    for column_name, column_type in NEW_DOCUMENT_COLUMNS.items():  # Check each newer column.
        if column_name not in existing_columns:  # It is missing in an older database.
            database.execute(f"ALTER TABLE documents ADD COLUMN {column_name} {column_type}")  # Add it.
    database.commit()  # Save the table definitions.
    return database  # Hand the connection back.


def get_last_position(database, category_code):  # Find the highest document number used so far in a category.
    row = database.execute("SELECT COALESCE(MAX(position), 0) AS last_position FROM documents WHERE category_code = ?", (category_code,)).fetchone()  # Ask the database.
    return row["last_position"]  # Return the number.


def load_scanned_page(database, page_url):  # Return a previously scanned page from the database, or None.
    page_row = database.execute("SELECT * FROM pages WHERE page_url = ?", (page_url,)).fetchone()  # Look the page up.
    if page_row is None:  # We have never scanned it.
        return None  # Nothing to load.
    document_rows = database.execute("SELECT document_url, title, position, file_name_stem FROM documents WHERE page_url = ? ORDER BY position", (page_url,)).fetchall()  # Load its standards in order.
    standards = [{"url": row["document_url"], "title": row["title"], "position": row["position"], "file_name_stem": row["file_name_stem"]} for row in document_rows]  # Convert rows to plain dictionaries.
    return standards, set(json.loads(page_row["pagination_links"]))  # Give back standards and pagination links.


def save_scanned_page(database, category_code, page_url, standards, pagination_links):  # Remember a freshly scanned page and its standards.
    database.execute("INSERT OR REPLACE INTO pages (page_url, category_code, pagination_links, scanned_at) VALUES (?, ?, ?, ?)", (page_url, category_code, json.dumps(sorted(pagination_links)), get_current_timestamp()))  # Store the page.
    for standard in standards:  # Store every standard found on it.
        database.execute(  # Insert only if we have not seen this standard before.
            "INSERT OR IGNORE INTO documents (document_url, category_code, page_url, position, title, file_name_stem, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",  # SQL statement.
            (standard["url"], category_code, page_url, standard["position"], standard["title"], standard["file_name_stem"], get_current_timestamp()),  # Values.
        )  # End of the insert.
    database.commit()  # Write everything to disk now.


def is_document_finished(database, document_url):  # Has this standard been saved already (and the file still exists)?
    row = database.execute("SELECT status, file_path FROM documents WHERE document_url = ?", (document_url,)).fetchone()  # Look it up.
    if row is None or not row["status"].startswith("saved"):  # Never saved.
        return False  # Not finished.
    return row["file_path"] is not None and Path(row["file_path"]).exists()  # Finished only if the file is still on disk.


def build_result(status, download_url=None, file_path=None, verdict=None, content_type=None, expected_size_bytes=None, first_bytes_hex=None):  # Bundle everything we learned about one download attempt.
    return {  # A plain dictionary keeps the code easy to read.
        "status": status,  # Human-readable outcome.
        "download_url": download_url,  # Link behind the DOWNLOAD button.
        "file_path": file_path,  # Where the file was saved (or None).
        "verdict": verdict,  # Result of the file check.
        "content_type": content_type,  # Content type announced by the server.
        "expected_size_bytes": expected_size_bytes,  # Size announced by the server or browser.
        "first_bytes_hex": first_bytes_hex,  # First bytes of the file in hexadecimal.
    }  # End of the dictionary.


def record_document_result(database, document_url, result):  # Store the result of a download attempt.
    saved_file_path = result["file_path"]  # Where the file went, if anywhere.
    file_size_bytes = None  # Unknown unless a file was saved.
    sha256_fingerprint = None  # Unknown unless a file was saved.
    if saved_file_path is not None and Path(saved_file_path).exists():  # A file exists.
        file_bytes = Path(saved_file_path).read_bytes()  # Read it once.
        file_size_bytes = len(file_bytes)  # Measure it.
        sha256_fingerprint = hashlib.sha256(file_bytes).hexdigest()  # Fingerprint it.
        duplicate_row = database.execute("SELECT document_url, file_path FROM documents WHERE sha256 = ? AND document_url != ?", (sha256_fingerprint, document_url)).fetchone()  # Is the same content saved elsewhere?
        if duplicate_row is not None:  # Identical content already exists.
            logger.warning("Identical content already saved for %s (%s)", duplicate_row["document_url"], duplicate_row["file_path"])  # Warn.
    database.execute(  # Update the row for this standard.
        "UPDATE documents SET status = ?, download_url = ?, file_path = ?, file_size_bytes = ?, sha256 = ?, verdict = ?, content_type = ?, expected_size_bytes = ?, first_bytes_hex = ?, updated_at = ? WHERE document_url = ?",  # SQL statement.
        (result["status"], result["download_url"], str(saved_file_path) if saved_file_path else None, file_size_bytes, sha256_fingerprint, result["verdict"], result["content_type"], result["expected_size_bytes"], result["first_bytes_hex"], get_current_timestamp(), document_url),  # Values.
    )  # End of the update.
    database.commit()  # Write it to disk immediately so Ctrl+C loses nothing.


def print_report(database):  # Print a summary of everything in the database (replaces the old CSV file).
    logger.info("=== Report from %s ===", DATABASE_PATH)  # Heading.
    status_rows = database.execute("SELECT category_code, status, COUNT(*) AS document_count FROM documents GROUP BY category_code, status ORDER BY category_code, status").fetchall()  # Count by category and status.
    for row in status_rows:  # One line per group.
        logger.info("%-6s %-80s %d", row["category_code"], row["status"][:80], row["document_count"])  # Print it.
    problem_rows = database.execute("SELECT category_code, position, title, status FROM documents WHERE status NOT LIKE 'saved%' ORDER BY category_code, position").fetchall()  # Everything not saved.
    logger.info("Documents not saved yet: %d", len(problem_rows))  # Count them.
    for row in problem_rows:  # List them.
        logger.info("  %s #%03d %s -> %s", row["category_code"], row["position"], row["title"][:60], row["status"])  # Print it.


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------


def build_file_name_stem(text):  # Turn any text into a safe file name using only a-z, 0-9 and single underscores.
    lowercase_text = text.lower()  # Convert everything to lowercase.
    cleaned_text = re.sub(r"[^a-z0-9]+", "_", lowercase_text)  # Replace every run of other characters with one underscore.
    shortened_text = cleaned_text.strip("_")[:120]  # Remove edge underscores, then limit the length.
    return shortened_text.strip("_")  # Remove edge underscores again in case the cut left one.


def attach_debug_listeners(page):  # Log browser events for a tab.
    logger.debug("Attaching listeners to new tab %s", page)  # Note that a new tab appeared.

    def log_console_message(console_message):  # Called for every browser console message.
        logger.debug("[browser console.%s] %s", console_message.type, console_message.text)  # Print it.

    def log_page_error(page_error):  # Called for every uncaught JavaScript error.
        logger.debug("[browser page error] %s", page_error)  # Print it.

    def log_request(request):  # Called for every outgoing network request.
        logger.debug("[request] %s %s", request.method, request.url)  # Print it.

    def log_response(response):  # Called for every network response.
        logger.debug("[response] %s %s", response.status, response.url)  # Print it.
        response_headers_by_url[response.url] = response.headers  # Remember the headers for the file-size check.

    def log_failed_request(request):  # Called when a network request fails.
        logger.debug("[request failed] %s %s", request.url, request.failure)  # Print it.

    def log_navigation(frame):  # Called when any frame navigates.
        if frame == frame.page.main_frame:  # Only report the top-level page.
            logger.debug("[navigated] %s", frame.url)  # Print the new URL.

    def log_tab_closed(closed_page):  # Called when the tab closes.
        logger.debug("[tab closed] %s", closed_page.url)  # Print it.

    page.on("console", log_console_message)  # Listen for console messages.
    page.on("pageerror", log_page_error)  # Listen for JavaScript errors.
    page.on("request", log_request)  # Listen for requests.
    page.on("response", log_response)  # Listen for responses.
    page.on("requestfailed", log_failed_request)  # Listen for failed requests.
    page.on("framenavigated", log_navigation)  # Listen for navigations.
    page.on("close", log_tab_closed)  # Listen for the tab closing.


async def wait_until_page_is_stable(page):  # Wait until a tab has finished loading and redirecting.
    logger.debug("Waiting for the tab to settle at %s", page.url)  # Show where we are.
    await page.wait_for_load_state("load")  # Wait for the full page load.
    try:  # Network-idle can time out on pages with trackers.
        await page.wait_for_load_state("networkidle", timeout=10000)  # Wait until network traffic quiets down.
    except PlaywrightTimeoutError:  # The network never went quiet.
        logger.debug("Network never went idle at %s, continuing anyway", page.url)  # Note it and carry on.
    logger.debug("Tab is stable at %s", page.url)  # Show the final URL.


async def run_with_retry_on_navigation(page, action, *arguments):  # Re-run a page action if a navigation interrupted it.
    maximum_attempts = 4  # How many times to try.
    for attempt_number in range(1, maximum_attempts + 1):  # Count attempts starting at 1.
        try:  # Attempt the action.
            logger.debug("Attempt %d of %d for %s", attempt_number, maximum_attempts, getattr(action, "__name__", action))  # Show progress.
            return await action(*arguments)  # Return the result when it works.
        except PlaywrightError as error:  # Playwright reported an error.
            first_error_line = str(error).splitlines()[0]  # Keep only the first line of the message.
            logger.warning("Playwright error on attempt %d: %s", attempt_number, first_error_line)  # Show it.
            is_navigation_error = "Execution context was destroyed" in str(error)  # Is this the navigation error we can retry?
            if not is_navigation_error or attempt_number == maximum_attempts:  # Other errors, or the last attempt, are fatal.
                raise  # Pass the error upward.
            await wait_until_page_is_stable(page)  # Let the navigation finish, then try again.
    return None  # Never reached, but keeps code checkers happy.


# ----------------------------------------------------------------------------
# Portal gate pages
# ----------------------------------------------------------------------------


async def fill_registration_form(page):  # Fill in and submit the registration form.
    logger.info("Registration page detected, filling the form...")  # Tell the user what is happening.
    for field_selector, field_value in REGISTRATION_FIELDS.items():  # Go through every text field.
        logger.debug("Filling field %s", field_selector)  # Show which field (values are not logged).
        await page.fill(field_selector, field_value)  # Type the value into the field.
    logger.debug("Selecting country UNITED STATES")  # Country step.
    await page.select_option("#ContentPlaceHolder_body_Country", "US")  # The country is already United States, keep it.
    await page.wait_for_load_state("networkidle")  # A country change can reload the state list, wait for it.
    logger.debug("Selecting state New York")  # State step.
    await page.select_option("#ContentPlaceHolder_body_State", "NY")  # Choose New York.
    logger.debug("Clicking the Next button")  # Submit step.
    await page.click("#ContentPlaceHolder_body_imgBtnSubmi")  # Press Next.
    await wait_until_page_is_stable(page)  # Wait for the following page.


async def accept_license_agreement(page):  # Click Accept on the End User License Agreement page.
    logger.info("License agreement page detected, clicking Accept...")  # Tell the user what is happening.
    await page.click("#ContentPlaceHolder_body_ImageButton_Accept")  # Click the Accept button.
    await wait_until_page_is_stable(page)  # Wait for the following page.


# ----------------------------------------------------------------------------
# Category pages
# ----------------------------------------------------------------------------


def get_pagination_number(category_code, page_url):  # Turn iec.aspx into 0, iec1.aspx into 1, and so on.
    match = re.search(rf"/{category_code}(\d*)\.aspx", page_url, re.IGNORECASE)  # Find the digits after the category code.
    if match and match.group(1):  # There are digits in the URL.
        return int(match.group(1))  # Use them as the page number.
    return 0  # No digits means the first page.


async def read_category_page(context, category_code, page_url):  # Open a category page in a NEW tab and list its standards.
    category_tab = await context.new_page()  # Open a new tab for this category page.
    try:  # Make sure the tab is closed afterwards.
        logger.debug("Opening category page %s", page_url)  # Show the URL.
        await category_tab.goto(page_url, wait_until="domcontentloaded")  # Open the page.
        await wait_until_page_is_stable(category_tab)  # Wait for redirects or reloads to finish.
        standards = await run_with_retry_on_navigation(category_tab, category_tab.evaluate, """() => {  // Read the standards table.
            const anchors = document.querySelectorAll('a[href*="Fulfill.aspx?DocumentId="]');  // Every View Only link.
            return [...anchors].map(anchorElement => ({  // Build one record per link.
                url: anchorElement.href,  // The View Only page address.
                title: (anchorElement.closest('tr').querySelector('span.title') || {}).textContent || anchorElement.href  // The title in the same table row.
            }));  // End of the records.
        }""")  # End of the JavaScript.
        logger.debug("Found %d standards on %s", len(standards), page_url)  # Count them.
        all_links = await run_with_retry_on_navigation(category_tab, category_tab.eval_on_selector_all, "a[href]", "elements => elements.map(element => element.href)")  # Read every link on the page.
        pagination_links = {link.split("#")[0] for link in all_links if re.search(rf"/Standards/{category_code}\d*\.aspx$", link, re.IGNORECASE)}  # Keep this category's pages only.
        logger.debug("Pagination links: %s", sorted(pagination_links))  # Show them.
        return standards, pagination_links  # Give both lists back.
    finally:  # Always runs.
        await category_tab.close()  # Close the category tab.


async def get_page_contents(context, database, category_code, page_url):  # Return a page's standards and pagination links, using the database when possible.
    saved_page = load_scanned_page(database, page_url)  # Have we scanned this page before?
    if saved_page is not None:  # Yes.
        standards, pagination_links = saved_page  # Unpack the stored data.
        logger.info("Page already scanned earlier, loaded %d standards from the database: %s", len(standards), page_url)  # Report.
        return standards, pagination_links  # No need to visit the page again.
    standards, pagination_links = await read_category_page(context, category_code, page_url)  # Visit the page.
    next_position = get_last_position(database, category_code)  # Continue numbering where we stopped.
    for standard in standards:  # Number every standard.
        next_position += 1  # Next document number.
        standard["position"] = next_position  # Remember the number.
        standard["file_name_stem"] = build_file_name_stem(f"{category_code} {next_position:03d} {standard['title']}")  # Clean, ordered file name.
    save_scanned_page(database, category_code, page_url, standards, pagination_links)  # Remember the page in the database.
    return standards, pagination_links  # Give the results back.


# ----------------------------------------------------------------------------
# Downloading (Chromium writes the file itself, so nothing is streamed through Playwright)
# ----------------------------------------------------------------------------


def handle_download_will_begin(event):  # Chromium reports that a download is starting.
    download_id = event["guid"]  # Unique ID of this download.
    logger.debug("[chromium download begins] id=%s file=%s url=%s", download_id, event.get("suggestedFilename"), event.get("url"))  # Print it.
    download_states[download_id] = {  # Create a progress record.
        "url": event.get("url"),  # Where the file comes from.
        "suggested_filename": event.get("suggestedFilename") or "",  # Name suggested by the server.
        "state": "inProgress",  # Current state.
        "total_bytes": 0,  # Size announced by the server (0 when unknown).
        "received_bytes": 0,  # Bytes received so far.
        "finished_event": asyncio.Event(),  # Set when the download completes or is cancelled.
    }  # End of the record.
    begun_download_queue.put_nowait(download_id)  # Tell the waiting code a download started.


def handle_download_progress(event):  # Chromium reports progress of a download.
    download_state = download_states.get(event["guid"])  # Find our record.
    if download_state is None:  # A download we did not see start.
        return  # Ignore it.
    download_state["total_bytes"] = int(event.get("totalBytes", 0) or 0)  # Update the announced size.
    download_state["received_bytes"] = int(event.get("receivedBytes", 0) or 0)  # Update the received bytes.
    download_state["state"] = event["state"]  # Update the state.
    logger.debug("[chromium download progress] id=%s state=%s %d/%d bytes", event["guid"], event["state"], download_state["received_bytes"], download_state["total_bytes"])  # Print it.
    if event["state"] in ("completed", "canceled"):  # The download is over.
        download_state["finished_event"].set()  # Wake up the waiting code.


async def wait_for_download_to_finish(download_state):  # Wait until a download completes, is cancelled, or stalls.
    last_received_bytes = -1  # Bytes seen at the previous check.
    while True:  # Keep checking until a decision is made.
        try:  # Wait for the finished signal.
            await asyncio.wait_for(download_state["finished_event"].wait(), timeout=DOWNLOAD_STALL_TIMEOUT_SECONDS)  # Wait up to the stall limit.
            return download_state["state"]  # Report completed or canceled.
        except asyncio.TimeoutError:  # Nothing finished within the limit.
            if download_state["received_bytes"] == last_received_bytes:  # No new bytes arrived.
                return "stalled"  # Give up on this attempt.
            last_received_bytes = download_state["received_bytes"]  # Remember the progress and keep waiting.
            logger.info("Still downloading: %d bytes received so far", last_received_bytes)  # Progress.


def inspect_saved_file(file_path, expected_size_bytes):  # Check whether a saved file is complete and what kind of file it is.
    file_bytes = file_path.read_bytes()  # Read the whole file into memory.
    file_size_bytes = len(file_bytes)  # Size of the file in bytes.
    logger.debug("File check: %s size=%d expected=%s first_bytes=%r", file_path.name, file_size_bytes, expected_size_bytes, file_bytes[:8])  # Show what we found.
    if file_size_bytes == 0:  # Nothing was written.
        return "empty"  # Incomplete.
    if expected_size_bytes is not None and file_size_bytes != expected_size_bytes:  # The announced size differs.
        return "size-mismatch"  # The download was cut short or altered.
    if file_bytes.lstrip()[:1] == b"<":  # Starts like HTML or XML.
        return "html-page"  # A web page, not the document.
    if not file_bytes.startswith(b"%PDF"):  # Not a PDF at all.
        return "unknown-format"  # Keep it, but with its original extension.
    if b"%%EOF" not in file_bytes[-2048:]:  # A complete PDF ends with %%EOF.
        return "truncated"  # The end of the file is missing.
    if b"FOPN_" in file_bytes:  # FileOpen's encryption marker is inside the PDF.
        return "fileopen-protected"  # Complete, but locked by the publisher's FileOpen protection.
    if b"/Encrypt" in file_bytes:  # Some other encryption is present.
        return "encrypted-pdf"  # Complete, but encrypted.
    return "ok"  # A complete, ordinary PDF.


def choose_file_extension(verdict, suggested_file_name):  # Pick the right extension for a saved file.
    if verdict in ("ok", "fileopen-protected", "encrypted-pdf"):  # All of these are PDF files.
        return ".pdf"  # Use the PDF extension.
    suggested_extension = Path(suggested_file_name).suffix.lower()  # The extension the server suggested.
    return suggested_extension or ".bin"  # Use it, or a neutral fallback.


async def download_one_standard(context, standard):  # Open one View Only page in a NEW tab and save what the portal delivers.
    file_name_stem = standard["file_name_stem"]  # Name to use for the file.
    logger.debug("Fetching %s -> %s", standard["url"], file_name_stem)  # Show the target.
    existing_files = [path for path in OUTPUT_FOLDER.glob(f"{file_name_stem}.*")]  # Files with this name already on disk.
    if existing_files:  # The file is already on disk.
        return build_result("saved (file already existed)", file_path=existing_files[0], verdict="existing")  # Reuse it.
    document_tab = await context.new_page()  # Open a new tab for this document.
    try:  # Make sure the tab is closed afterwards.
        await document_tab.goto(standard["url"], wait_until="domcontentloaded")  # Open the View Only page.
        await wait_until_page_is_stable(document_tab)  # Wait for redirects to finish.
        for gate_step in range(1, 4):  # Allow up to three gate pages (registration, then license agreement).
            logger.debug("Gate check %d, current URL %s", gate_step, document_tab.url)  # Show where we are.
            if "Register.aspx" in document_tab.url:  # The site wants us to register.
                await fill_registration_form(document_tab)  # Fill the form and press Next.
            elif "EULA.aspx" in document_tab.url:  # The site shows the license agreement.
                await accept_license_agreement(document_tab)  # Click Accept.
            else:  # No gate page is left.
                break  # Stop checking.
        if "Fulfill.aspx" not in document_tab.url:  # After the gates we might be on another page.
            logger.debug("Not on the View Only page (%s), reopening it", document_tab.url)  # Explain.
            await document_tab.goto(standard["url"], wait_until="domcontentloaded")  # Go back to the View Only page.
            await wait_until_page_is_stable(document_tab)  # Wait for it to finish loading.
        download_link = document_tab.locator("#ContentPlaceHolder_body_hl_Download")  # The DOWNLOAD link.
        if await download_link.count() == 0:  # The link is missing.
            logger.warning("No DOWNLOAD link on %s", document_tab.url)  # Warn.
            return build_result("no download link")  # Report it.
        download_url = await download_link.get_attribute("href")  # Remember the link target.
        logger.debug("Download link: %s", download_url)  # Show it.
        last_status = "no file delivered"  # Default result.
        for attempt_number in range(1, MAXIMUM_DOWNLOAD_ATTEMPTS + 1):  # Retry incomplete downloads.
            logger.info("Download attempt %d of %d for %s", attempt_number, MAXIMUM_DOWNLOAD_ATTEMPTS, file_name_stem)  # Progress.
            if attempt_number > 1:  # This is a retry.
                await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt_number - 1))  # Wait a little longer each time.
            while not begun_download_queue.empty():  # Remove stale download events.
                begun_download_queue.get_nowait()  # Discard one.
            download_tab = None  # The new tab opened by the link.
            try:  # The link opens a new tab, so clicking it should produce one.
                async with context.expect_page(timeout=30000) as new_tab_info:  # Wait for the new tab.
                    await download_link.click()  # Click the DOWNLOAD link like a person would.
                download_tab = await new_tab_info.value  # Get the new tab.
                logger.debug("Download tab opened at %s", download_tab.url)  # Show it.
            except PlaywrightTimeoutError:  # No new tab appeared.
                logger.warning("The click opened no new tab, opening the link in one manually")  # Fallback.
                download_tab = await context.new_page()  # Open our own new tab.
                try:  # Going to a download URL raises an error that is normal.
                    await download_tab.goto(download_url)  # Open the link directly.
                except Exception as navigation_error:  # Ignore the "download is starting" error.
                    logger.debug("goto raised (often normal for downloads): %s", str(navigation_error).splitlines()[0])  # Note it.
            try:  # Wait for Chromium to report the start of a download.
                download_id = await asyncio.wait_for(begun_download_queue.get(), timeout=DOWNLOAD_START_TIMEOUT_SECONDS)  # Wait up to the limit.
            except asyncio.TimeoutError:  # Nothing started.
                logger.warning("No download started within %d seconds; tab URL: %s", DOWNLOAD_START_TIMEOUT_SECONDS, download_tab.url)  # Show where the tab ended up.
                last_status = "no file delivered (viewer page only)"  # Remember the result.
                if not download_tab.is_closed():  # The tab is still open.
                    await download_tab.close()  # Close it.
                continue  # Try again.
            download_state = download_states[download_id]  # The progress record for this download.
            final_state = await wait_for_download_to_finish(download_state)  # Wait until it ends.
            if not download_tab.is_closed():  # The download tab is still open.
                await download_tab.close()  # Close it now that the download is over.
            if final_state != "completed":  # The download did not finish.
                logger.warning("Download ended with state '%s' after %d bytes", final_state, download_state["received_bytes"])  # Report.
                last_status = f"download {final_state}"  # Remember it.
                continue  # Try again.
            raw_file_path = RAW_DOWNLOAD_FOLDER / download_id  # Chromium names the finished file after the download ID.
            if not raw_file_path.exists():  # The file is not where we expect.
                logger.warning("Chromium reported completion but %s is missing; folder holds: %s", raw_file_path, [path.name for path in RAW_DOWNLOAD_FOLDER.iterdir()])  # Show what is there.
                last_status = "download completed but file missing"  # Remember it.
                continue  # Try again.
            response_headers = response_headers_by_url.get(download_state["url"], {})  # Headers the server sent for this download.
            content_type = response_headers.get("content-type")  # Content type announced by the server.
            logger.debug("Download headers: content-type=%s content-length=%s content-disposition=%s content-encoding=%s", content_type, response_headers.get("content-length"), response_headers.get("content-disposition"), response_headers.get("content-encoding"))  # Show them.
            expected_size_bytes = download_state["total_bytes"] if download_state["total_bytes"] > 0 else None  # Prefer the size Chromium saw.
            if expected_size_bytes is None and response_headers.get("content-length", "").isdigit() and not response_headers.get("content-encoding"):  # Fall back to the header.
                expected_size_bytes = int(response_headers["content-length"])  # The size the server promised.
            if download_state["received_bytes"] and raw_file_path.stat().st_size != download_state["received_bytes"]:  # Chromium's count differs from the file.
                logger.warning("File size %d differs from Chromium's received count %d", raw_file_path.stat().st_size, download_state["received_bytes"])  # Warn.
            verdict = inspect_saved_file(raw_file_path, expected_size_bytes)  # Check the file.
            first_bytes_hex = raw_file_path.read_bytes()[:16].hex()  # Keep the first bytes for diagnosis.
            if verdict in ("empty", "truncated", "size-mismatch", "html-page"):  # The file is not usable.
                logger.warning("File verdict '%s', retrying", verdict)  # Warn.
                raw_file_path.unlink(missing_ok=True)  # Delete the bad file.
                last_status = f"incomplete ({verdict})"  # Remember it.
                continue  # Try again.
            final_path = OUTPUT_FOLDER / f"{file_name_stem}{choose_file_extension(verdict, download_state['suggested_filename'])}"  # Final name with the right extension.
            shutil.move(str(raw_file_path), str(final_path))  # Move the verified file into the PDFs folder.
            logger.info("Saved %s (%d bytes, %s)", final_path, final_path.stat().st_size, verdict)  # Report.
            if verdict == "ok":  # A verified ordinary PDF.
                status = "saved"  # Success.
            elif verdict in ("fileopen-protected", "encrypted-pdf"):  # Complete but locked.
                logger.warning("%s is complete but %s: ordinary PDF viewers cannot open it", final_path.name, verdict)  # Explain.
                status = f"saved (complete, {verdict}: needs the publisher's viewer plug-in)"  # Report.
            else:  # Something we do not recognise.
                status = "saved (unrecognized file type, kept as delivered)"  # Report.
            return build_result(status, download_url, final_path, verdict, content_type, expected_size_bytes, first_bytes_hex)  # Done.
        return build_result(last_status, download_url)  # Every attempt was used up.
    finally:  # Always runs.
        await document_tab.close()  # Close the document tab.


# ----------------------------------------------------------------------------
# Browser start-up and shutdown
# ----------------------------------------------------------------------------


def find_playwright_chromium(playwright):  # Locate the Chromium that Playwright installed.
    chromium_path = Path(playwright.chromium.executable_path)  # Playwright knows where its own Chromium lives.
    if not chromium_path.exists():  # It has not been installed yet.
        raise RuntimeError("Playwright's Chromium is missing. Run: playwright install chromium")  # Tell the user how to fix it.
    return str(chromium_path)  # Use this program.


async def connect_to_chromium(playwright):  # Connect to a running Playwright Chromium, or start one.
    global chromium_process  # We remember the program we start so Ctrl+C can close it.
    debugging_address = f"http://localhost:{CHROMIUM_DEBUG_PORT}"  # Where Chromium listens for control commands.
    try:  # First try an already running Chromium.
        browser = await playwright.chromium.connect_over_cdp(debugging_address)  # Connect to it.
        logger.info("Connected to the Chromium that is already running")  # Good news.
        return browser  # Use it.
    except PlaywrightError:  # Nothing is listening yet.
        logger.info("No Chromium is running yet, starting Playwright's Chromium")  # Explain.
    chromium_program = find_playwright_chromium(playwright)  # Always Playwright's own Chromium.
    logger.debug("Launching %s with profile %s", chromium_program, PROFILE_FOLDER.resolve())  # Show the details.
    chromium_process = subprocess.Popen(  # Start Chromium as an independent program.
        [chromium_program, f"--remote-debugging-port={CHROMIUM_DEBUG_PORT}", f"--user-data-dir={PROFILE_FOLDER.resolve()}", "--no-sandbox", "--no-first-run", "--no-default-browser-check", PORTAL_BASE_URL],  # Options and start page (no-sandbox matches Playwright's default).
        stdout=subprocess.DEVNULL,  # Hide Chromium's own output.
        stderr=subprocess.DEVNULL,  # Hide Chromium's own errors.
        start_new_session=True,  # Keep Chromium running if the script ends normally.
    )  # End of the launch command.
    for wait_attempt in range(1, 31):  # Wait up to 30 seconds for Chromium to be ready.
        time.sleep(1)  # Pause one second.
        try:  # Try to connect again.
            browser = await playwright.chromium.connect_over_cdp(debugging_address)  # Connect to the new Chromium.
            logger.info("Connected to the new Chromium after %d seconds", wait_attempt)  # Report.
            return browser  # Use it.
        except PlaywrightError:  # Chromium is not ready yet.
            logger.debug("Chromium not ready yet (attempt %d)", wait_attempt)  # Note it.
    raise RuntimeError(f"Could not connect to Chromium on port {CHROMIUM_DEBUG_PORT}")  # Give up with a clear message.


async def route_downloads_to_raw_folder(browser):  # Tell Chromium to save downloads itself, in our folder, and report progress.
    RAW_DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the folder exists.
    browser_session = await browser.new_browser_cdp_session()  # Open a low-level control channel (kept open while we work).
    browser_session.on("Browser.downloadWillBegin", handle_download_will_begin)  # Listen for download starts.
    browser_session.on("Browser.downloadProgress", handle_download_progress)  # Listen for download progress.
    await browser_session.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(RAW_DOWNLOAD_FOLDER.resolve()), "eventsEnabled": True})  # Files are named after their download ID.
    logger.info("Chromium saves downloads itself into %s", RAW_DOWNLOAD_FOLDER.resolve())  # Report.
    return browser_session  # Keep the session so the setting stays active.


async def close_chromium(browser_session):  # Close Chromium completely (used after Ctrl+C).
    logger.info("Closing Chromium...")  # Tell the user.
    try:  # Ask Chromium itself to quit, which closes every tab.
        await browser_session.send("Browser.close")  # Tell Chromium to close.
    except PlaywrightError as error:  # The connection drops as Chromium quits, which is expected.
        logger.debug("Close command finished with: %s", str(error).splitlines()[0])  # Note it.
    if chromium_process is not None:  # We also started this Chromium ourselves.
        try:  # Wait briefly for a clean exit.
            chromium_process.wait(timeout=10)  # Wait up to 10 seconds.
        except subprocess.TimeoutExpired:  # It did not exit in time.
            logger.warning("Chromium did not exit, terminating it")  # Warn.
            chromium_process.terminate()  # Force it to stop.


# ----------------------------------------------------------------------------
# Main program
# ----------------------------------------------------------------------------


async def process_all_categories(context, database, category_codes):  # Go through every category, page by page, document by document.
    for category_code in category_codes:  # Every chosen category, one by one.
        logger.info("=== Category: %s ===", category_code.upper())  # Progress.
        visited_pages = set()  # Pages of this category handled in this run.
        pending_pages = [urljoin(PORTAL_BASE_URL, f"Standards/{category_code}.aspx")]  # Start with page 1.
        while pending_pages:  # Continue until every page was processed.
            page_url = pending_pages.pop(0)  # Take the lowest-numbered pending page.
            if page_url in visited_pages:  # Already done in this run.
                continue  # Skip it.
            visited_pages.add(page_url)  # Mark it as done.
            standards, pagination_links = await get_page_contents(context, database, category_code, page_url)  # Load or scan the page.
            logger.info("--- Page %d: %d standards ---", get_pagination_number(category_code, page_url) + 1, len(standards))  # Progress.
            for pagination_link in pagination_links:  # Add pages discovered on this page.
                if pagination_link not in visited_pages and pagination_link not in pending_pages:  # Only new ones.
                    pending_pages.append(pagination_link)  # Schedule it.
            pending_pages.sort(key=lambda pending_url: get_pagination_number(category_code, pending_url))  # Keep pages in numeric order.
            logger.debug("Pages still pending: %s", pending_pages)  # Show them.
            for standard in standards:  # Download the first, then the second, and so on.
                if is_document_finished(database, standard["url"]):  # Already saved in an earlier run.
                    logger.info("[%03d] already saved, skipping: %s", standard["position"], standard["title"][:60])  # Report.
                    continue  # Do not download it twice.
                try:  # One failure should not stop the whole run.
                    result = await download_one_standard(context, standard)  # Download it in a new tab.
                except (asyncio.CancelledError, KeyboardInterrupt):  # Ctrl+C was pressed.
                    raise  # Let the shutdown code handle it.
                except Exception as unexpected_error:  # Catch anything unexpected.
                    logger.exception("Unexpected error on %s", standard["url"])  # Print the full traceback.
                    result = build_result(f"error: {unexpected_error}")  # Record the problem.
                record_document_result(database, standard["url"], result)  # Save the result immediately.
                logger.info("[%03d] %s: %s", standard["position"], standard["title"][:60], result["status"])  # Progress.
                await asyncio.sleep(PAUSE_BETWEEN_DOCUMENTS_SECONDS)  # Be polite to the server.


async def main(category_codes):  # Entry point of the downloader.
    global begun_download_queue  # We assign the module-level queue here.
    begun_download_queue = asyncio.Queue()  # Create the queue that collects download-start events.
    logger.info("Starting. Files: %s, profile and database: %s", OUTPUT_FOLDER.resolve(), PROFILE_FOLDER.resolve())  # Show where things go.
    OUTPUT_FOLDER.mkdir(exist_ok=True)  # Create the PDFs folder if it is missing.
    database = open_database()  # Open the progress database.
    try:  # Make sure the database is always saved and closed.
        async with async_playwright() as playwright:  # Start Playwright.
            browser = await connect_to_chromium(playwright)  # Get a Playwright Chromium.
            browser_session = await route_downloads_to_raw_folder(browser)  # Let Chromium write downloads itself.
            context = browser.contexts[0] if browser.contexts else await browser.new_context()  # Use Chromium's normal profile so cookies stay.
            for existing_tab in context.pages:  # Tabs that were already open.
                attach_debug_listeners(existing_tab)  # Log their events too (they are left open).
            context.on("page", attach_debug_listeners)  # Attach listeners to every future tab.
            try:  # Watch for Ctrl+C while working.
                await process_all_categories(context, database, category_codes)  # Do all the work.
                logger.info("All chosen categories done. Chromium is left open so your session stays registered.")  # Explain why we do not close it.
            except (asyncio.CancelledError, KeyboardInterrupt):  # Ctrl+C was pressed.
                logger.warning("Ctrl+C received: saving progress and closing Chromium")  # Tell the user.
                database.commit()  # Make sure everything is written to the database.
                await close_chromium(browser_session)  # Close Chromium and every tab.
    finally:  # Runs on normal exit and after Ctrl+C.
        database.commit()  # Save any pending changes.
        print_report(database)  # Show a summary in the console.
        database.close()  # Close the database.
        logger.info("Progress saved in %s. Run the script again to continue where it stopped.", DATABASE_PATH)  # Final message.


def read_command_line_options():  # Read the options given when starting the script.
    parser = argparse.ArgumentParser(description="Download the standards listed on the ANSI IBR portal.")  # Describe the program.
    parser.add_argument("--categories", nargs="+", choices=CATEGORY_CODES, default=CATEGORY_CODES, help="Only process these categories (default: all 12).")  # Optional category filter.
    parser.add_argument("--report", action="store_true", help="Print a summary from the database and exit.")  # Report-only mode.
    return parser.parse_args()  # Return the parsed options.


if __name__ == "__main__":  # Run only when this file is executed directly.
    command_line_options = read_command_line_options()  # Read the options.
    if command_line_options.report:  # The user only wants a report.
        report_database = open_database()  # Open the database.
        print_report(report_database)  # Print the summary.
        report_database.close()  # Close it.
        sys.exit(0)  # Nothing else to do.
    try:  # Catch the final Ctrl+C that asyncio re-raises.
        asyncio.run(main(command_line_options.categories))  # Start the program.
    except KeyboardInterrupt:  # Ctrl+C was pressed.
        logger.warning("Stopped by Ctrl+C")  # Say so, then exit quietly.
