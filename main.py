# ANSI IBR portal downloader: everything lives in this one file.
# Run it with:  python3 main.py
# Needs:        pip install playwright   and   playwright install chromium
import asyncio  # Run the asynchronous program, queues, events and timeouts.
import hashlib  # Fingerprint saved files so duplicates can be noticed.
import json  # Store lists of links inside one database column.
import logging  # Print detailed log lines to the console.
import re  # Regular expressions clean up text and match addresses.
import sqlite3  # Python's built-in SQLite support.
import subprocess  # Start Chromium as a separate program.
import sys  # Console output.
import time  # Measure how long the run takes.
from collections import (
    OrderedDict,
)  # A dictionary that remembers insertion order, used as a small cache.
from dataclasses import dataclass, field  # Tidy containers for related values.
from datetime import datetime, timezone  # Timestamps for database rows.
from enum import Enum  # Readable names for the possible file verdicts.
from pathlib import Path  # Work with folders and files in a cross-platform way.
from urllib.parse import urljoin  # Turn relative links into full addresses.

from playwright.async_api import Error as PlaywrightError  # Generic Playwright error.
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)  # Playwright's timeout error.
from playwright.async_api import async_playwright  # Starts Playwright.

logger = logging.getLogger("ibr")  # The one logger used by the whole file.


# ============================================================================
# Settings
# ============================================================================

PORTAL_BASE_URL = "https://ibr.ansi.org/"  # Home page of the ANSI IBR portal.
PROFILE_FOLDER = Path(
    "chrome_profile"
)  # Everything Chromium and SQLite need lives in this one folder.
STATE_DATABASE_PATH = (
    PROFILE_FOLDER / "state.db"
)  # SQLite file that remembers what is already done.
OUTPUT_FOLDER = Path(
    "PDFs"
)  # Chromium downloads straight into this folder, and verified files stay here.
PARTIAL_FILE_SUFFIX = ".part"  # A file ending in .part is downloaded but not verified yet; the suffix is removed once it passes.
LEFTOVER_DOWNLOAD_NAME_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.crdownload)?$",
    re.IGNORECASE,
)  # Names Chromium uses for unfinished downloads.
CHROMIUM_DEBUG_PORT = 9222  # Port that lets this program control Chromium.
CATEGORY_CODES = [
    "iec",
    "iso",
    "aham",
    "amca",
    "ampp",
    "asa",
    "aws",
    "bsi",
    "hfes",
    "isa",
    "mss",
    "nema",
]  # The 12 ANSI-hosted categories.
PAUSE_BETWEEN_DOCUMENTS_SECONDS = (
    3  # Wait between documents to avoid hammering the server.
)
MAXIMUM_DOWNLOAD_ATTEMPTS = 3  # How many times to retry an incomplete download.
MAXIMUM_PAGE_SCAN_ATTEMPTS = 3  # How many times to retry reading a category page.
MAXIMUM_CONSECUTIVE_FAILURES = (
    8  # Stop politely after this many failed standards in a row.
)
DOWNLOAD_START_TIMEOUT_SECONDS = (
    60  # How long to wait for a download to begin after clicking.
)
DOWNLOAD_STALL_TIMEOUT_SECONDS = (
    30  # A download that receives no new bytes for this long counts as stalled.
)
RETRY_DELAY_SECONDS = 5  # Base delay before retrying (grows with each attempt).
MAXIMUM_REMEMBERED_RESPONSES = (
    500  # How many recent response headers are kept in memory.
)
COOKIE_BANNER_ALLOW_ALL_SELECTOR = "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, button:has-text('Allow all cookies')"  # The "Allow all cookies" button of the Cookiebot banner.
COOKIE_BANNER_CONTAINER_SELECTOR = "#CybotCookiebotDialog"  # The whole cookie banner, used to wait until it disappears.


@dataclass(frozen=True)  # Frozen means the values cannot change after creation.
class RegistrationDetails:  # Everything the portal's registration form asks for.
    email_address: str  # Email address.
    first_name: str  # First name.
    last_name: str  # Last name.
    organization: str  # Organization or company.
    street_address: str  # Street address.
    city: str  # City.
    state_code: str  # Two-letter US state code, for example NY.
    zip_code: str  # ZIP code.
    telephone_number: str  # Telephone number.
    country_code: str = "US"  # Two-letter country code, United States by default.

    # Map the registration form's text boxes to the text that belongs in each one.
    def to_text_field_values(self):  # No inputs besides the object itself.
        return {  # Build the dictionary of selector -> value.
            "#ContentPlaceHolder_body_tbemail": self.email_address,  # Email box.
            "#ContentPlaceHolder_body_tbFirstName": self.first_name,  # First name box.
            "#ContentPlaceHolder_body_tbLastName": self.last_name,  # Last name box.
            "#ContentPlaceHolder_body_tbOrganization": self.organization,  # Organization box.
            "#ContentPlaceHolder_body_tbAddress": self.street_address,  # Address box.
            "#ContentPlaceHolder_body_tbCity": self.city,  # City box.
            "#ContentPlaceHolder_body_tbZIP": self.zip_code,  # ZIP box.
            "#ContentPlaceHolder_body_tbPhone": self.telephone_number,  # Telephone box.
        }  # End of the dictionary.


REGISTRATION_DETAILS = (
    RegistrationDetails(  # The details that get typed into the registration form.
        email_address="prajwalkoirala638@gmail.com",  # Email address.
        first_name="Prajwal",  # First name.
        last_name="Koirala",  # Last name.
        organization="C-O",  # Organization or company.
        street_address="72-09 Northern Blvd, Jackson Heights",  # Street address.
        city="New York City",  # City.
        state_code="NY",  # New York.
        zip_code="11372",  # ZIP code.
        telephone_number="+17185338434",  # Telephone number.
    )
)  # End of the registration details.


# ============================================================================
# Logging
# ============================================================================


# Call this once at start-up to decide how log lines look.
def configure_logging():  # No inputs.
    logging.basicConfig(  # Configure the root logger.
        level=logging.DEBUG,  # Show everything, including debug details.
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(funcName)s:%(lineno)d | %(message)s",  # Time, level, function, line, message.
        datefmt="%H:%M:%S",  # Short clock format.
        stream=sys.stdout,  # Print to the console.
    )  # End of the logging configuration.


# ============================================================================
# Text and time helpers
# ============================================================================


# Turn any text into a safe file name using only a-z, 0-9 and single underscores.
def build_file_name_stem(text):  # The text is usually a standard's title.
    lowercase_text = text.lower()  # Convert everything to lowercase.
    cleaned_text = re.sub(
        r"[^a-z0-9]+", "_", lowercase_text
    )  # Replace every run of other characters with one underscore.
    shortened_text = cleaned_text.strip("_")[
        :120
    ]  # Remove edge underscores, then limit the length.
    return shortened_text.strip(
        "_"
    )  # Remove edge underscores again in case the cut left one.


# Return the current time as text, for example 2026-10-30T12:00:00+00:00.
def get_current_timestamp():  # No inputs.
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )  # Current UTC time without fractions of a second.


# Show the first bytes of a file as hexadecimal text, which helps diagnose odd files.
def format_first_bytes_as_hex(
    file_bytes, byte_count=16
):  # By default look at 16 bytes.
    return file_bytes[:byte_count].hex()  # Convert them to hex text.


# Turn a number of seconds into text such as 1h 02m 03s.
def format_duration(total_seconds):  # The duration in seconds.
    hours, remainder = divmod(int(total_seconds), 3600)  # Split off whole hours.
    minutes, seconds = divmod(remainder, 60)  # Split the rest into minutes and seconds.
    return f"{hours}h {minutes:02d}m {seconds:02d}s"  # Assemble the text.


# ============================================================================
# Data containers
# ============================================================================


@dataclass  # A standard listed on a category page.
class Standard:  # One document offered by the portal.
    url: str  # Address of the View Only page.
    title: str  # Title shown on the portal.
    position: int = 0  # Order within its category (assigned when first scanned).
    file_name_stem: str = (
        ""  # File name without extension (assigned when first scanned).
    )


@dataclass  # Everything we learned from one download attempt.
class DownloadResult:  # Stored in the database after each standard.
    status: str  # Human-readable outcome, for example "saved".
    download_url: str = None  # Link behind the DOWNLOAD button.
    file_path: object = (
        None  # Where the verified file was saved (None if nothing was saved).
    )
    verdict: str = None  # Result of the file check, for example "ok".
    content_type: str = None  # Content type announced by the server.
    expected_size_bytes: int = None  # Size announced by the browser or server.
    file_size_bytes: int = None  # Size of the saved file.
    sha256: str = None  # Fingerprint of the saved file.
    first_bytes_hex: str = None  # First bytes of the file in hexadecimal.

    # A download counts as successful only when a verified file was saved.
    def is_saved(self):  # No inputs besides the object itself.
        return self.file_path is not None  # True when a file exists.


@dataclass  # Counters for one run of the program.
class RunStatistics:  # Printed at the end of the run.
    saved_count: int = 0  # Standards saved during this run.
    skipped_count: int = 0  # Standards skipped because they were already saved.
    failed_count: int = 0  # Standards that could not be saved.
    consecutive_failures: int = (
        0  # Failures in a row, used to stop politely if the portal blocks us.
    )
    started_at: float = field(default_factory=time.monotonic)  # When the run began.

    # Text summary shown at the end of the run.
    def describe(self):  # No inputs besides the object itself.
        elapsed_text = format_duration(
            time.monotonic() - self.started_at
        )  # How long the run took.
        return f"saved={self.saved_count} skipped={self.skipped_count} failed={self.failed_count} elapsed={elapsed_text}"  # Assemble the text.


class TooManyFailuresError(Exception):  # Raised when too many standards fail in a row.
    pass  # Nothing to add: the message says everything.


# ============================================================================
# State database (SQLite)
# ============================================================================


class StateDatabase:  # Wraps the SQLite file so the rest of the code never writes SQL.
    # Remember where the file lives; the file itself is opened later.
    def __init__(self, database_path):  # The path comes from settings.
        self.database_path = database_path  # Location of the SQLite file.
        self.connection = None  # Opened by open().

    # Called automatically at the start of a "with StateDatabase(...)" block.
    def __enter__(self):  # No inputs.
        self.open()  # Open the database.
        return self  # Give the caller this object.

    # Called automatically at the end of the "with" block, even after errors or Ctrl+C.
    def __exit__(
        self, exception_type, exception_value, traceback_object
    ):  # Python passes in any error details.
        self.close()  # Always close the database.
        return False  # Do not hide any error.

    # Open the database, creating folders, tables and indexes when needed.
    def open(self):  # No inputs.
        self.database_path.parent.mkdir(
            parents=True, exist_ok=True
        )  # Make sure the folder exists.
        self.connection = sqlite3.connect(
            self.database_path
        )  # Connect to the SQLite file.
        self.connection.row_factory = sqlite3.Row  # Let us read columns by name.
        self.create_tables()  # Create tables and indexes that do not exist yet.
        logger.info("State database ready: %s", self.database_path)  # Report.

    # Close the connection if it is open.
    def close(self):  # No inputs.
        if self.connection is not None:  # It is open.
            self.connection.commit()  # Save anything still pending.
            self.connection.close()  # Close it.
            self.connection = None  # Forget it.

    # Create the tables and indexes if they are missing.
    def create_tables(self):  # No inputs.
        assert (
            self.connection is not None
        ), "The database must be opened first"  # Catch programming mistakes early.
        with self.connection:  # Commits automatically when the block ends.
            self.connection.execute(  # Table of category pages we have scanned.
                """CREATE TABLE IF NOT EXISTS pages (  -- Remembers scanned category pages.
                    page_url TEXT PRIMARY KEY,  -- Address of the category page.
                    category_code TEXT NOT NULL,  -- Category such as iec.
                    pagination_links TEXT NOT NULL,  -- JSON list of the other pages found on this page.
                    scanned_at TEXT NOT NULL  -- When the page was scanned.
                )"""  # End of the pages table definition.
            )  # End of the statement.
            self.connection.execute(  # Table of standards and their download results.
                """CREATE TABLE IF NOT EXISTS documents (  -- Remembers every standard and its result.
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
                    verdict TEXT,  -- What the file check concluded.
                    content_type TEXT,  -- Content type the server announced.
                    expected_size_bytes INTEGER,  -- Size the server or browser announced.
                    first_bytes_hex TEXT,  -- First bytes of the saved file, for diagnosis.
                    updated_at TEXT NOT NULL  -- Last change.
                )"""  # End of the documents table definition.
            )  # End of the statement.
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS index_documents_page ON documents (page_url)"
            )  # Speeds up loading a page's standards.
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS index_documents_sha256 ON documents (sha256)"
            )  # Speeds up duplicate detection.

    # Find the highest document number already used in a category.
    def get_last_position(self, category_code):  # The category is something like "iec".
        row = self.connection.execute(
            "SELECT COALESCE(MAX(position), 0) AS last_position FROM documents WHERE category_code = ?",
            (category_code,),
        ).fetchone()  # Ask the database.
        return row["last_position"]  # Return the number.

    # Return a page scanned earlier (its standards and pagination links), or None if it is new.
    def load_scanned_page(self, page_url):  # The address of the category page.
        page_row = self.connection.execute(
            "SELECT * FROM pages WHERE page_url = ?", (page_url,)
        ).fetchone()  # Look the page up.
        if page_row is None:  # We have never scanned it.
            return None  # Nothing to load.
        document_rows = self.connection.execute(
            "SELECT document_url, title, position, file_name_stem FROM documents WHERE page_url = ? ORDER BY position",
            (page_url,),
        ).fetchall()  # Load its standards in order.
        standards = [
            Standard(
                url=row["document_url"],
                title=row["title"],
                position=row["position"],
                file_name_stem=row["file_name_stem"],
            )
            for row in document_rows
        ]  # Convert rows to Standard objects.
        return standards, set(
            json.loads(page_row["pagination_links"])
        )  # Give back standards and pagination links.

    # Remember a freshly scanned page and every standard found on it.
    def save_scanned_page(
        self, category_code, page_url, standards, pagination_links
    ):  # All the details of the scan.
        with self.connection:  # Commits automatically when the block ends.
            self.connection.execute(
                "INSERT OR REPLACE INTO pages (page_url, category_code, pagination_links, scanned_at) VALUES (?, ?, ?, ?)",
                (
                    page_url,
                    category_code,
                    json.dumps(sorted(pagination_links)),
                    get_current_timestamp(),
                ),
            )  # Store the page.
            for standard in standards:  # Store every standard found on it.
                self.connection.execute(  # Insert only if we have not seen this standard before.
                    "INSERT OR IGNORE INTO documents (document_url, category_code, page_url, position, title, file_name_stem, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",  # SQL statement.
                    (
                        standard.url,
                        category_code,
                        page_url,
                        standard.position,
                        standard.title,
                        standard.file_name_stem,
                        get_current_timestamp(),
                    ),  # Values.
                )  # End of the insert.

    # True when the standard was saved earlier and its file is still on disk.
    def is_document_finished(self, document_url):  # The address of the View Only page.
        row = self.connection.execute(
            "SELECT status, file_path FROM documents WHERE document_url = ?",
            (document_url,),
        ).fetchone()  # Look it up.
        if row is None or not row["status"].startswith("saved"):  # Never saved.
            return False  # Not finished.
        return (
            row["file_path"] is not None and Path(row["file_path"]).exists()
        )  # Finished only if the file is still there.

    # Store the outcome of a download attempt right away, so Ctrl+C loses nothing.
    def record_document_result(
        self, document_url, download_result
    ):  # The standard's address and what happened.
        if (
            download_result.sha256 is not None
        ):  # A file was saved, so look for identical content elsewhere.
            duplicate_row = self.connection.execute(
                "SELECT document_url, file_path FROM documents WHERE sha256 = ? AND document_url != ?",
                (download_result.sha256, document_url),
            ).fetchone()  # Is the same content saved elsewhere?
            if duplicate_row is not None:  # Identical content already exists.
                logger.warning(
                    "Identical content already saved for %s (%s)",
                    duplicate_row["document_url"],
                    duplicate_row["file_path"],
                )  # Warn.
        with self.connection:  # Commits automatically when the block ends.
            self.connection.execute(  # Update the row for this standard.
                "UPDATE documents SET status = ?, download_url = ?, file_path = ?, file_size_bytes = ?, sha256 = ?, verdict = ?, content_type = ?, expected_size_bytes = ?, first_bytes_hex = ?, updated_at = ? WHERE document_url = ?",  # SQL statement.
                (
                    download_result.status,
                    download_result.download_url,
                    (
                        str(download_result.file_path)
                        if download_result.file_path
                        else None
                    ),
                    download_result.file_size_bytes,
                    download_result.sha256,
                    download_result.verdict,
                    download_result.content_type,
                    download_result.expected_size_bytes,
                    download_result.first_bytes_hex,
                    get_current_timestamp(),
                    document_url,
                ),  # Values.
            )  # End of the update.

    # Count standards per category and status, for the report.
    def count_documents_by_status(self):  # No inputs.
        return self.connection.execute(
            "SELECT category_code, status, COUNT(*) AS document_count FROM documents GROUP BY category_code, status ORDER BY category_code, status"
        ).fetchall()  # One row per group.

    # Hand out standards that are not saved yet, one at a time (a generator).
    def iterate_unsaved_documents(self):  # No inputs.
        cursor = self.connection.execute(
            "SELECT category_code, position, title, status FROM documents WHERE status NOT LIKE 'saved%' ORDER BY category_code, position"
        )  # Ask the database.
        for row in cursor:  # Go through the matching rows.
            yield row  # Give back one row, then wait to be asked for the next.


# ============================================================================
# File inspection
# ============================================================================


class FileVerdict(Enum):  # Every possible conclusion of the file check.
    OK = "ok"  # A complete, ordinary PDF.
    FILEOPEN_PROTECTED = (
        "fileopen-protected"  # Complete, but locked by FileOpen protection.
    )
    ENCRYPTED_PDF = "encrypted-pdf"  # Complete, but encrypted in another way.
    UNKNOWN_FORMAT = "unknown-format"  # Not a PDF, kept as delivered.
    EMPTY = "empty"  # Nothing was written.
    TRUNCATED = "truncated"  # A PDF whose ending is missing.
    SIZE_MISMATCH = "size-mismatch"  # The size differs from what was announced.
    HTML_PAGE = "html-page"  # A web page instead of the document.

    # Unusable files are deleted and downloaded again.
    def is_unusable(self):  # No inputs besides the verdict itself.
        return self in (
            FileVerdict.EMPTY,
            FileVerdict.TRUNCATED,
            FileVerdict.SIZE_MISMATCH,
            FileVerdict.HTML_PAGE,
        )  # These four mean "try again".


@dataclass  # Everything learned by looking inside one file.
class FileInspection:  # Produced by inspect_saved_file.
    verdict: FileVerdict  # The conclusion.
    size_bytes: int  # Size of the file.
    sha256: str  # Fingerprint of the file.
    first_bytes_hex: str  # First bytes in hexadecimal.


# Decide what kind of file some bytes are, and whether they are complete.
def classify_file_bytes(
    file_bytes, expected_size_bytes
):  # The bytes and the size we expect (or None).
    if len(file_bytes) == 0:  # Nothing was written.
        return FileVerdict.EMPTY  # Incomplete.
    if (
        expected_size_bytes is not None and len(file_bytes) != expected_size_bytes
    ):  # The announced size differs.
        return FileVerdict.SIZE_MISMATCH  # The download was cut short or altered.
    if file_bytes.lstrip()[:1] == b"<":  # Starts like HTML or XML.
        return FileVerdict.HTML_PAGE  # A web page, not the document.
    if not file_bytes.startswith(b"%PDF"):  # Not a PDF at all.
        return FileVerdict.UNKNOWN_FORMAT  # Keep it, but with its original extension.
    if b"%%EOF" not in file_bytes[-2048:]:  # A complete PDF ends with %%EOF.
        return FileVerdict.TRUNCATED  # The end of the file is missing.
    if b"FOPN_" in file_bytes:  # FileOpen's encryption marker is inside the PDF.
        return (
            FileVerdict.FILEOPEN_PROTECTED
        )  # Complete, but locked by the publisher's protection.
    if b"/Encrypt" in file_bytes:  # Some other encryption is present.
        return FileVerdict.ENCRYPTED_PDF  # Complete, but encrypted.
    return FileVerdict.OK  # A complete, ordinary PDF.


# Read a saved file once and describe it completely.
def inspect_saved_file(
    file_path, expected_size_bytes
):  # The file and the size we expect (or None).
    assert (
        file_path.exists()
    ), f"Cannot inspect a missing file: {file_path}"  # Catch programming mistakes early.
    file_bytes = file_path.read_bytes()  # Read the whole file into memory, once.
    verdict = classify_file_bytes(file_bytes, expected_size_bytes)  # Decide what it is.
    logger.debug(
        "File check: %s size=%d expected=%s verdict=%s first_bytes=%r",
        file_path.name,
        len(file_bytes),
        expected_size_bytes,
        verdict.value,
        file_bytes[:8],
    )  # Show what we found.
    return FileInspection(
        verdict=verdict,
        size_bytes=len(file_bytes),
        sha256=hashlib.sha256(file_bytes).hexdigest(),
        first_bytes_hex=format_first_bytes_as_hex(file_bytes),
    )  # Bundle the findings.


# Pick the right file extension for a verdict.
def choose_file_extension(
    verdict, suggested_file_name
):  # The verdict and the name the server suggested.
    match verdict:  # Compare the verdict against each case below.
        case (
            FileVerdict.OK | FileVerdict.FILEOPEN_PROTECTED | FileVerdict.ENCRYPTED_PDF
        ):  # All of these are PDF files.
            return ".pdf"  # Use the PDF extension.
        case other_verdict:  # Anything else keeps the server's extension.
            logger.debug(
                "Keeping the server's extension for verdict %s", other_verdict
            )  # Explain.
            suggested_extension = (
                suggested_file_name.rsplit(".", 1)[-1].lower()
                if "." in suggested_file_name
                else ""
            )  # Text after the last dot.
            return (
                f".{suggested_extension}" if suggested_extension else ".bin"
            )  # Use it, or a neutral fallback.


# Turn a verdict into the status text stored in the database.
def describe_saved_status(verdict):  # The verdict of a saved file.
    match verdict:  # Compare the verdict against each case below.
        case FileVerdict.OK:  # A normal PDF.
            return "saved"  # Simple success.
        case (
            FileVerdict.FILEOPEN_PROTECTED | FileVerdict.ENCRYPTED_PDF
        ):  # Complete but locked.
            return f"saved (complete, {verdict.value}: needs the publisher's viewer plug-in)"  # Explain why it may not open.
        case unrecognized_verdict:  # Anything else.
            return f"saved ({unrecognized_verdict.value}, kept as delivered)"  # Be honest about it.


# ============================================================================
# Download tracking
# ============================================================================

type DownloadId = str  # A readable name for the text ID Chromium gives every download.


@dataclass  # Progress of one download.
class DownloadState:  # Updated every time Chromium reports progress.
    url: str  # Where the file comes from.
    suggested_filename: str  # Name suggested by the server.
    state: str = "inProgress"  # inProgress, completed or canceled.
    total_bytes: int = 0  # Size announced by the server (0 when unknown).
    received_bytes: int = 0  # Bytes received so far.
    finished_event: asyncio.Event = field(
        default_factory=asyncio.Event
    )  # Set when the download completes or is cancelled.


class DownloadTracker:  # Receives Chromium's download events and lets the rest of the code wait for them.
    # Start with no known downloads.
    def __init__(self):  # No inputs.
        self.begun_download_ids = asyncio.Queue()  # IDs of downloads that just started.
        self.states = {}  # Progress records keyed by download ID.

    # Chromium calls this when a download starts.
    def handle_download_will_begin(
        self, event
    ):  # The event is a dictionary from Chromium.
        download_id = event["guid"]  # Unique ID of this download.
        logger.debug(
            "[chromium download begins] id=%s file=%s url=%s",
            download_id,
            event.get("suggestedFilename"),
            event.get("url"),
        )  # Print it.
        self.states[download_id] = DownloadState(
            url=event.get("url"),
            suggested_filename=event.get("suggestedFilename") or "",
        )  # Create a progress record.
        self.begun_download_ids.put_nowait(
            download_id
        )  # Tell the waiting code a download started.

    # Chromium calls this whenever a download makes progress.
    def handle_download_progress(
        self, event
    ):  # The event is a dictionary from Chromium.
        download_state = self.states.get(event["guid"])  # Find our record.
        if download_state is None:  # A download we did not see start.
            return  # Ignore it.
        download_state.total_bytes = int(
            event.get("totalBytes", 0) or 0
        )  # Update the announced size.
        download_state.received_bytes = int(
            event.get("receivedBytes", 0) or 0
        )  # Update the received bytes.
        download_state.state = event["state"]  # Update the state.
        logger.debug(
            "[chromium download progress] id=%s state=%s %d/%d bytes",
            event["guid"],
            event["state"],
            download_state.received_bytes,
            download_state.total_bytes,
        )  # Print it.
        if event["state"] in ("completed", "canceled"):  # The download is over.
            download_state.finished_event.set()  # Wake up the waiting code.

    # Throw away start events left over from earlier attempts.
    def clear_stale_events(self):  # No inputs.
        while (
            not self.begun_download_ids.empty()
        ):  # Keep going until the queue is empty.
            self.begun_download_ids.get_nowait()  # Discard one.

    # Wait for the next download to start, and return its ID.
    async def wait_for_download_to_start(
        self, timeout_seconds
    ):  # How long we are willing to wait.
        return await asyncio.wait_for(
            self.begun_download_ids.get(), timeout=timeout_seconds
        )  # Raises a timeout error if nothing starts.

    # Wait until a download completes, is cancelled, or stops making progress.
    async def wait_for_download_to_finish(
        self, download_state
    ):  # The progress record to watch.
        last_received_bytes = -1  # Bytes seen at the previous check.
        while True:  # Keep checking until a decision is made.
            try:  # Wait for the finished signal.
                await asyncio.wait_for(
                    download_state.finished_event.wait(),
                    timeout=DOWNLOAD_STALL_TIMEOUT_SECONDS,
                )  # Wait up to the stall limit.
                return download_state.state  # Report completed or canceled.
            except asyncio.TimeoutError:  # Nothing finished within the limit.
                if (
                    download_state.received_bytes == last_received_bytes
                ):  # No new bytes arrived.
                    return "stalled"  # Give up on this attempt.
                last_received_bytes = (
                    download_state.received_bytes
                )  # Remember the progress and keep waiting.
                logger.info(
                    "Still downloading: %d bytes received so far", last_received_bytes
                )  # Progress.

    # Remove a finished download from memory so the dictionary does not grow forever.
    def forget(self, download_id):  # The ID of the download to forget.
        if download_id in self.states:  # We know this download.
            del self.states[download_id]  # Delete its record.


# ============================================================================
# Browser debug listeners
# ============================================================================

response_headers_by_url = (
    OrderedDict()
)  # Headers of the most recent responses, keyed by address.
total_responses_seen = 0  # How many responses all tabs together have received.


# Remember a response's headers, forgetting the oldest ones so memory stays small.
def remember_response_headers(url, headers):  # The address and the headers.
    response_headers_by_url[url] = headers  # Store them.
    response_headers_by_url.move_to_end(url)  # Mark this address as the newest.
    while (
        len(response_headers_by_url) > MAXIMUM_REMEMBERED_RESPONSES
    ):  # Too many remembered.
        response_headers_by_url.popitem(last=False)  # Forget the oldest.


# Look up the headers the server sent for an address (an empty dictionary if unknown).
def get_response_headers(url):  # The address of the response.
    return response_headers_by_url.get(url, {})  # Return what we remembered.


# Report how many responses were seen in total.
def get_total_responses_seen():  # No inputs.
    return total_responses_seen  # The global counter.


# Attach logging listeners to one browser tab.
def attach_debug_listeners(page):  # The tab to watch.
    responses_seen_on_this_tab = 0  # Counter private to this tab.
    logger.debug(
        "Attaching listeners to new tab %s", page
    )  # Note that a new tab appeared.

    def log_console_message(
        console_message,
    ):  # Called for every browser console message.
        logger.debug(
            "[browser console.%s] %s", console_message.type, console_message.text
        )  # Print it.

    def log_page_error(page_error):  # Called for every uncaught JavaScript error.
        logger.debug("[browser page error] %s", page_error)  # Print it.

    def log_request(request):  # Called for every outgoing network request.
        logger.debug("[request] %s %s", request.method, request.url)  # Print it.

    def log_response(response):  # Called for every network response.
        nonlocal responses_seen_on_this_tab  # We change the counter that belongs to the surrounding function.
        global total_responses_seen  # We also change the counter shared by the whole file.
        responses_seen_on_this_tab += 1  # Count it for this tab.
        total_responses_seen += 1  # Count it overall.
        logger.debug(
            "[response #%d on this tab] %s %s",
            responses_seen_on_this_tab,
            response.status,
            response.url,
        )  # Print it.
        remember_response_headers(
            response.url, response.headers
        )  # Remember the headers for the file-size check.

    def log_failed_request(request):  # Called when a network request fails.
        logger.debug(
            "[request failed] %s %s", request.url, request.failure
        )  # Print it.

    def log_navigation(frame):  # Called when any frame navigates.
        if frame == frame.page.main_frame:  # Only report the top-level page.
            logger.debug("[navigated] %s", frame.url)  # Print the new address.

    def log_tab_closed(closed_page):  # Called when the tab closes.
        logger.debug("[tab closed] %s", closed_page.url)  # Print it.

    page.on("console", log_console_message)  # Listen for console messages.
    page.on("pageerror", log_page_error)  # Listen for JavaScript errors.
    page.on("request", log_request)  # Listen for requests.
    page.on("response", log_response)  # Listen for responses.
    page.on("requestfailed", log_failed_request)  # Listen for failed requests.
    page.on("framenavigated", log_navigation)  # Listen for navigations.
    page.on("close", log_tab_closed)  # Listen for the tab closing.


# ============================================================================
# Page helpers
# ============================================================================


# Click "Allow all cookies" if the cookie banner is showing, so it never blocks other clicks.
async def accept_cookie_banner_if_present(page):  # The tab to check.
    allow_all_button = page.locator(
        COOKIE_BANNER_ALLOW_ALL_SELECTOR
    ).first  # The button, if the banner exists.
    try:  # The page can change while we look, so guard everything.
        if (
            not await allow_all_button.is_visible()
        ):  # No banner is showing (this check does not wait).
            return  # Nothing to do.
        logger.info(
            "Cookie banner detected, clicking Allow all cookies"
        )  # Tell the user what is happening.
        await allow_all_button.click(timeout=5000)  # Click the button.
        await page.locator(COOKIE_BANNER_CONTAINER_SELECTOR).wait_for(
            state="hidden", timeout=5000
        )  # Wait for the banner to disappear.
    except (
        PlaywrightError
    ) as error:  # The click or the wait failed (the banner may have vanished by itself).
        logger.debug(
            "Cookie banner handling ended with: %s", str(error).splitlines()[0]
        )  # Note it and carry on.


# Wait until a tab has finished loading and redirecting.
async def wait_until_page_is_stable(page):  # The tab to wait for.
    logger.debug("Waiting for the tab to settle at %s", page.url)  # Show where we are.
    await page.wait_for_load_state("load")  # Wait for the full page load.
    try:  # Network-idle can time out on pages with trackers.
        await page.wait_for_load_state(
            "networkidle", timeout=10000
        )  # Wait until network traffic quiets down.
    except PlaywrightTimeoutError:  # The network never went quiet.
        pass  # That is fine, the page is probably ready anyway.
    await accept_cookie_banner_if_present(
        page
    )  # Dismiss the cookie banner if it popped up.
    logger.debug("Tab is stable at %s", page.url)  # Show the final address.


# Re-run a page action if a navigation interrupted it.
async def run_with_retry_on_navigation(
    page, action, *arguments
):  # The tab, the action, and the action's inputs.
    maximum_attempts = 4  # How many times to try.
    for attempt_number in range(
        1, maximum_attempts + 1
    ):  # Count attempts starting at 1.
        try:  # Attempt the action.
            logger.debug(
                "Attempt %d of %d for %s",
                attempt_number,
                maximum_attempts,
                getattr(action, "__name__", action),
            )  # Show progress.
            return await action(*arguments)  # Return the result when it works.
        except PlaywrightError as error:  # Playwright reported an error.
            first_error_line = str(error).splitlines()[
                0
            ]  # Keep only the first line of the message.
            logger.warning(
                "Playwright error on attempt %d: %s", attempt_number, first_error_line
            )  # Show it.
            is_navigation_error = "Execution context was destroyed" in str(
                error
            )  # Is this the navigation error we can retry?
            if (
                not is_navigation_error or attempt_number == maximum_attempts
            ):  # Other errors, or the last attempt, are fatal.
                raise  # Pass the error upward.
            await wait_until_page_is_stable(
                page
            )  # Let the navigation finish, then try again.
    return None  # Never reached, but keeps code checkers happy.


# Close a tab only if it is still open (some tabs close themselves).
async def close_tab_if_open(page):  # The tab to close.
    if page is not None and not page.is_closed():  # It exists and is open.
        await page.close()  # Close it.


# ============================================================================
# Portal registration and license pages
# ============================================================================


# Fill in and submit the registration form.
async def fill_registration_form(page):  # The tab showing Register.aspx.
    logger.info(
        "Registration page detected, filling the form..."
    )  # Tell the user what is happening.
    await accept_cookie_banner_if_present(
        page
    )  # Make sure the cookie banner is not covering the form.
    for (
        field_selector,
        field_value,
    ) in (
        REGISTRATION_DETAILS.to_text_field_values().items()
    ):  # Go through every text box.
        logger.debug(
            "Filling field %s", field_selector
        )  # Show which field (values are not logged).
        await page.fill(field_selector, field_value)  # Type the value into the box.
    logger.debug("Selecting the country")  # Country step.
    await page.select_option(
        "#ContentPlaceHolder_body_Country", REGISTRATION_DETAILS.country_code
    )  # Choose the country.
    await page.wait_for_load_state(
        "networkidle"
    )  # A country change can reload the state list, wait for it.
    logger.debug("Selecting the state")  # State step.
    await page.select_option(
        "#ContentPlaceHolder_body_State", REGISTRATION_DETAILS.state_code
    )  # Choose the state.
    logger.debug("Clicking the Next button")  # Submit step.
    await page.click("#ContentPlaceHolder_body_imgBtnSubmi")  # Press Next.
    await wait_until_page_is_stable(page)  # Wait for the following page.


# Click Accept on the End User License Agreement page.
async def accept_license_agreement(page):  # The tab showing EULA.aspx.
    logger.info(
        "License agreement page detected, clicking Accept..."
    )  # Tell the user what is happening.
    await page.click(
        "#ContentPlaceHolder_body_ImageButton_Accept"
    )  # Click the Accept button.
    await wait_until_page_is_stable(page)  # Wait for the following page.


# Open an address in a tab and get through any registration or agreement pages that appear.
async def open_page_and_pass_gates(
    page, target_url
):  # The tab and the address we really want.
    await page.goto(target_url, wait_until="domcontentloaded")  # Open the address.
    await wait_until_page_is_stable(page)  # Wait for redirects to finish.
    for gate_step in range(
        1, 4
    ):  # Allow up to three gate pages (registration, then agreement).
        logger.debug(
            "Gate check %d, current URL %s", gate_step, page.url
        )  # Show where we are.
        if "Register.aspx" in page.url:  # The site wants us to register.
            await fill_registration_form(page)  # Fill the form and press Next.
        elif "EULA.aspx" in page.url:  # The site shows the license agreement.
            await accept_license_agreement(page)  # Click Accept.
        else:  # No gate page is left.
            break  # Stop checking.
    if (
        "Register.aspx" in page.url
    ):  # The registration form is still showing, so it was not accepted.
        raise RuntimeError(
            "The portal did not accept the registration form; check the values in REGISTRATION_DETAILS"
        )  # Fail loudly instead of looping.
    if (
        "Fulfill.aspx" in page.url or target_url in page.url
    ):  # We ended up where we wanted.
        return  # Nothing more to do.
    logger.debug("Not on the wanted page (%s), reopening it", page.url)  # Explain.
    await page.goto(
        target_url, wait_until="domcontentloaded"
    )  # Go back to the wanted page.
    await wait_until_page_is_stable(page)  # Wait for it to finish loading.


# ============================================================================
# Category pages
# ============================================================================

READ_STANDARDS_JAVASCRIPT = """() => {  // Read the standards table.
    const anchors = document.querySelectorAll('a[href*="Fulfill.aspx?DocumentId="]');  // Every View Only link.
    return [...anchors].map(anchorElement => ({  // Build one record per link.
        url: anchorElement.href,  // The View Only page address.
        title: (anchorElement.closest('tr').querySelector('span.title') || {}).textContent || anchorElement.href  // The title in the same table row.
    }));  // End of the records.
}"""  # End of the JavaScript.


# Turn iec.aspx into 0, iec1.aspx into 1, iec2.aspx into 2, and so on.
def get_pagination_number(
    category_code, page_url
):  # The category and the page address.
    match = re.search(
        rf"/{category_code}(\d*)\.aspx", page_url, re.IGNORECASE
    )  # Find the digits after the category code.
    if match and match.group(1):  # There are digits in the address.
        return int(match.group(1))  # Use them as the page number.
    return 0  # No digits means the first page.


# Open a category page in a NEW tab and list its standards and pagination links.
async def read_category_page(
    context, category_code, page_url
):  # The browser context, the category and the page address.
    category_tab = await context.new_page()  # Open a new tab for this category page.
    try:  # Make sure the tab is closed afterwards.
        logger.debug("Opening category page %s", page_url)  # Show the address.
        await category_tab.goto(
            page_url, wait_until="domcontentloaded"
        )  # Open the page.
        await wait_until_page_is_stable(
            category_tab
        )  # Wait for redirects or reloads to finish.
        raw_standards = await run_with_retry_on_navigation(
            category_tab, category_tab.evaluate, READ_STANDARDS_JAVASCRIPT
        )  # Read the table.
        standards = [
            Standard(url=raw_standard["url"], title=raw_standard["title"].strip())
            for raw_standard in raw_standards
        ]  # Convert to Standard objects.
        logger.debug(
            "Found %d standards on %s", len(standards), page_url
        )  # Count them.
        all_links = await run_with_retry_on_navigation(
            category_tab,
            category_tab.eval_on_selector_all,
            "a[href]",
            "elements => elements.map(element => element.href)",
        )  # Read every link on the page.
        pagination_links = {
            link.split("#")[0]
            for link in all_links
            if re.search(rf"/Standards/{category_code}\d*\.aspx$", link, re.IGNORECASE)
        }  # Keep this category's pages only.
        logger.debug("Pagination links: %s", sorted(pagination_links))  # Show them.
        return standards, pagination_links  # Give both back.
    finally:  # Always runs.
        await category_tab.close()  # Close the category tab.


# Read a category page, trying again a few times if the portal is slow or fails.
async def read_category_page_with_retries(
    context, category_code, page_url
):  # Same inputs as read_category_page.
    for attempt_number in range(
        1, MAXIMUM_PAGE_SCAN_ATTEMPTS + 1
    ):  # Count attempts starting at 1.
        try:  # Attempt the read.
            return await read_category_page(
                context, category_code, page_url
            )  # Return the result when it works.
        except PlaywrightError as error:  # The page failed to load or read.
            logger.warning(
                "Reading %s failed on attempt %d: %s",
                page_url,
                attempt_number,
                str(error).splitlines()[0],
            )  # Show why.
            if attempt_number == MAXIMUM_PAGE_SCAN_ATTEMPTS:  # No attempts left.
                raise  # Give up and let the caller decide.
            await asyncio.sleep(
                RETRY_DELAY_SECONDS * attempt_number
            )  # Wait a little longer each time.
    return None  # Never reached, but keeps code checkers happy.


# Return a page's standards and pagination links, using the database when we have seen the page before.
async def get_page_contents(
    context, state_database, category_code, page_url
):  # Everything needed to load or scan a page.
    saved_page = state_database.load_scanned_page(
        page_url
    )  # Have we scanned this page before?
    if saved_page is not None:  # Yes.
        standards, pagination_links = saved_page  # Unpack the stored data.
        logger.info(
            "Page already scanned earlier, loaded %d standards from the database: %s",
            len(standards),
            page_url,
        )  # Report.
        return standards, pagination_links  # No need to visit the page again.
    standards, pagination_links = await read_category_page_with_retries(
        context, category_code, page_url
    )  # Visit the page.
    next_position = state_database.get_last_position(
        category_code
    )  # Continue numbering where we stopped.
    for standard in standards:  # Number every standard.
        next_position += 1  # Next document number.
        standard.position = next_position  # Remember the number.
        standard.file_name_stem = build_file_name_stem(
            f"{category_code} {next_position:03d} {standard.title}"
        )  # Clean, ordered file name.
    state_database.save_scanned_page(
        category_code, page_url, standards, pagination_links
    )  # Remember the page in the database.
    return standards, pagination_links  # Give the results back.


# ============================================================================
# Chromium control
# ============================================================================


class ChromiumController:  # Owns the browser for the whole run.
    # Remember Playwright; the browser itself is created in connect().
    def __init__(self, playwright):  # The running Playwright object.
        self.playwright = playwright  # Playwright itself.
        self.browser = None  # The connected browser.
        self.browser_session = (
            None  # Low-level control channel used for downloads and closing.
        )
        self.chromium_process = None  # The Chromium program, if we started it.

    # Locate the Chromium that Playwright installed.
    def find_chromium_program(self):  # No inputs.
        chromium_path = Path(
            self.playwright.chromium.executable_path
        )  # Playwright knows where its own Chromium lives.
        if not chromium_path.exists():  # It has not been installed yet.
            raise RuntimeError(
                "Playwright's Chromium is missing. Run: playwright install chromium"
            )  # Tell the user how to fix it.
        return str(chromium_path)  # Use this program.

    # Try once to connect to a Chromium that is already running.
    async def try_to_connect(self):  # No inputs.
        try:  # Connecting fails when nothing is listening.
            self.browser = await self.playwright.chromium.connect_over_cdp(
                f"http://localhost:{CHROMIUM_DEBUG_PORT}"
            )  # Connect to it.
            return True  # Connected.
        except PlaywrightError:  # Nothing is listening yet.
            return False  # Not connected.

    # Connect to a running Chromium, or start Playwright's Chromium and connect to that.
    async def connect(self):  # No inputs.
        if await self.try_to_connect():  # A Chromium is already running.
            logger.info(
                "Connected to the Chromium that is already running"
            )  # Good news.
            return  # Done.
        logger.info(
            "No Chromium is running yet, starting Playwright's Chromium"
        )  # Explain.
        chromium_program = (
            self.find_chromium_program()
        )  # Always Playwright's own Chromium.
        logger.debug(
            "Launching %s with profile %s", chromium_program, PROFILE_FOLDER.resolve()
        )  # Show the details.
        self.chromium_process = subprocess.Popen(  # Start Chromium as an independent program.
            [
                chromium_program,
                f"--remote-debugging-port={CHROMIUM_DEBUG_PORT}",
                f"--user-data-dir={PROFILE_FOLDER.resolve()}",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                PORTAL_BASE_URL,
            ],  # Options and start page (no-sandbox matches Playwright's default).
            stdout=subprocess.DEVNULL,  # Hide Chromium's own output.
            stderr=subprocess.DEVNULL,  # Hide Chromium's own errors.
            start_new_session=True,  # Keep Chromium running if this program ends normally.
        )  # End of the launch command.
        for wait_attempt in range(
            1, 31
        ):  # Wait up to 30 seconds for Chromium to be ready.
            await asyncio.sleep(1)  # Pause one second.
            if await self.try_to_connect():  # Chromium is ready.
                logger.info(
                    "Connected to the new Chromium after %d seconds", wait_attempt
                )  # Report.
                return  # Done.
            logger.debug(
                "Chromium not ready yet (attempt %d)", wait_attempt
            )  # Note it.
        raise RuntimeError(
            f"Could not connect to Chromium on port {CHROMIUM_DEBUG_PORT}"
        )  # Give up with a clear message.

    # Tell Chromium to write downloads itself, into our folder, and to report progress.
    async def route_downloads(
        self, download_tracker
    ):  # The tracker that receives the events.
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the folder exists.
        self.browser_session = (
            await self.browser.new_browser_cdp_session()
        )  # Open a low-level control channel (kept open while we work).
        self.browser_session.on(
            "Browser.downloadWillBegin", download_tracker.handle_download_will_begin
        )  # Listen for download starts.
        self.browser_session.on(
            "Browser.downloadProgress", download_tracker.handle_download_progress
        )  # Listen for download progress.
        await self.browser_session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(OUTPUT_FOLDER.resolve()),
                "eventsEnabled": True,
            },
        )  # Files are saved in the PDFs folder, named after their download ID.
        logger.info(
            "Chromium saves downloads itself into %s", OUTPUT_FOLDER.resolve()
        )  # Report.

    # Return Chromium's normal browsing context, so cookies and sessions are kept.
    async def get_context(self):  # No inputs.
        if self.browser.contexts:  # Chromium already has a default context.
            return self.browser.contexts[0]  # Use it.
        return await self.browser.new_context()  # Otherwise create one.

    # Close Chromium completely (used after Ctrl+C).
    async def close(self):  # No inputs.
        logger.info("Closing Chromium...")  # Tell the user.
        try:  # Ask Chromium itself to quit, which closes every tab.
            await self.browser_session.send("Browser.close")  # Tell Chromium to close.
        except (
            PlaywrightError,
            AttributeError,
        ) as error:  # The connection drops as Chromium quits, which is expected.
            logger.debug(
                "Close command finished with: %s", str(error).splitlines()[0]
            )  # Note it.
        if (
            self.chromium_process is not None
        ):  # We also started this Chromium ourselves.
            try:  # Wait briefly for a clean exit.
                self.chromium_process.wait(timeout=10)  # Wait up to 10 seconds.
            except subprocess.TimeoutExpired:  # It did not exit in time.
                logger.warning("Chromium did not exit, terminating it")  # Warn.
                self.chromium_process.terminate()  # Force it to stop.


# ============================================================================
# Downloading one standard
# ============================================================================


# Delete unfinished files left behind by an interrupted run (.part files and Chromium's temporary files).
def clean_leftover_downloads():  # No inputs.
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the folder exists.
    for leftover_path in OUTPUT_FOLDER.iterdir():  # Look at everything inside.
        is_unfinished = leftover_path.suffix in (
            PARTIAL_FILE_SUFFIX,
            ".crdownload",
        ) or LEFTOVER_DOWNLOAD_NAME_PATTERN.match(
            leftover_path.name
        )  # Does the name mark an unfinished download?
        if (
            leftover_path.is_file() and is_unfinished
        ):  # Only unfinished files, never real PDFs.
            logger.info("Removing leftover download %s", leftover_path.name)  # Report.
            leftover_path.unlink(missing_ok=True)  # Delete it.


# Click the DOWNLOAD link (which opens a new tab) and return that new tab.
async def click_download_link(
    context, download_link, download_url
):  # The context, the link, and the link's address.
    try:  # The link opens a new tab, so clicking it should produce one.
        async with context.expect_page(
            timeout=30000
        ) as new_tab_info:  # Wait for the new tab.
            await download_link.click()  # Click the DOWNLOAD link like a person would.
        download_tab = await new_tab_info.value  # Get the new tab.
        logger.debug("Download tab opened at %s", download_tab.url)  # Show it.
        return download_tab  # Hand it back.
    except PlaywrightTimeoutError:  # No new tab appeared.
        logger.warning(
            "The click opened no new tab, opening the link in one manually"
        )  # Fallback.
        download_tab = await context.new_page()  # Open our own new tab.
        try:  # Going to a download address raises an error that is normal.
            await download_tab.goto(download_url)  # Open the link directly.
        except (
            Exception
        ) as navigation_error:  # Ignore the "download is starting" error.
            logger.debug(
                "goto raised (often normal for downloads): %s",
                str(navigation_error).splitlines()[0],
            )  # Note it.
        return download_tab  # Hand it back.


# Work out the size the file should have, preferring what Chromium counted.
def work_out_expected_size(
    download_state, response_headers
):  # Chromium's record and the server's headers.
    if download_state.total_bytes > 0:  # Chromium knows the size.
        return download_state.total_bytes  # Use it.
    content_length = response_headers.get(
        "content-length", ""
    )  # The server's announced length.
    if content_length.isdigit() and not response_headers.get(
        "content-encoding"
    ):  # A trustworthy length is available.
        return int(content_length)  # Use it.
    return None  # Unknown.


# Make one download attempt and return a DownloadResult (file_path is set only on success).
async def attempt_single_download(
    context, download_tracker, download_link, download_url, standard
):  # Everything one attempt needs.
    download_tracker.clear_stale_events()  # Forget events from earlier attempts.
    download_tab = await click_download_link(
        context, download_link, download_url
    )  # Click the link.
    try:  # Wait for Chromium to report the start of a download.
        download_id = await download_tracker.wait_for_download_to_start(
            DOWNLOAD_START_TIMEOUT_SECONDS
        )  # Wait up to the limit.
    except asyncio.TimeoutError:  # Nothing started.
        logger.warning(
            "No download started within %d seconds; tab URL: %s",
            DOWNLOAD_START_TIMEOUT_SECONDS,
            download_tab.url,
        )  # Show where the tab ended up.
        await close_tab_if_open(download_tab)  # Close the tab.
        return DownloadResult(
            status="no file delivered (viewer page only)", download_url=download_url
        )  # Report it.
    try:  # Always forget the download afterwards.
        download_state = download_tracker.states[
            download_id
        ]  # The progress record for this download.
        final_state = await download_tracker.wait_for_download_to_finish(
            download_state
        )  # Wait until it ends.
        await close_tab_if_open(
            download_tab
        )  # Close the download tab now that the download is over.
        if final_state != "completed":  # The download did not finish.
            logger.warning(
                "Download ended with state '%s' after %d bytes",
                final_state,
                download_state.received_bytes,
            )  # Report.
            return DownloadResult(
                status=f"download {final_state}", download_url=download_url
            )  # Report it.
        chromium_file_path = (
            OUTPUT_FOLDER / download_id
        )  # Chromium names the finished file after the download ID.
        if not chromium_file_path.exists():  # The file is not where we expect.
            logger.warning(
                "Chromium reported completion but %s is missing; the folder starts with: %s",
                chromium_file_path.name,
                sorted(path.name for path in OUTPUT_FOLDER.iterdir())[:10],
            )  # Show what is there.
            return DownloadResult(
                status="download completed but file missing", download_url=download_url
            )  # Report it.
        partial_path = (
            OUTPUT_FOLDER / f"{standard.file_name_stem}.pdf{PARTIAL_FILE_SUFFIX}"
        )  # The finished download keeps a .part ending until it has been verified.
        chromium_file_path.replace(
            partial_path
        )  # Give it the readable name plus .part.
        response_headers = get_response_headers(
            download_state.url
        )  # Headers the server sent for this download.
        content_type = response_headers.get(
            "content-type"
        )  # Content type announced by the server.
        logger.debug(
            "Download headers: content-type=%s content-length=%s content-disposition=%s",
            content_type,
            response_headers.get("content-length"),
            response_headers.get("content-disposition"),
        )  # Show them.
        expected_size_bytes = work_out_expected_size(
            download_state, response_headers
        )  # The size the file should have.
        inspection = inspect_saved_file(
            partial_path, expected_size_bytes
        )  # Check the file (read once).
        if inspection.verdict.is_unusable():  # The file cannot be used.
            logger.warning(
                "File verdict '%s', it will be deleted", inspection.verdict.value
            )  # Warn.
            partial_path.unlink(missing_ok=True)  # Delete the bad file.
            return DownloadResult(
                status=f"incomplete ({inspection.verdict.value})",
                download_url=download_url,
                verdict=inspection.verdict.value,
                content_type=content_type,
                expected_size_bytes=expected_size_bytes,
                first_bytes_hex=inspection.first_bytes_hex,
            )  # Report it.
        final_path = (
            OUTPUT_FOLDER
            / f"{standard.file_name_stem}{choose_file_extension(inspection.verdict, download_state.suggested_filename)}"
        )  # Final name with the right extension.
        partial_path.replace(
            final_path
        )  # Remove the .part ending: the file is complete and verified.
        logger.info(
            "Saved %s (%d bytes, %s)",
            final_path,
            inspection.size_bytes,
            inspection.verdict.value,
        )  # Report.
        return DownloadResult(
            status=describe_saved_status(inspection.verdict),
            download_url=download_url,
            file_path=final_path,
            verdict=inspection.verdict.value,
            content_type=content_type,
            expected_size_bytes=expected_size_bytes,
            file_size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
            first_bytes_hex=inspection.first_bytes_hex,
        )  # Success.
    finally:  # Runs whether the attempt worked or not.
        download_tracker.forget(download_id)  # Remove the download from memory.


# Open one standard's View Only page in a NEW tab, download it (with retries), and return the result.
async def download_one_standard(
    context, download_tracker, standard
):  # Everything one standard needs.
    logger.debug(
        "Fetching %s -> %s", standard.url, standard.file_name_stem
    )  # Show the target.
    existing_files = [
        path
        for path in OUTPUT_FOLDER.glob(f"{standard.file_name_stem}.*")
        if path.suffix != PARTIAL_FILE_SUFFIX
    ]  # Finished files with this name already on disk (unfinished .part files do not count).
    if existing_files:  # The file is already on disk.
        inspection = inspect_saved_file(
            existing_files[0], None
        )  # Describe it so the database stays complete.
        return DownloadResult(
            status="saved (file already existed)",
            file_path=existing_files[0],
            verdict=inspection.verdict.value,
            file_size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
            first_bytes_hex=inspection.first_bytes_hex,
        )  # Reuse it.
    document_tab = await context.new_page()  # Open a new tab for this document.
    try:  # Make sure the tab is closed afterwards.
        await open_page_and_pass_gates(
            document_tab, standard.url
        )  # Open the page and handle registration or agreement pages.
        await accept_cookie_banner_if_present(
            document_tab
        )  # Make sure the cookie banner is not covering the page.
        download_link = document_tab.locator(
            "#ContentPlaceHolder_body_hl_Download"
        )  # The DOWNLOAD link.
        if await download_link.count() == 0:  # The link is missing.
            logger.warning("No DOWNLOAD link on %s", document_tab.url)  # Warn.
            return DownloadResult(status="no download link")  # Report it.
        download_url = await download_link.get_attribute(
            "href"
        )  # Remember the link target.
        logger.debug("Download link: %s", download_url)  # Show it.
        latest_result = DownloadResult(
            status="no file delivered", download_url=download_url
        )  # Default result.
        for attempt_number in range(
            1, MAXIMUM_DOWNLOAD_ATTEMPTS + 1
        ):  # Retry incomplete downloads.
            logger.info(
                "Download attempt %d of %d for %s",
                attempt_number,
                MAXIMUM_DOWNLOAD_ATTEMPTS,
                standard.file_name_stem,
            )  # Progress.
            if attempt_number > 1:  # This is a retry.
                await asyncio.sleep(
                    RETRY_DELAY_SECONDS * (attempt_number - 1)
                )  # Wait a little longer each time.
            latest_result = await attempt_single_download(
                context, download_tracker, download_link, download_url, standard
            )  # Try once.
            if latest_result.is_saved():  # It worked.
                break  # No more attempts needed.
        return latest_result  # The last result, good or bad.
    finally:  # Always runs.
        await close_tab_if_open(document_tab)  # Close the document tab.


# ============================================================================
# Reporting
# ============================================================================


# Show how many standards are in each status, and list the ones that are not saved.
def print_report(state_database):  # The open database.
    logger.info("=== Report from %s ===", STATE_DATABASE_PATH)  # Heading.
    for (
        row
    ) in (
        state_database.count_documents_by_status()
    ):  # One line per category and status.
        logger.info(
            "%-6s %-80s %d",
            row["category_code"],
            row["status"][:80],
            row["document_count"],
        )  # Print it.
    unsaved_count = 0  # How many standards still need attention.
    for (
        row
    ) in (
        state_database.iterate_unsaved_documents()
    ):  # One at a time, using the generator.
        unsaved_count += 1  # Count it.
        logger.info(
            "  not saved: %s #%03d %s -> %s",
            row["category_code"],
            row["position"],
            row["title"][:60],
            row["status"],
        )  # Print it.
    logger.info("Standards not saved yet: %d", unsaved_count)  # Total.


# ============================================================================
# Main workflow
# ============================================================================


# Download one standard unless it was already saved, and record the result.
async def process_one_standard(
    context, state_database, download_tracker, statistics, standard
):  # Everything one standard needs.
    if state_database.is_document_finished(
        standard.url
    ):  # Already saved in an earlier run.
        statistics.skipped_count += 1  # Count it.
        logger.info(
            "[%03d] already saved, skipping: %s", standard.position, standard.title[:60]
        )  # Report.
        return  # Do not download it twice.
    try:  # One failure should not stop the whole run.
        download_result = await download_one_standard(
            context, download_tracker, standard
        )  # Download it in a new tab.
    except (asyncio.CancelledError, KeyboardInterrupt):  # Ctrl+C was pressed.
        raise  # Let the shutdown code handle it.
    except Exception as unexpected_error:  # Catch anything unexpected.
        logger.exception(
            "Unexpected error on %s", standard.url
        )  # Print the full traceback.
        download_result = DownloadResult(
            status=f"error: {unexpected_error}"
        )  # Record the problem.
    state_database.record_document_result(
        standard.url, download_result
    )  # Save the result immediately.
    logger.info(
        "[%03d] %s: %s", standard.position, standard.title[:60], download_result.status
    )  # Progress.
    if download_result.is_saved():  # It worked.
        statistics.saved_count += 1  # Count it.
        statistics.consecutive_failures = 0  # The streak of failures is over.
    else:  # It failed.
        statistics.failed_count += 1  # Count it.
        statistics.consecutive_failures += 1  # Extend the streak of failures.
    if (
        statistics.consecutive_failures >= MAXIMUM_CONSECUTIVE_FAILURES
    ):  # Too many failures in a row.
        raise TooManyFailuresError(
            f"{statistics.consecutive_failures} standards failed in a row; the portal may be blocking us or changed"
        )  # Stop politely.
    await asyncio.sleep(PAUSE_BETWEEN_DOCUMENTS_SECONDS)  # Be polite to the server.


# Go through every page of one category, in page order.
async def process_one_category(
    context, state_database, download_tracker, statistics, category_code
):  # Everything one category needs.
    logger.info("=== Category: %s ===", category_code.upper())  # Progress.
    visited_pages = set()  # Pages of this category handled in this run.
    pending_pages = [
        urljoin(PORTAL_BASE_URL, f"Standards/{category_code}.aspx")
    ]  # Start with page 1.
    while pending_pages:  # Continue until every page was processed.
        page_url = pending_pages.pop(0)  # Take the lowest-numbered pending page.
        if page_url in visited_pages:  # Already done in this run.
            continue  # Skip it.
        visited_pages.add(page_url)  # Mark it as done.
        try:  # A broken page should not stop the other pages.
            standards, pagination_links = await get_page_contents(
                context, state_database, category_code, page_url
            )  # Load or scan the page.
        except (asyncio.CancelledError, KeyboardInterrupt):  # Ctrl+C was pressed.
            raise  # Let the shutdown code handle it.
        except Exception:  # Anything else went wrong.
            logger.exception(
                "Could not read %s; it will be tried again on the next run", page_url
            )  # Print the full traceback.
            statistics.failed_count += 1  # Count it.
            continue  # Move on to the next page.
        logger.info(
            "--- Page %d: %d standards ---",
            get_pagination_number(category_code, page_url) + 1,
            len(standards),
        )  # Progress.
        for pagination_link in pagination_links:  # Add pages discovered on this page.
            if (
                pagination_link not in visited_pages
                and pagination_link not in pending_pages
            ):  # Only new ones.
                pending_pages.append(pagination_link)  # Schedule it.
        pending_pages.sort(
            key=lambda pending_url: get_pagination_number(category_code, pending_url)
        )  # Keep pages in numeric order.
        logger.debug("Pages still pending: %s", pending_pages)  # Show them.
        for standard in standards:  # Download the first, then the second, and so on.
            await process_one_standard(
                context, state_database, download_tracker, statistics, standard
            )  # Handle it.


# The whole program: open everything, process the chosen categories, and clean up.
async def run_downloader(category_codes):  # The categories to process.
    logger.info(
        "Starting. Files: %s, profile and state database: %s",
        OUTPUT_FOLDER.resolve(),
        PROFILE_FOLDER.resolve(),
    )  # Show where things go.
    OUTPUT_FOLDER.mkdir(exist_ok=True)  # Create the PDFs folder if it is missing.
    clean_leftover_downloads()  # Remove unfinished files from an interrupted run.
    statistics = RunStatistics()  # Counters for this run.
    with StateDatabase(
        STATE_DATABASE_PATH
    ) as state_database:  # Open the database; it is closed automatically at the end.
        async with async_playwright() as playwright:  # Start Playwright.
            chromium_controller = ChromiumController(
                playwright
            )  # Prepare the browser manager.
            await chromium_controller.connect()  # Start or find Chromium.
            download_tracker = DownloadTracker()  # Prepare the download tracker.
            await chromium_controller.route_downloads(
                download_tracker
            )  # Let Chromium write downloads itself.
            context = (
                await chromium_controller.get_context()
            )  # Chromium's normal profile, so cookies stay.
            for existing_tab in context.pages:  # Tabs that were already open.
                attach_debug_listeners(
                    existing_tab
                )  # Log their events too (they are left open).
            context.on(
                "page", attach_debug_listeners
            )  # Attach listeners to every future tab.
            try:  # Watch for Ctrl+C and for too many failures.
                for (
                    category_code
                ) in category_codes:  # Every chosen category, one by one.
                    await process_one_category(
                        context,
                        state_database,
                        download_tracker,
                        statistics,
                        category_code,
                    )  # Handle it.
                logger.info(
                    "All chosen categories done. Chromium is left open so your session stays registered."
                )  # Explain why we do not close it.
            except (asyncio.CancelledError, KeyboardInterrupt):  # Ctrl+C was pressed.
                logger.warning(
                    "Ctrl+C received: saving progress and closing Chromium"
                )  # Tell the user.
                await chromium_controller.close()  # Close Chromium and every tab.
                clean_leftover_downloads()  # Remove any unfinished download that was interrupted.
            except (
                TooManyFailuresError
            ) as failure_error:  # The portal seems to be refusing us.
                logger.error(
                    "Stopping early: %s. Check the log above, then run the program again.",
                    failure_error,
                )  # Explain.
            finally:  # Runs on normal exit, after Ctrl+C and after early stops.
                logger.info(
                    "Browser responses seen in total: %d", get_total_responses_seen()
                )  # Statistic.
        print_report(state_database)  # Show a summary in the console.
        logger.info(
            "This run: %s", statistics.describe()
        )  # Show the counters for this run.
        logger.info(
            "Progress saved in %s. Run the program again to continue where it stopped.",
            STATE_DATABASE_PATH,
        )  # Final message.


# ============================================================================
# Start of the program
# ============================================================================


# Start the program: it always processes every category.
def main():  # No inputs.
    configure_logging()  # Turn on DEBUG logging to the console.
    try:  # Catch the final Ctrl+C that asyncio re-raises.
        asyncio.run(
            run_downloader(CATEGORY_CODES)
        )  # Run the downloader for all 12 categories.
    except KeyboardInterrupt:  # Ctrl+C was pressed.
        logger.warning("Stopped by Ctrl+C")  # Say so, then exit quietly.


if __name__ == "__main__":  # Run only when this file is executed directly.
    main()  # Start the program.
