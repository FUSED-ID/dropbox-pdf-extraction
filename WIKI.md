# Dropbox PDF Extraction — System Wiki

> **Last updated:** 2026-06-28  
> **Script:** `~/obsidian-vault/dropbox/dropbox_pdf_extract.py`  
> **State file:** `~/obsidian-vault/dropbox/_extract_state.json`  
> **Test suite:** `~/obsidian-vault/dropbox/_test_suite.py` (129 tests, codex lanes A–Q)

---

## 1. What This System Does

Every PDF in your Dropbox is extracted to a searchable Obsidian markdown note with YAML frontmatter (`tags`, `project`, `domains`, `source_path`). Text extraction uses PyPDF2 first; if that yields nothing, it falls back to OCR via Tesseract + pdftoppm.

The daily scheduled Claude job runs at 4:30 AM. Each run processes up to 1,000 PDFs. At ~1,782 remaining (as of 2026-06-28), the backlog clears in 2 more runs.

---

## 2. Architecture

```
Dropbox PDFs
     │
     ▼
scan_dropbox_pdfs()          ← discovers new PDFs, stores relative-path keys
     │
     ▼
get_unextracted_pdfs()       ← filters: not extracted, not deferred, not terminal,
     │                          modified within 5 years (CUTOFF_YEARS)
     ▼
ProcessPoolExecutor          ← persistent worker pool (max_workers=1)
     │                          recycled on BrokenExecutor or hard timeout
     ├─ PyPDF2 extraction
     └─ OCR fallback (tesseract + pdftoppm → temp images in ~/.ocr_tmp/)
          │
          ▼
     write_note()            ← atomic: tmp + os.replace() + os.fsync()
          │                    src_to_slug guard prevents duplicate notes
          ▼
     record_success/failure()
          │
          ├── transient → retried up to MAX_ATTEMPTS=3
          ├── permanent → terminalized (no retry)
          └── deferred  → online-only Dropbox placeholder (needs "Make available offline")
               │
               ▼
     save_extract_state()    ← atomic write; also fires in finally: (poltergeist callback)
               │
               ▼
     _write_daily_report()   ← self-writes markdown report; agent exit doesn't lose it
```

### Key Design Decisions

**Relative-path state keys** (implemented 2026-06-28): State keys are stored as paths relative to `DROPBOX_ROOT` (e.g., `Sunified Group BV/doc.pdf`). This eliminates cross-machine orphan notes — keys match regardless of which machine's absolute prefix is used. Notes' `source_path` frontmatter stays absolute for human readability.

**`_rel_norm(path)`** is the universal comparison primitive: converts any path (absolute or relative, NFC or NFD normalized) to a normalized relative form before any key lookup.

**Transparent migration**: On first `load_extract_state()` after upgrade, `_normalize_state_keys()` converts all absolute keys to relative in memory. After the first `save_extract_state()` call, the file is permanently migrated. No manual step needed.

**Atomic writes**: All state and note writes use `tmp file → os.replace() → os.fsync()`. No partial writes survive a crash.

**Poltergeist callback**: `finally: save_extract_state(state)` in `main()` fires even on SIGTERM — the ghost that saves state no matter what kills the process.

---

## 3. State File Schema

`_extract_state.json` — 24,090 entries (19,519 PDFs + 4,571 non-PDFs from legacy catalog).

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

**`extracted` field values:**
- `true` — note exists in Obsidian
- `false` — not yet extracted (in the work queue)
- absent/`null` — legacy; treated as false

**`status` field** (for failed/deferred entries):
- `"deferred"` — online-only Dropbox placeholder; file has 0 bytes
- `"terminal_failure"` — gave up after MAX_ATTEMPTS=3 (usually encrypted/corrupt PDF)
- `"scanned_image"` — OCR attempted but image quality too low (retryable with `--retry-ocr`)

---

## 4. Maintenance Flags

Run from `~/obsidian-vault/dropbox/`:

```bash
python3 dropbox_pdf_extract.py [FLAGS]
```

### `--audit`

**Non-destructive.** Cross-matches notes on disk against state. Reports:

| Bucket | Meaning | Fix |
|--------|---------|-----|
| Orphaned notes | Note exists, no state entry | Future `--re-queue-orphans` or manual |
| Missing notes | State says extracted, no note file | `--re-extract-missing` |
| Stale entries | State entry exists, PDF deleted | `--prune-stale` |

As of 2026-06-28: 16 orphaned, 81 missing, 196 stale (pruned).

### `--prune-stale`

Removes state entries for PDFs that no longer exist on disk. Safe to run anytime. Returns count removed. Saves state.

### `--re-extract-missing`

Resets `extracted=True` entries that have no note on disk back to `extracted=False, attempts=0`. Those PDFs re-enter the work queue on the next normal run. Returns count reset.

### `--retry-ocr`

Resets `scanned_image` failures back to the work queue. Use after improving OCR toolchain or after "Make available offline" for previously deferred files.

### Composing flags

Flags are composable — run multiple in one invocation:

```bash
# Full maintenance pass (non-destructive audit first, then clean):
python3 dropbox_pdf_extract.py --audit --prune-stale --re-extract-missing

# Recover from a bad state:
python3 dropbox_pdf_extract.py --prune-stale
python3 dropbox_pdf_extract.py --re-extract-missing
python3 dropbox_pdf_extract.py  # normal batch to re-extract
```

---

## 5. Reconcile-from-Disk

Runs automatically at the start of every extraction pass. Reads notes' `source_path` frontmatter, converts via `_rel_norm()`, matches against state keys. Restores `extracted=True` for any state entry with a note on disk — this is the recovery mechanism after a state corruption event.

As of 2026-06-28, reconcile recovered 13,331 extracted flags after a scan-bug incident. The bug (using absolute path in the `if fpath in state` check after migration to relative keys) has been patched.

---

## 6. OCR Pipeline

When PyPDF2 extracts < 100 characters, OCR activates:

1. `pdftoppm` renders the PDF to PNG images in `~/.ocr_tmp/`
2. `tesseract` (v5.5.2 at `/opt/homebrew/bin/tesseract`) reads each image
3. Text is concatenated and written to the note
4. Temp images are cleaned up in the same `finally:` block

**Error classification:**
- `FileNotFoundError`, `PermissionError` → permanent failure (don't retry)
- Non-zero returncode, timeout → transient (retry up to MAX_ATTEMPTS)
- Zero bytes output → `scanned_image` status (retryable with `--retry-ocr`)

---

## 7. Test Codex (Layers A–Q, 129 tests)

The full test harness lives in `_test_suite.py`. All 129 tests must pass green before deploying any script change.

```
Layer A  — Pure functions (text extraction, slug generation)
Layer B  — State I/O (load, save, atomic write)
Layer C  — Failure routing (transient vs. permanent vs. deferred)
Layer D  — Path normalization (NFC/NFD, Unicode edge cases)
Layer E  — OCR error classification
Layer F  — State recovery (crash simulation)
Layer G  — Discovery (scan_dropbox_pdfs, 0-byte detection)
Layer H  — Worker pool (persistent ProcessPoolExecutor)
Layer I  — Reconcile-from-disk
Layer J  — Notes and report writing
Layer K  — Regression: no subprocess spawn per PDF
Layer L  — Legacy permanent-failure reset regression

── NEW (2026-06-28) ──
Layer M  — Relative-path state keys (to_rel, to_abs, _rel_norm, migration)
Layer N  — --audit flag (orphan/missing/stale counts)
Layer O  — --prune-stale flag
Layer P  — --re-extract-missing flag
Layer Q  — Poltergeist callback (SIGTERM survival, atomic write verified)
```

**Run the suite:**

```bash
cd ~/obsidian-vault/dropbox
python3 _test_suite.py
# Expected: PASSED: 129 / FAILED: 0 / ALL 129 TESTS GREEN ✓
```

The suite runs in an isolated sandbox (`~/.tmp_pdf_test_XXXXX`), never touches production state or notes, and exits 0 only when all green.

---

## 8. Does the Daily Scheduled Job Need the Codex Harness?

**No — not as part of the daily 4:30 AM run.**

The codex harness is a development/CI tool, not an operational one. Running 129 tests nightly adds ~45 seconds with no operational benefit when the script hasn't changed. The harness catches regressions in *code*, not in *data*.

**When to run the harness:**
- After any modification to `dropbox_pdf_extract.py`
- After dependency updates (PyPDF2, tesseract, poppler, Python version)
- As part of the weekly health checkpoint (see §9)
- After any incident that required a hotfix

The daily job should remain:
```
1. Run extraction batch (up to 1,000 PDFs)
2. Write daily report to _reconsider_report_YYYY-MM-DD.md
3. Write progress to _extraction_progress.json
```

---

## 9. Weekly Health Checkpoint

A weekly scheduled task (suggested: Sunday 6:00 AM) covering:

### Step 1 — Run the test suite
```bash
cd ~/obsidian-vault/dropbox && python3 _test_suite.py
```
Any failure here means a dependency or environment change broke something. Fix before the Monday extraction run.

### Step 2 — Audit state
```bash
python3 dropbox_pdf_extract.py --audit
```
Review the three buckets. If stale > 50 or missing > 20, run `--prune-stale --re-extract-missing`.

### Step 3 — Check deferred count
```bash
cat _extraction_progress.json | python3 -m json.tool | grep deferred
```
If `total_deferred_online_only` is growing, some folders may have gone offline-only. Open Dropbox, right-click the folder, "Make available offline", then `--retry-ocr` on the next run.

### Step 4 — Check OCR toolchain versions
```bash
/opt/homebrew/bin/tesseract --version
/opt/homebrew/bin/pdftoppm -v 2>&1
```
If these paths have changed (brew upgrade), update `TESSERACT_PATH` and `PDFTOPPM_PATH` at the top of the script.

### Step 5 — Write checkpoint summary to Obsidian
The weekly checkpoint agent writes a summary note at `~/obsidian-vault/dropbox/_weekly_checkpoint_YYYY-WXX.md` with:
- Test suite result (129/129 or N failures)
- Audit counts (orphaned / missing / stale)
- Extraction progress delta (notes added this week)
- Any deferred / terminal failure trends
- Action items for the coming week

---

## 10. Troubleshooting

### "15,000+ new PDFs discovered" on every run

**Cause:** The `if fpath in state` check in `scan_dropbox_pdfs()` is using an absolute path against a state file with relative keys. After the 2026-06-28 patch this should not recur. Verify with:
```bash
grep "if to_rel(fpath) in state" dropbox_pdf_extract.py
```

**Recovery:** Run reconcile (normal extraction run) to restore extracted flags from notes on disk.

### Notes not being found for already-extracted PDFs (orphan class)

**Cause (pre-2026-06-28):** State had absolute keys; cross-machine sync produced different absolute prefixes. **Fixed** by relative-path key migration.

**Cause (post-migration):** The source_path in the note frontmatter was written with one machine's absolute path. `reconcile_from_disk` uses `_rel_norm(src)` which handles this correctly.

### "DEFERRED: online-only placeholder" errors

The PDF exists in Dropbox but isn't downloaded. In Dropbox app: right-click the folder → "Make available offline". After sync completes, run `--retry-ocr` to requeue.

### State migration message appears on every run

**Cause:** The `--audit` flag (and other read-only passes) don't call `save_extract_state()` after migration, so the migration re-runs on each load until a write operation persists it.

**Fix:** Run any write-mode flag (`--prune-stale`, `--re-extract-missing`) or a normal extraction batch once. After the first `save_extract_state()`, the file has relative keys permanently and the migration message disappears.

### Extraction produces empty notes (< 100 chars, OCR fallback triggered)

Check `~/.ocr_tmp/` for leftover temp images — if cleanup failed, a crash interrupted the finally block. Safe to delete manually. Check tesseract is installed: `/opt/homebrew/bin/tesseract --version`.

---

## 11. Configuration Reference

Top of `dropbox_pdf_extract.py`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DROPBOX_ROOT` | `~/Dropbox` | Root of Dropbox sync folder |
| `OBSIDIAN_VAULT` | `~/obsidian-vault/dropbox` | Where notes are written |
| `STATE_FILE` | `_extract_state.json` | Persistent extraction state |
| `BATCH_SIZE` | 1000 | PDFs per run |
| `MAX_ATTEMPTS` | 3 | Retries before terminal failure |
| `CUTOFF_YEARS` | 5 | Skip PDFs older than N years |
| `WORKER_TIMEOUT` | 120 | Seconds per PDF before kill |
| `TESSERACT_PATH` | `/opt/homebrew/bin/tesseract` | OCR binary |
| `PDFTOPPM_PATH` | `/opt/homebrew/bin/pdftoppm` | PDF-to-image binary |

---

## 12. File Index

| File | Purpose |
|------|---------|
| `dropbox_pdf_extract.py` | Main extraction script |
| `_test_suite.py` | 129-test codex harness (layers A–Q) |
| `_extract_state.json` | Persistent state (24,090 entries, relative-path keys) |
| `_extraction_progress.json` | Latest batch summary (read by scheduled job agent) |
| `_reconsider_report_YYYY-MM-DD.md` | Daily agent self-report |
| `_audit_log_YYYY-MM-DD.txt` | Output of `--audit` runs |
| `_weekly_checkpoint_YYYY-WXX.md` | Weekly health checkpoint (to be created) |

---

*Sources: This wiki synthesizes design decisions, implementation details, test results, and incident analysis from the 2026-06-11 hardening session, the 2026-06-23 patch, and the 2026-06-28 relative-key + maintenance-flag extension session. All test results (129/129 green) verified by running `_test_suite.py` against the deployed script. Production audit data sourced from `_extract_state.json` and `_audit_log_2026-06-28.txt`. Confidence: high — all claims backed by code or live run output.*
