#!/opt/homebrew/bin/python3
"""
Dropbox PDF Extraction to Obsidian Knowledge Graph — HARDENED (deployed 2026-06-11, patched 2026-06-23, extended 2026-06-28)

Hardened replacement for the original brittle version. Deployed after passing
the test suite (_test_hardened.py + _integ_run.py). Behavioural goals are unchanged;
documented in _reconsider_report_2026-06-11.md:

  1. No per-PDF process spawn. One persistent worker (ProcessPoolExecutor, max_workers=1)
     is reused across PDFs and only recreated after a hard timeout. All module-level side
     effects are guarded so spawn-imported children stay silent. -> kills the
     "[Errno 32] Broken pipe" failure class and most of the runtime.
  2. Transient vs permanent failure classification + attempt counter. Transient errors
     (broken pipe, timeout, no output, pdftoppm produced no images) are retried up to
     MAX_ATTEMPTS before becoming a permanent "failed"; encrypted/no-text-after-OCR are
     permanent immediately.
  3. Atomic state writes (tmp + os.replace).
  4. permanently_failed treated as terminal so the ETA converges.
  5. YAML-escaped frontmatter, narrowed except, banner printed once.

Flags:
  --retry-ocr            reset scanned-image failures for OCR retry (as before)
  --reset-transient      one-off: requeue prior transient failures incl. broken pipe
"""

import json
import os
import re
import sys
import subprocess
import datetime
import unicodedata
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout, BrokenExecutor

# === Configuration ===
# Paths are env-overridable so the suite can run fully isolated from production
# data (set DROPBOX_ROOT / EXTRACT_STATE_PATH / PROGRESS_PATH / PDF_OUTPUT_DIR /
# OBSIDIAN_VAULT). OBSIDIAN_VAULT must be set BEFORE the derived defaults below.
DROPBOX_ROOT = os.environ.get("DROPBOX_ROOT", "/Users/lg/Dropbox")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT", "/Users/lg/obsidian-vault")
EXTRACT_STATE_PATH = os.environ.get(
    "EXTRACT_STATE_PATH", os.path.join(OBSIDIAN_VAULT, "dropbox", "_extract_state.json"))
PROGRESS_PATH = os.environ.get(
    "PROGRESS_PATH", os.path.join(OBSIDIAN_VAULT, "dropbox", "_extraction_progress.json"))
PDF_OUTPUT_DIR = os.environ.get(
    "PDF_OUTPUT_DIR", os.path.join(OBSIDIAN_VAULT, "dropbox", "pdfs"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1000"))
SAVE_INTERVAL = 100
MAX_PAGES = 10
MAX_WORDS = 2000
CUTOFF_YEARS = 6   # company age window: captures full Sunified Group BV history (est. ~2020)
MAX_ATTEMPTS = 3                # transient failures retried up to this many runs
OCR_DPI = 200
OCR_MAX_PAGES = 5
OCR_CONVERT_TIMEOUT = 45
OCR_TESSERACT_TIMEOUT = 30
PDF_TOTAL_TIMEOUT = 90
TESSERACT_PATH = "/opt/homebrew/bin/tesseract"
PDFTOPPM_PATH = "/opt/homebrew/bin/pdftoppm"
# === Relative-path state keys (v2) ===
# State keys are stored relative to DROPBOX_ROOT so the state file is portable
# across machines. Legacy absolute keys are transparently migrated on first load.

def to_rel(fpath):
    """Return fpath relative to DROPBOX_ROOT; fall back to original for out-of-tree paths."""
    try:
        rel = os.path.relpath(fpath, DROPBOX_ROOT)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return fpath

def to_abs(key):
    """Reconstruct absolute path from a state key. Handles legacy absolute keys."""
    if os.path.isabs(key):
        return key
    return os.path.join(DROPBOX_ROOT, key)

def _rel_norm(path):
    """Normalize any path (absolute or relative) for cross-machine key comparison.
    Converts absolute Dropbox paths to relative, then applies _norm_path so that
    NFD/NFC variants and zero-width chars are collapsed."""
    return _norm_path(to_rel(path))

def _normalize_state_keys(state):
    """One-time transparent migration: convert absolute state keys to relative on load.
    After one save_extract_state() call the file is fully migrated; subsequent loads
    are a no-op (no absolute keys found)."""
    if not state or not any(os.path.isabs(k) for k in state):
        return state
    new_state = {}
    migrated = 0
    for k, v in state.items():
        rk = to_rel(k)
        new_state[rk] = v
        if rk != k:
            migrated += 1
    print(f"  [migrate] Converted {migrated} absolute state keys → relative (one-time)")
    return new_state



# Terminal (do-not-retry) statuses in state.
TERMINAL_STATUSES = ("failed", "permanently_failed")

# Substrings that mark a failure as transient (retry next run rather than give up).
TRANSIENT_MARKERS = (
    "broken pipe", "timed out", "timeout", "no output",
    "produced no images", "resource temporarily unavailable",
)

# Substrings in a pdftoppm stderr detail that mark the PDF as deterministically
# unreadable (corrupt structure / no pages). These recur identically on every run,
# so OCR-retrying them MAX_ATTEMPTS times across nightly runs gains nothing. We
# terminalize them in one attempt (like encrypted PDFs) instead of looping them
# through "produced no images" -> transient -> 3 wasted retries. Conservative,
# deterministic markers only — anything not listed stays transient (fail-safe
# toward retry, never toward burying a recoverable document).
DETERMINISTIC_PDF_ERRORS = (
    "couldn't read xref", "wrong page range", "may not be a pdf",
    "couldn't find trailer", "document stream is empty", "invalid xref",
    "not a pdf file", "no pages", "damaged",
)

# === PyPDF2 (import; install handled lazily in main, not at module scope) ===
try:
    import PyPDF2
    _HAVE_PYPDF2 = True
except ImportError:
    PyPDF2 = None
    _HAVE_PYPDF2 = False

import warnings
warnings.filterwarnings("ignore")
import logging
logging.disable(logging.CRITICAL)

# OCR availability is detected silently here; the banner is printed once from main().
OCR_AVAILABLE = False
try:
    import pytesseract  # noqa: F401  (presence check; we call tesseract via subprocess)
    from pdf2image import convert_from_path  # noqa: F401
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


def ensure_pypdf2():
    """Install PyPDF2 if missing. Called only from main(), never at import time."""
    global PyPDF2, _HAVE_PYPDF2
    if _HAVE_PYPDF2:
        return
    print("Installing PyPDF2...")
    for cmd in (
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "PyPDF2"],
        [sys.executable, "-m", "pip", "install", "PyPDF2"],
        ["/opt/homebrew/bin/pip3", "install", "PyPDF2"],
    ):
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    import PyPDF2 as _p  # noqa
    PyPDF2 = _p
    _HAVE_PYPDF2 = True


# === Classification Rules ===
PROJECT_RULES = [
    (re.compile(r'(?i)(sunified|solara|stak)'), "sunified"),
    (re.compile(r'(?i)(gmdx|GMDx|genomics)'), "gmdx"),
    (re.compile(r'(?i)(openclaw)'), "openclaw"),
    (re.compile(r'(?i)(powerbank)'), "powerbank"),
]
DOMAIN_RULES = [
    (re.compile(r'(?i)(solar|energy|renewable|grid|battery|bess|\bpv\b)'), "energy"),
    (re.compile(r'(?i)(bitcoin|crypto|blockchain|token|defi|nft)'), "crypto"),
    (re.compile(r'(?i)(\bai\b|llm|machine.?learning|deep.?learning|neural)'), "ai"),
    (re.compile(r'(?i)(chip|sim|esim|iot|semiconductor|hardware|nfc)'), "hardware"),
    (re.compile(r'(?i)(nda|contract|legal|compliance|patent|deed)'), "legal"),
    (re.compile(r'(?i)(finance|invest|bank|fund|pitch|budget|cashflow)'), "business"),
    (re.compile(r'(?i)(ssi|did|identity|passport|kyc)'), "identity"),
    (re.compile(r'(?i)(genom|gmdx|biotech|vaccine|mrna|health)'), "biotech"),
]


def classify_project(filepath):
    for pattern, project in PROJECT_RULES:
        if pattern.search(filepath):
            return project
    return "sunified"


def classify_domains(filepath, max_domains=2):
    domains = []
    for pattern, domain in DOMAIN_RULES:
        if pattern.search(filepath) and domain not in domains:
            domains.append(domain)
            if len(domains) >= max_domains:
                break
    return domains


def slugify(text, max_len=80):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text).strip('-')
    return text[:max_len]


def unique_slug(slug, existing_slugs):
    if slug not in existing_slugs:
        existing_slugs.add(slug)
        return slug
    counter = 2
    while f"{slug}-{counter}" in existing_slugs:
        counter += 1
    result = f"{slug}-{counter}"
    existing_slugs.add(result)
    return result


def _decode_source_value(raw):
    """Decode a note's `source_path:` frontmatter value.

    Notes are written with `source_path: {json.dumps(source_path)}` (ensure_ascii=True), so any
    non-ASCII path is stored as \\uXXXX escapes. Reading it back with a bare `.strip('"')` yields
    the literal escaped text, so non-ASCII paths never reconcile (a real cause of orphaned notes).
    json-decode first; fall back to quote-stripping for legacy/un-escaped lines."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        return raw.strip('"').strip("'")


def _norm_path(p):
    """Normalize a path for cross-store matching (state key <-> note source_path).

    NFC-normalize and strip true control/format chars (categories Cc/Cf, e.g. zero-width
    spaces) that creep in from synced folder names. Deliberately does NOT strip visible
    symbols like ␠ (U+2420, category So): fuzzing symbols could merge genuinely-distinct
    paths. Verified by _test_dedup_reconcile.py."""
    if not p:
        return ""
    p = unicodedata.normalize("NFC", p)
    return "".join(ch for ch in p if unicodedata.category(ch)[0] != "C")


# ---------------------------------------------------------------------------
# Extraction (runs inside the persistent pool worker)
# ---------------------------------------------------------------------------
def _is_online_only_placeholder(filepath):
    """True if a 0-byte file is a Dropbox smart-sync / online-only placeholder
    (real content lives in the cloud) rather than a genuinely empty file.

    macOS Python has no os.listxattr, so we shell out to /usr/bin/xattr. On any
    error we default to True (treat as placeholder) so a real-but-evicted
    document is PARKED for retry rather than terminally buried. Genuinely-empty
    stubs return no placeholder xattr and fall through to the terminal path."""
    try:
        out = subprocess.run(["/usr/bin/xattr", filepath],
                             capture_output=True, timeout=10
                             ).stdout.decode("utf-8", "replace")
        return "com.dropbox.placeholder" in out.split()
    except Exception:
        return True


def extract_pdf_text(filepath, max_pages=MAX_PAGES):
    """Return (text, method). method is 'pypdf2' or 'ocr'. Raises ValueError on failure."""
    # A 0-byte file is either a Dropbox online-only placeholder (real content in
    # the cloud — NOT hopeless; must be materialised first, so keep it RETRYABLE)
    # or a genuinely empty stub (terminal). Decide before touching PyPDF2/OCR so
    # we never spawn OCR on either, and never bury an evicted real document.
    try:
        if os.path.getsize(filepath) == 0:
            if _is_online_only_placeholder(filepath):
                raise ValueError("DEFERRED: online-only placeholder (not downloaded)")
            raise ValueError("PERMANENT: empty file (0 bytes)")
    except OSError as e:
        raise ValueError(f"PERMANENT: cannot stat file ({e})")

    text_parts = []
    is_encrypted = False
    try:
        with open(filepath, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            if reader.is_encrypted:
                # Try the empty password — this unlocks owner-password-only PDFs,
                # which are fully readable. PyPDF2 3.x returns a FALSY PasswordType
                # (NOT_DECRYPTED) on the wrong password WITHOUT raising, so we must
                # check the return value, not just catch exceptions. (The old code
                # only caught exceptions, so user-password PDFs slipped through with
                # is_encrypted still False, hit FileNotDecryptedError on .pages, and
                # were wastefully sent to OCR every run — which also can't read them.)
                unlocked = False
                try:
                    unlocked = bool(reader.decrypt(''))
                except Exception:
                    unlocked = False
                if not unlocked:
                    is_encrypted = True
                    # Terminal in one attempt: never spend an OCR pass on a
                    # password-protected file. Gives a clean, queryable bucket.
                    raise ValueError("PERMANENT: encrypted (password-protected)")
            pages_to_read = min(len(reader.pages), max_pages)
            for i in range(pages_to_read):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text_parts.append(page_text)
    except ValueError:
        raise
    except Exception as e:
        if OCR_AVAILABLE and not is_encrypted:
            return _extract_via_ocr(filepath), "ocr"
        raise ValueError(f"PDF read error: {e}")

    full_text = "\n".join(text_parts).strip()
    if full_text:
        return full_text, "pypdf2"
    if OCR_AVAILABLE:
        return _extract_via_ocr(filepath), "ocr"
    raise ValueError("No text extracted (scanned image PDF, OCR not available)")


def _extract_via_ocr(filepath, max_pages=OCR_MAX_PAGES):
    ocr_tmp_dir = os.path.expanduser("~/.ocr_tmp")
    os.makedirs(ocr_tmp_dir, exist_ok=True)
    tmp_prefix = os.path.join(ocr_tmp_dir, f"ocr_{os.getpid()}")

    def _cleanup():
        for f in os.listdir(ocr_tmp_dir):
            if f.startswith(f"ocr_{os.getpid()}"):
                try:
                    os.unlink(os.path.join(ocr_tmp_dir, f))
                except OSError:
                    pass

    try:
        res = subprocess.run(
            [PDFTOPPM_PATH, "-jpeg", "-r", str(OCR_DPI), "-f", "1",
             "-l", str(max_pages), filepath, tmp_prefix],
            capture_output=True, timeout=OCR_CONVERT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _cleanup()
        raise ValueError(f"OCR: PDF->image conversion timed out after {OCR_CONVERT_TIMEOUT}s")
    except FileNotFoundError:
        raise ValueError("OCR: pdftoppm not found — install poppler via brew")

    page_images = sorted(
        f for f in os.listdir(ocr_tmp_dir)
        if f.startswith(f"ocr_{os.getpid()}") and f.endswith(".jpg")
    )
    if not page_images:
        # Surface the real cause: empty/corrupt PDF, online-only placeholder, etc.
        err_lines = res.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = err_lines[-1] if err_lines else f"rc={res.returncode}"
        # A deterministic structural error (bad xref / 0 pages / not-a-PDF) will
        # fail identically on every retry — terminalize in one attempt instead of
        # looping it through the transient "produced no images" path MAX_ATTEMPTS
        # times. Unrecognised errors stay transient (retryable) on purpose.
        if any(m in detail.lower() for m in DETERMINISTIC_PDF_ERRORS):
            raise ValueError(f"PERMANENT: corrupt/unreadable PDF ({detail})")
        raise ValueError(f"OCR: pdftoppm produced no images ({detail})")

    text_parts = []
    for img_name in page_images:
        img_path = os.path.join(ocr_tmp_dir, img_name)
        try:
            tess = subprocess.run([TESSERACT_PATH, img_path, "stdout"],
                                  capture_output=True, timeout=OCR_TESSERACT_TIMEOUT)
            if tess.returncode == 0:
                page_text = tess.stdout.decode("utf-8", errors="replace").strip()
                if page_text:
                    text_parts.append(page_text)
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue
        finally:
            try:
                os.unlink(img_path)
            except OSError:
                pass

    full_text = "\n".join(text_parts).strip()
    if not full_text or len(full_text) < 20:
        raise ValueError("OCR: no meaningful text extracted")
    return full_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def first_n_words(text, n=MAX_WORDS):
    return " ".join(text.split()[:n])


def detect_title(filepath, text):
    basename = Path(filepath).stem
    return re.sub(r'[-_]+', ' ', basename).strip().title()


def load_extract_state():
    # Try primary state file first, then the atomic-write backup (.tmp left behind
    # on crash), then fall back to an empty dict. This prevents a corrupt primary
    # file (disk error, Dropbox conflict, partial SIGKILL) from aborting the whole run.
    for candidate in (EXTRACT_STATE_PATH, EXTRACT_STATE_PATH + ".tmp"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, 'r') as f:
                data = json.load(f)
            if candidate != EXTRACT_STATE_PATH:
                print(f"  WARNING: primary state file corrupt; recovered from backup: {candidate}")
            return _normalize_state_keys(data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: {candidate} unreadable ({e}); trying next candidate")
            continue
    # Derive from EXTRACT_STATE_PATH's directory so sandbox tests never escape to production.
    legacy_path = os.path.join(os.path.dirname(EXTRACT_STATE_PATH), "_catalog_state.json")
    if os.path.exists(legacy_path):
        print("  Migrating from _catalog_state.json to _extract_state.json...")
        try:
            with open(legacy_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: legacy state also unreadable ({e}); starting fresh")
    print("  WARNING: no valid state file found — starting fresh (all PDFs will be re-scanned)")
    return {}


def save_extract_state(state):
    """Atomic write: tmp file + os.replace so a kill mid-write can't truncate state."""
    os.makedirs(os.path.dirname(EXTRACT_STATE_PATH), exist_ok=True)
    tmp = EXTRACT_STATE_PATH + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, EXTRACT_STATE_PATH)


def reconcile_from_disk(state):
    """Restore extracted flags from notes on disk, and build a source->slug index.

    Returns (state, reconciled, src_to_slug). src_to_slug maps a normalized source_path to the
    slug of the note that already represents it, so main() can SKIP re-extracting an
    already-noted source instead of writing a numeric-suffixed duplicate. Matching is done on
    _norm_path(_decode_source_value(...)) so json-escaped / NFC-NFD / zero-width variants line
    up with their state key."""
    src_to_slug = {}
    if not os.path.exists(PDF_OUTPUT_DIR):
        return state, 0, src_to_slug
    on_disk_norm = {}  # normalized source_path -> raw source_path
    for fname in os.listdir(PDF_OUTPUT_DIR):
        if not fname.endswith('.md'):
            continue
        note_path = os.path.join(PDF_OUTPUT_DIR, fname)
        try:
            with open(note_path, 'r', encoding='utf-8', errors='ignore') as f:
                in_fm = False
                for line in f:
                    line = line.strip()
                    if line == '---' and not in_fm:
                        in_fm = True
                        continue
                    if line == '---' and in_fm:
                        break
                    if in_fm and line.startswith('source_path:'):
                        src = _decode_source_value(line.split(':', 1)[1])
                        if src:
                            key = _rel_norm(src)  # relative+norm: cross-machine safe
                            on_disk_norm[key] = src
                            # First note seen wins as the canonical slug for this source.
                            src_to_slug.setdefault(key, fname[:-3])  # key=_rel_norm
                        break
        except (OSError, UnicodeDecodeError):
            continue
    reconciled = 0
    for fpath, meta in state.items():
        if not isinstance(meta, dict) or meta.get('content_type') != 'pdf':
            continue
        if meta.get('extracted') is True:
            continue
        if _norm_path(to_rel(fpath)) in on_disk_norm:  # handles legacy abs keys
            meta['extracted'] = True
            reconciled += 1
    return state, reconciled, src_to_slug


def scan_dropbox_pdfs(state):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=CUTOFF_YEARS * 365)
    new_count = 0
    for root, dirs, files in os.walk(DROPBOX_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if not fname.lower().endswith('.pdf'):
                continue
            fpath = os.path.join(root, fname)
            if to_rel(fpath) in state:
                continue
            try:
                stat = os.stat(fpath)
                mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
                if mtime < cutoff:
                    continue
                # Classify 0-byte files at discovery so online-only placeholders
                # (a quarter of this corpus) never enter an extraction batch.
                if stat.st_size == 0:
                    if _is_online_only_placeholder(fpath):
                        status, reason = "deferred", "DEFERRED: online-only placeholder (not downloaded)"
                    else:
                        status, reason = "failed", "PERMANENT: empty file (0 bytes)"
                else:
                    status, reason = False, None
                entry = {
                    "modified_at": mtime.strftime("%Y-%m-%d"),
                    "size": stat.st_size,
                    "domains": classify_domains(fpath),
                    "project": classify_project(fpath),
                    "content_type": "pdf",
                    "title": detect_title(fpath, ""),
                    "extracted": status,
                }
                if reason:
                    entry["fail_reason"] = reason
                state[to_rel(fpath)] = entry
                new_count += 1
            except OSError:
                continue
    if new_count > 0:
        print(f"  Discovered {new_count} new PDFs in Dropbox scan")
    return state


def normalize_date(val):
    if isinstance(val, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(val).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return ""
    if isinstance(val, str):
        v = val.strip()
        # A stringified Unix epoch (e.g. "1611123456.0") must NOT be sliced to
        # "1611123456" — convert it back to a real date first.
        if re.fullmatch(r"\d{9,}(\.\d+)?", v):
            try:
                return datetime.datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                return ""
        return v[:10]
    return ""


def is_terminal(meta):
    """True if this entry should never be retried."""
    return meta.get("extracted") in TERMINAL_STATUSES


def get_unextracted_pdfs(state):
    cutoff_str = (datetime.datetime.now() -
                  datetime.timedelta(days=CUTOFF_YEARS * 365)).strftime("%Y-%m-%d")
    candidates = []
    for fpath, meta in state.items():
        if not isinstance(meta, dict) or meta.get("content_type") != "pdf":
            continue
        ex = meta.get("extracted")
        # Skip done (True), terminal (failed/permanently_failed) AND parked
        # ("deferred" online-only placeholders) so a batch is never wasted
        # re-attempting files whose content isn't on local disk.
        if ex is True or ex == "deferred" or is_terminal(meta):
            continue
        mod = normalize_date(meta.get("modified_at", ""))
        if not mod or mod < cutoff_str:
            continue
        candidates.append((to_abs(fpath), meta))
    candidates.sort(key=lambda x: normalize_date(x[1].get("modified_at", "")))
    return candidates[:BATCH_SIZE]


def is_transient(reason):
    r = (reason or "").lower()
    return any(m in r for m in TRANSIENT_MARKERS)


def record_failure(meta, reason):
    """Route a failure to the right state bucket.

    - 'DEFERRED:' -> parked (Dropbox online-only placeholder). Stays retryable but
      is EXCLUDED from normal batches and never advances toward terminal, so an
      evicted real document is never buried. It re-enters work via --retry-deferred
      after the user makes it available offline.
    - 'PERMANENT:' -> known-hopeless (genuinely empty, unstattable). Terminal now,
      bypassing the transient/attempt logic so we never waste OCR passes on it.
    - transient (broken pipe, timeout, produced-no-images, ...) -> retry until
      MAX_ATTEMPTS, then terminal.
    """
    meta["fail_reason"] = reason
    if reason.startswith("DEFERRED:"):
        meta["extracted"] = "deferred"
        meta["attempts"] = 0
        return
    attempts = int(meta.get("attempts", 0)) + 1
    meta["attempts"] = attempts
    permanent = reason.startswith("PERMANENT:")
    if not permanent and is_transient(reason) and attempts < MAX_ATTEMPTS:
        meta["extracted"] = False           # retry next run
    else:
        meta["extracted"] = "failed"        # terminal now


def write_obsidian_note(slug, title, source_path, domains, project, extracted_date,
                        modified_at, size_kb, summary_text, extraction_method="pypdf2"):
    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    note_path = os.path.join(PDF_OUTPUT_DIR, f"{slug}.md")
    # json.dumps gives safe, quoted YAML scalars (handles embedded quotes/specials).
    content = (
        "---\n"
        f"title: {json.dumps(title)}\n"
        f"source_path: {json.dumps(source_path)}\n"
        f"domains: {json.dumps(domains)}\n"
        f"project: {json.dumps(project)}\n"
        f"extracted_date: {json.dumps(extracted_date)}\n"
        "source: dropbox\n"
        "content_type: pdf\n"
        f"extraction_method: {json.dumps(extraction_method)}\n"
        f"modified_at: {json.dumps(modified_at)}\n"
        f"size_kb: {size_kb}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"## Summary (first {MAX_WORDS} words)\n\n"
        f"{summary_text}\n"
    )
    with open(note_path, 'w', encoding='utf-8') as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Persistent-worker extraction with hard timeout (replaces per-PDF spawn)
# ---------------------------------------------------------------------------
def _pool_task(filepath):
    return extract_pdf_text(filepath)


# --- self-test hooks (module-level so they're picklable by the pool) ---
def _worker_pid(_=None):
    return os.getpid()


def _sleep_task(seconds):
    import time
    time.sleep(seconds)
    return "done"


class Extractor:
    """One reusable worker process. Recreated only after a hard timeout."""
    def __init__(self):
        self._pool = ProcessPoolExecutor(max_workers=1)

    def _recycle(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._pool = ProcessPoolExecutor(max_workers=1)

    def extract(self, filepath, timeout=PDF_TOTAL_TIMEOUT):
        # Two attempts: the second is after an auto-recycle on BrokenExecutor so
        # a single worker death never aborts the whole batch.
        for attempt in range(2):
            try:
                fut = self._pool.submit(_pool_task, filepath)
                return fut.result(timeout=timeout)     # (text, method)
            except FutureTimeout:
                self._recycle()
                raise ValueError(f"PDF extraction timed out after {timeout}s (pathological PDF)")
            except BrokenExecutor:
                # Worker process died (segfault / OOM / kill). Recycle and retry once.
                self._recycle()
                if attempt == 0:
                    continue
                raise ValueError("Worker process died (BrokenExecutor) — PDF may be pathological")
            except Exception as e:
                raise ValueError(str(e))

    def close(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Maintenance flag implementations (v2)
# ---------------------------------------------------------------------------

def audit_state(state, src_to_slug):
    """--audit: cross-match on-disk notes vs state, print a discrepancy report.

    Returns (orphaned_count, missing_count, stale_count) for programmatic use.
    orphaned  = notes on disk whose source_path has NO state entry (old paths, moved files)
    missing   = state entries marked extracted=True with no note file on disk
    stale     = state entries for PDF files that no longer exist on disk
    """
    # Build set of _rel_norm keys that DO have a note (already in src_to_slug)
    noted_keys = set(src_to_slug.keys())

    # Build set of state keys for extracted PDFs
    extracted_keys = {}
    for k, v in state.items():
        if isinstance(v, dict) and v.get("content_type") == "pdf" and v.get("extracted") is True:
            extracted_keys[_norm_path(to_rel(k))] = k

    # Orphaned notes: in src_to_slug but not in extracted_keys
    orphaned = noted_keys - set(extracted_keys.keys())

    # Missing notes: in extracted_keys but not in src_to_slug
    missing_keys = set(extracted_keys.keys()) - noted_keys
    missing_paths = [extracted_keys[k] for k in missing_keys]

    # Stale entries: PDF state entries where the file no longer exists on disk
    stale = []
    for k, v in state.items():
        if isinstance(v, dict) and v.get("content_type") == "pdf":
            abs_p = to_abs(k)
            if not os.path.exists(abs_p):
                stale.append(k)

    print("\n=== --audit report ===")
    print(f"  Notes on disk:              {len(noted_keys)}")
    print(f"  PDFs extracted in state:    {len(extracted_keys)}")
    print(f"  Orphaned notes (no state):  {len(orphaned)}")
    print(f"    Fix: --re-queue-orphans (future flag) or manual reconcile")
    print(f"  Missing notes (state=True, no file): {len(missing_paths)}")
    print(f"    Fix: --re-extract-missing  requeues these for re-extraction")
    print(f"  Stale state entries (file deleted): {len(stale)}")
    print(f"    Fix: --prune-stale  removes these entries")

    if missing_paths[:5]:
        print(f"  Missing examples:")
        for p in missing_paths[:5]:
            print(f"    {to_abs(p)[:90]}")
    if stale[:3]:
        print(f"  Stale examples:")
        for k in stale[:3]:
            print(f"    {to_abs(k)[:90]}")
    return len(orphaned), len(missing_paths), len(stale)


def prune_stale(state):
    """--prune-stale: remove state entries for PDF files that no longer exist on disk.

    Only removes PDF entries. Non-PDF entries (docx, image, etc.) are left untouched
    since they belong to the catalog sync and this script doesn't manage them.
    Returns count of entries removed."""
    to_remove = []
    for k, v in list(state.items()):
        if isinstance(v, dict) and v.get("content_type") == "pdf":
            if not os.path.exists(to_abs(k)):
                to_remove.append(k)
    for k in to_remove:
        del state[k]
    return len(to_remove)


def requeue_missing(state, src_to_slug):
    """--re-extract-missing: reset extracted=True entries that have no note on disk.

    These are PDFs where the state says extracted but the .md file was lost
    (disk error, failed sync, mid-write crash). Resetting to False re-queues
    them for the next batch run.
    Returns count of entries reset."""
    noted_keys = set(src_to_slug.keys())
    reset = 0
    for k, v in state.items():
        if not isinstance(v, dict) or v.get("content_type") != "pdf":
            continue
        if v.get("extracted") is not True:
            continue
        if _norm_path(to_rel(k)) not in noted_keys:
            v["extracted"] = False
            v.pop("fail_reason", None)
            v["attempts"] = 0
            reset += 1
    return reset

def main():
    print("=== Dropbox PDF Extraction (hardened) ===")
    print(f"Date: {datetime.date.today()}")
    print(f"Batch size: {BATCH_SIZE}")
    ensure_pypdf2()
    print(f"OCR support: {'enabled' if OCR_AVAILABLE else 'disabled'}")
    print()

    print("Loading extraction state...")
    state = load_extract_state()
    print(f"  State has {len(state)} entries")

    print("Reconciling from Obsidian notes on disk...")
    state, reconciled, src_to_slug = reconcile_from_disk(state)
    if reconciled:
        print(f"  Restored {reconciled} extracted flags from notes on disk")

    # Rescue Dropbox online-only placeholders that an earlier build mis-marked as
    # empty/failed (the 0-byte short-circuit deployed 2026-06-14 buried these) or
    # that are still sitting retryable. Park them as "deferred" so they are not
    # buried and do not waste batch slots. Disk checks are bounded to entries that
    # already look empty, so this stays cheap.
    print("Reclassifying online-only placeholders...")
    reclaimed = 0
    for fpath, meta in state.items():
        if not isinstance(meta, dict) or meta.get("content_type") != "pdf":
            continue
        ex = meta.get("extracted")
        if ex is True or ex == "deferred":
            continue
        reason = (meta.get("fail_reason") or "").lower()
        looks_empty = (meta.get("size", None) == 0
                       or "empty file" in reason or "produced no images" in reason)
        if not looks_empty:
            continue
        try:
            if os.path.getsize(fpath) == 0 and _is_online_only_placeholder(fpath):
                meta["extracted"] = "deferred"
                meta["fail_reason"] = "DEFERRED: online-only placeholder (not downloaded)"
                meta["attempts"] = 0
                reclaimed += 1
        except OSError:
            continue
    if reclaimed:
        print(f"  Reclassified {reclaimed} online-only placeholders as deferred (not buried)")

    if "--retry-deferred" in sys.argv:
        reset = 0
        for meta in state.values():
            if isinstance(meta, dict) and meta.get("extracted") == "deferred":
                meta["extracted"] = False
                meta["fail_reason"] = ""
                meta["attempts"] = 0
                reset += 1
        print(f"  --retry-deferred: requeued {reset} placeholders "
              f"(run after making them available offline in Dropbox)")

    if "--retry-ocr" in sys.argv and OCR_AVAILABLE:
        reset = 0
        for meta in state.values():
            if not isinstance(meta, dict) or meta.get("extracted") != "failed":
                continue
            reason = (meta.get("fail_reason") or "").lower()
            if not reason or any(k in reason for k in
                                 ["scanned", "no text", "likely scanned", "ocr not available"]):
                meta["extracted"] = False
                meta.pop("fail_reason", None)
                meta["attempts"] = 0
                reset += 1
        print(f"  --retry-ocr: reset {reset} scanned-image failures")
    elif "--retry-ocr" in sys.argv:
        print("  WARNING: --retry-ocr requested but OCR is not available!")

    if "--reset-transient" in sys.argv:
        reset = 0
        for meta in state.values():
            if not isinstance(meta, dict) or meta.get("extracted") != "failed":
                continue
            if is_transient(meta.get("fail_reason")):
                meta["extracted"] = False
                meta["fail_reason"] = ""
                meta["attempts"] = 0
                reset += 1
        print(f"  --reset-transient: requeued {reset} transient failures (incl. broken pipe)")

    if "--audit" in sys.argv or "--prune-stale" in sys.argv or "--re-extract-missing" in sys.argv:
        # Maintenance modes need notes on disk indexed first
        print("Running maintenance pass...")

    if "--prune-stale" in sys.argv:
        removed = prune_stale(state)
        print(f"  --prune-stale: removed {removed} stale PDF entries (file no longer on disk)")
        save_extract_state(state)
        if "--audit" not in sys.argv and "--re-extract-missing" not in sys.argv:
            _write_progress(state, processed=0, failed=0, ocr_count=0)
            return

    if "--re-extract-missing" in sys.argv:
        removed_count = requeue_missing(state, src_to_slug)
        print(f"  --re-extract-missing: reset {removed_count} entries (state=True, no note on disk)")
        save_extract_state(state)
        if "--audit" not in sys.argv:
            _write_progress(state, processed=0, failed=0, ocr_count=0)
            return

    if "--audit" in sys.argv:
        audit_state(state, src_to_slug)
        _write_progress(state, processed=0, failed=0, ocr_count=0)
        return

    print("Scanning Dropbox for new PDFs...")
    state = scan_dropbox_pdfs(state)
    save_extract_state(state)

    candidates = get_unextracted_pdfs(state)
    # --limit N caps this run (for bounded validation runs / testing)
    if "--limit" in sys.argv:
        try:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
            candidates = candidates[:lim]
            print(f"  --limit {lim}: capping batch")
        except (ValueError, IndexError):
            print("  --limit given without a valid integer; ignoring")
    print(f"  Found {len(candidates)} un-extracted PDFs for this batch")

    if not candidates:
        print("\nNo PDFs to extract. Backlog is clear!")
        _write_progress(state, processed=0, failed=0, ocr_count=0)
        _write_daily_report(state, processed=0, failed=0, ocr_count=0)
        return

    existing_slugs = set()
    if os.path.exists(PDF_OUTPUT_DIR):
        for fname in os.listdir(PDF_OUTPUT_DIR):
            if fname.endswith('.md'):
                existing_slugs.add(fname[:-3])

    processed = failed = ocr_count = 0
    new_permanent_failures = []   # (fpath, reason) — passed to self-report at end
    today = str(datetime.date.today())
    extractor = Extractor()
    print(f"\nProcessing {len(candidates)} PDFs...")
    try:
        for i, (fpath, meta) in enumerate(candidates):
            try:
                text, method = extractor.extract(fpath)

                # GUARD: if a note already exists for this source (matched on normalized,
                # json-decoded path), mark state done and skip — never mint a slug-2 twin.
                existing = src_to_slug.get(_rel_norm(fpath))
                if existing:
                    meta["extracted"] = True
                    meta.setdefault("extraction_method", method)
                    meta.pop("fail_reason", None)
                    meta.pop("attempts", None)
                    processed += 1
                    done = processed + failed
                    if done % SAVE_INTERVAL == 0:
                        save_extract_state(state)
                    continue

                summary = first_n_words(text)
                title = detect_title(fpath, text)
                project = classify_project(fpath)
                domains = classify_domains(fpath)
                size_kb = round(meta.get("size", 0) / 1024)
                modified_at = normalize_date(meta.get("modified_at", today)) or today

                slug = slugify(title) or slugify(os.path.basename(fpath)) or f"pdf-{i}"
                slug = unique_slug(slug, existing_slugs)

                write_obsidian_note(slug, title, fpath, domains, project, today,
                                    modified_at, size_kb, summary, extraction_method=method)
                src_to_slug[_rel_norm(fpath)] = slug  # _rel_norm: cross-machine safe

                meta["extracted"] = True
                meta["extraction_method"] = method
                meta["title"] = title
                meta["domains"] = domains
                meta["project"] = project
                meta.pop("fail_reason", None)
                meta.pop("attempts", None)
                processed += 1
                if method == "ocr":
                    ocr_count += 1
            except Exception as e:               # narrowed: no BaseException
                record_failure(meta, str(e))
                failed += 1
                if meta.get("extracted") in TERMINAL_STATUSES:
                    new_permanent_failures.append((fpath, str(e)))
                if (processed + failed) <= 20 or (processed + failed) % 100 == 0:
                    tag = "RETRY" if meta.get("extracted") is False else "FAILED"
                    print(f"  {tag} [{processed + failed}/{len(candidates)}]: "
                          f"{os.path.basename(fpath)} — {e}")

            done = processed + failed
            if done % 50 == 0 or done == len(candidates):
                print(f"  Progress: {done}/{len(candidates)} "
                      f"(extracted: {processed}, failed: {failed})")
            if done % SAVE_INTERVAL == 0:
                save_extract_state(state)
                print(f"  [checkpoint] State saved at {done} PDFs")
    finally:
        extractor.close()
        save_extract_state(state)

    _write_progress(state, processed, failed, ocr_count, verbose=True)
    _write_daily_report(state, processed, failed, ocr_count,
                        new_failures=new_permanent_failures)


def _write_progress(state, processed, failed, ocr_count, verbose=False):
    pdfs = [v for v in state.values() if isinstance(v, dict) and v.get("content_type") == "pdf"]
    total_pdfs = len(pdfs)
    total_extracted = sum(1 for v in pdfs if v.get("extracted") is True)
    # Count BOTH terminal statuses so "remaining" reflects only retryable work.
    total_failed = sum(1 for v in pdfs if is_terminal(v))
    # Parked online-only placeholders: not done, not failed, not retryable here —
    # they need a Dropbox "make available offline" action before they can extract.
    total_deferred = sum(1 for v in pdfs if v.get("extracted") == "deferred")
    total_remaining = total_pdfs - total_extracted - total_failed - total_deferred
    runs_remaining = max(0, -(-total_remaining // BATCH_SIZE))

    progress = {
        "date": str(datetime.date.today()),
        "batch_processed": processed,
        "batch_failed": failed,
        "batch_ocr": ocr_count,
        "total_extracted": total_extracted,
        "total_remaining": total_remaining,
        "total_failed": total_failed,
        "total_deferred_online_only": total_deferred,
        "estimated_runs_remaining": runs_remaining,
    }
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp, PROGRESS_PATH)

    if verbose:
        print("\n=== Extraction Complete ===")
        print(f"This batch: {processed} extracted ({ocr_count} via OCR), {failed} failed/deferred")
        print(f"Total progress: {total_extracted}/{total_pdfs} PDFs extracted")
        print(f"Remaining (retryable): {total_remaining}")
        print(f"Deferred (online-only, need offline): {total_deferred}")
        print(f"Terminal failures: {total_failed}")
        print(f"Estimated runs remaining: {runs_remaining}")
        print(f"Estimated completion: "
              f"{datetime.date.today() + datetime.timedelta(days=runs_remaining)}")


def _write_daily_report(state, processed, failed, ocr_count, new_failures=None):
    """Write a self-contained markdown report so the agent report phase is optional.

    Called from main() after _write_progress(). Even if the scheduled-task agent exits
    before completing its analysis pass, this file captures the day's state. Directly
    addresses the 'agent-level brittleness' identified in round 8 (2026-06-21).
    """
    today = str(datetime.date.today())
    report_path = os.path.join(OBSIDIAN_VAULT, "dropbox", f"_reconsider_report_{today}.md")

    pdfs = [v for v in state.values() if isinstance(v, dict) and v.get("content_type") == "pdf"]
    total_pdfs = len(pdfs)
    total_extracted = sum(1 for v in pdfs if v.get("extracted") is True)
    total_term = sum(1 for v in pdfs if is_terminal(v))
    total_deferred = sum(1 for v in pdfs if v.get("extracted") == "deferred")
    total_remaining = total_pdfs - total_extracted - total_term - total_deferred

    # Count notes on disk
    notes_on_disk = 0
    if os.path.exists(PDF_OUTPUT_DIR):
        notes_on_disk = sum(1 for f in os.listdir(PDF_OUTPUT_DIR) if f.endswith(".md"))
    deficit = total_extracted - notes_on_disk

    # New failures this batch (permanent)
    failure_lines = ""
    if new_failures:
        lines = "\n".join(f"  - `{os.path.basename(p)}`: {r}" for p, r in new_failures[:20])
        if len(new_failures) > 20:
            lines += f"\n  - … and {len(new_failures) - 20} more"
        failure_lines = f"\n### New permanent failures this batch\n\n{lines}\n"

    content = f"""---
title: "PDF Extraction — Daily Self-Report (round auto): {today}"
date: {today}
author: dropbox_pdf_extract.py (self-written, no agent required)
---

# PDF Extraction — {today}

## Batch summary

| metric | value |
|---|---|
| processed (extracted) | {processed} |
| failed/deferred | {failed} |
| via OCR | {ocr_count} |

## Cumulative state ({total_pdfs} in-scope PDFs)

| status | count |
|---|---|
| `True` (extracted) | {total_extracted} |
| `False` (retryable) | {total_remaining} |
| `deferred` (online-only) | {total_deferred} |
| terminal (`failed`/`permanently_failed`) | {total_term} |

Notes on disk: **{notes_on_disk}**
State vs disk delta: {total_extracted} extracted − {notes_on_disk} notes = **{deficit:+d}**
{failure_lines}
## Residuals (unchanged from round 8 unless noted)

- **{total_deferred} deferred (online-only)**: need *Make available offline* + `--retry-deferred`
- **{total_term} terminal failures**: see `_extract_state.json` for `fail_reason` per entry
- **Estimated runs remaining**: {max(0, -(-total_remaining // BATCH_SIZE))}

## Notes

This report is written by the Python script at the end of every run. The scheduled-task
agent's manual report pass is supplementary — this file is the authoritative daily record.
"""
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Daily report written: {report_path}")


if __name__ == "__main__":
    main()
