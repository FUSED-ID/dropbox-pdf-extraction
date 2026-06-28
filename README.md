# Dropbox PDF Extraction

Batch-extracts text from every PDF in your Dropbox into searchable Obsidian markdown notes. Runs daily at 4:30 AM, processes up to 1,000 PDFs per run, with full OCR fallback, crash-safe atomic writes, and a 129-test codex harness.

---

## Quick Start

```bash
cd ~/obsidian-vault/dropbox

# Normal extraction batch (up to 1,000 PDFs)
python3 dropbox_pdf_extract.py

# Audit state (non-destructive)
python3 dropbox_pdf_extract.py --audit

# Maintenance pass
python3 dropbox_pdf_extract.py --prune-stale --re-extract-missing

# Run the full test suite (must be 129/129 green before any deploy)
python3 _test_suite.py
```

---

## Architecture

```
Dropbox PDFs
     │
     ▼
scan_dropbox_pdfs()          ← stores relative-path keys (cross-machine portable)
     │
     ▼
get_unextracted_pdfs()       ← filters: not extracted, not deferred, not terminal
     │
     ▼
ProcessPoolExecutor           ← persistent pool (max_workers=1)
     │                          recycled on BrokenExecutor or hard timeout
     ├─ PyPDF2 extraction
     └─ OCR fallback           ← tesseract v5.5+ via pdftoppm → temp images
          │
          ▼
     write_note()              ← atomic: tmp → os.replace() → os.fsync()
          │
          ▼
     save_extract_state()      ← atomic; fires in finally: (poltergeist callback)
          │
          ▼
     _write_daily_report()     ← self-contained; never lost if agent exits early
```

### Key Design Decisions

**Relative-path state keys**: State keys are relative to `DROPBOX_ROOT` (e.g., `Sunified Group BV/doc.pdf`). Eliminates cross-machine orphan notes — a state file synced from another machine produces matching keys regardless of where Dropbox is mounted.

**Poltergeist callback**: `finally: save_extract_state(state)` in `main()` fires even on SIGTERM. State is never lost mid-run.

**Atomic writes everywhere**: All state and note files use `tmp file → os.replace() → os.fsync()`. No partial writes survive a crash.

**Transparent key migration**: First run after upgrade auto-converts old absolute keys to relative in memory; first `save_extract_state()` call persists the migration permanently. No manual step.

---

## Maintenance Flags

| Flag | Effect |
|------|--------|
| `--audit` | Non-destructive: cross-match notes vs state, print orphaned/missing/stale counts |
| `--prune-stale` | Remove state entries for PDFs that no longer exist on disk |
| `--re-extract-missing` | Reset `extracted=True` entries with no note on disk → back to work queue |
| `--retry-ocr` | Reset `scanned_image` failures → back to work queue |

Flags are composable: `--audit --prune-stale --re-extract-missing` runs all three.

---

## State File

`_extract_state.json` — 24,090 entries with relative-path keys.

```json
{
  "Sunified Group BV/Energy Token subsidie/doc.pdf": {
    "modified_at": 1656233601.0,
    "size": 235664,
    "domains": ["energy", "crypto"],
    "project": "sunified",
    "content_type": "pdf",
    "title": "Fwd Communicatie IZ Energie",
    "extracted": true
  }
}
```

**`extracted`**: `true` = note exists, `false` = in work queue, absent = legacy (treated as false)  
**`status`**: `"deferred"` = online-only Dropbox placeholder, `"terminal_failure"` = gave up after 3 attempts, `"scanned_image"` = OCR yielded no text

---

## Test Codex

`_test_suite.py` — 129 tests across 17 labeled lanes. All must be green before deploying any change.

| Layer | Title | Tests |
|-------|-------|-------|
| A | Pure functions | Slug generation, text extraction |
| B | State I/O | Load, save, atomic write |
| C | Failure routing | Transient vs permanent vs deferred |
| D | Path normalization | NFC/NFD, Unicode |
| E | OCR error classification | |
| F | State recovery | Crash simulation |
| G | Discovery | `scan_dropbox_pdfs`, 0-byte detection |
| H | Worker pool | Persistent `ProcessPoolExecutor` |
| I | Reconcile-from-disk | |
| J | Notes and report writing | |
| K | Regression: no subprocess spawn | |
| L | Legacy permanent-failure reset | |
| M | Relative-path state keys | `to_rel`, `to_abs`, `_rel_norm`, migration |
| N | `--audit` flag | Orphan/missing/stale counts |
| O | `--prune-stale` flag | |
| P | `--re-extract-missing` flag | |
| Q | Poltergeist callback | SIGTERM survival, atomic write verified |

### Using the Codex Runner for New Scripts

`_codex_runner.py` is the extracted, reusable test infrastructure. Use it to add a codex harness to any script:

```python
import sys
sys.path.insert(0, "/path/to/dropbox")
from _codex_runner import CodexRunner

cx = CodexRunner(name="My Script Tests")

@cx.layer("A", "Core logic")
def t_core():
    cx.check("basic case", my_fn("input") == "expected")
    cx.expect_equal("edge case", my_fn(""), "")

@cx.layer("B", "File I/O")
def t_io():
    cx.check("state saves", os.path.exists(state_path))

@cx.layer("Q", "Poltergeist callback")
def t_sigterm():
    def setup():
        open(state_path, "w").write("{}")
    def assert_state():
        cx.check("state exists after SIGTERM", os.path.exists(state_path))
        cx.check("state valid JSON", is_valid_json(state_path))
    cx.poltergeist("my_script.py", setup, assert_state, kill_after=1.5)

if __name__ == "__main__":
    cx.run()
```

---

## Configuration

Top of `dropbox_pdf_extract.py`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DROPBOX_ROOT` | `~/Dropbox` | Root of Dropbox sync folder |
| `OBSIDIAN_VAULT` | `~/obsidian-vault/dropbox` | Where notes are written |
| `STATE_FILE` | `_extract_state.json` | Persistent state |
| `BATCH_SIZE` | 1000 | PDFs per run |
| `MAX_ATTEMPTS` | 3 | Retries before terminal failure |
| `CUTOFF_YEARS` | 5 | Skip PDFs older than N years |
| `WORKER_TIMEOUT` | 120 | Seconds per PDF before kill |
| `TESSERACT_PATH` | `/opt/homebrew/bin/tesseract` | OCR binary |
| `PDFTOPPM_PATH` | `/opt/homebrew/bin/pdftoppm` | PDF-to-image binary |

---

## Scheduled Jobs

| Task | Schedule | Description |
|------|----------|-------------|
| `dropbox-pdf-extraction` | Daily 4:30 AM | Extract up to 1,000 PDFs |
| `dropbox-pdf-weekly-checkpoint` | Saturday 6:00 AM | Test suite + audit + checkpoint note |

Manage in Claude Desktop → Scheduled Tasks sidebar.

---

## File Index

| File | Purpose |
|------|---------|
| `dropbox_pdf_extract.py` | Main extraction script |
| `_codex_runner.py` | Reusable Codex Lane test runner |
| `_test_suite.py` | 129-test harness (imports codex runner) |
| `_extract_state.json` | Persistent state (24,090 entries) |
| `_extraction_progress.json` | Latest batch summary |
| `_reconsider_report_YYYY-MM-DD.md` | Daily self-report |
| `_weekly_checkpoint_YYYY-WXX.md` | Saturday health checkpoint |
| `dropbox-pdf-extraction-wiki.md` | Full system wiki |
| `README.md` | This file |

---

## Troubleshooting

**"15,000+ new PDFs discovered" on every run** → `scan_dropbox_pdfs()` key lookup bug. Verify: `grep "if to_rel(fpath) in state" dropbox_pdf_extract.py`. Recovery: run reconcile (normal batch run) to restore extracted flags.

**State migration message on every run** → Run any write-mode operation once (normal batch, `--prune-stale`, etc.) to persist the relative-key migration.

**DEFERRED / online-only errors** → Open Dropbox app, right-click folder → "Make available offline". After sync: `python3 dropbox_pdf_extract.py --retry-ocr`.

**OCR path broken** → `brew install tesseract poppler` or update `TESSERACT_PATH`/`PDFTOPPM_PATH` in the script.

---

## Incident Log

| Date | Incident | Fix |
|------|----------|-----|
| 2026-06-11 | Brittle per-PDF subprocess → broken pipes | Replaced with persistent `ProcessPoolExecutor` |
| 2026-06-23 | Crash mid-batch → partial state | Atomic write + poltergeist callback |
| 2026-06-28 | Key migration race: scan overwrote extracted flags | Fixed `if to_rel(fpath) in state` in `scan_dropbox_pdfs()` |
| 2026-06-28 | Cross-machine orphan class | Relative-path state keys (permanent fix) |
