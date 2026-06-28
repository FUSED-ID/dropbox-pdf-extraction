"""
_codex_runner.py — Reusable Codex Lane Test Infrastructure
===========================================================

Drop this file next to any script and import it to get a structured,
self-documenting test harness with labeled lanes, pass/fail tracking,
and a clean exit code.

Usage
-----
    from _codex_runner import CodexRunner

    cx = CodexRunner()

    @cx.layer("A", "Pure functions")
    def t_pure():
        cx.check("slugify empty string", slugify("") == "untitled")
        cx.check("slugify unicode", slugify("über") == "uber")

    @cx.layer("B", "State I/O")
    def t_state():
        data = load_state("/tmp/test_state.json")
        cx.check("load returns dict", isinstance(data, dict))

    if __name__ == "__main__":
        cx.run()

Design
------
- Layers are labeled A–Z (or multi-char: AA, AB, …)
- Each layer is a zero-arg function registered via @cx.layer(id, title)
- cx.check(name, condition, extra="") records PASS or FAIL
- cx.run() executes all layers in registration order, prints summary, sys.exit(0 or 1)
- cx.check() is safe to call from nested helpers — all go into the active layer's bucket
- Thread-safe for single-threaded test runners (no locking needed)

Poltergeist integration
-----------------------
If you need to verify SIGTERM resilience:
    cx.poltergeist(script_path, setup_fn, assert_fn, timeout=15)
See the poltergeist() docstring for details.

Codex Lane convention
---------------------
Each layer function should have a docstring:
    '''Layer A — Pure functions: slugify, classify, normalize'''
The runner prints this as the lane header for self-documentation.
"""

import os
import sys
import signal
import subprocess
import time
import traceback
from typing import Callable, List, Optional, Tuple


class CodexRunner:
    """Reusable Codex Lane test runner."""

    def __init__(self, name: str = "Codex"):
        self.name = name
        self._layers: List[Tuple[str, str, Callable]] = []  # (id, title, fn)
        self._passes: List[str] = []
        self._fails: List[str] = []
        self._current_layer: Optional[str] = None
        self._errors: List[str] = []  # layers that threw exceptions

    # ── Registration ──────────────────────────────────────────────────────────

    def layer(self, layer_id: str, title: str):
        """Decorator: register a test layer.

        @cx.layer("M", "Relative-path state keys")
        def t_relative_keys():
            cx.check("to_rel strips root", to_rel("/a/b/c") == "b/c")
        """
        def decorator(fn: Callable):
            self._layers.append((layer_id, title, fn))
            return fn
        return decorator

    def register(self, layer_id: str, title: str, fn: Callable):
        """Imperative alternative to the decorator form.

        cx.register("A", "Pure functions", t_pure)
        """
        self._layers.append((layer_id, title, fn))

    # ── Assertion ─────────────────────────────────────────────────────────────

    def check(self, name: str, condition: bool, extra: str = ""):
        """Record a single test result.

        Args:
            name:      Human-readable test name (shown in FAIL lines)
            condition: True = PASS, False = FAIL
            extra:     Optional detail appended to FAIL line (e.g. repr of actual value)
        """
        full_name = f"{self._current_layer or '?'}: {name}"
        if condition:
            self._passes.append(full_name)
        else:
            detail = f" — {extra}" if extra else ""
            self._fails.append(f"{full_name}{detail}")
            print(f"  ✗ FAIL  {name}{detail}")

    def expect_equal(self, name: str, actual, expected):
        """Convenience: check equality, auto-format the failure message."""
        self.check(name, actual == expected, f"got {actual!r}, want {expected!r}")

    def expect_true(self, name: str, value):
        """Convenience: check truthiness."""
        self.check(name, bool(value), f"got {value!r}")

    def expect_in(self, name: str, item, container):
        """Convenience: check membership."""
        self.check(name, item in container, f"{item!r} not in {type(container).__name__}")

    # ── Poltergeist pattern ────────────────────────────────────────────────────

    def poltergeist(
        self,
        script_path: str,
        setup: Callable,
        assertions: Callable,
        kill_after: float = 1.5,
        timeout: float = 15.0,
        env: Optional[dict] = None,
        args: Optional[List[str]] = None,
    ):
        """Test that a script survives SIGTERM and saves state correctly.

        The "poltergeist callback" pattern: a finally: save_state() in the script
        acts as the ghost that fires even when the process is killed.

        Args:
            script_path:  Absolute path to the script under test
            setup:        Callable() — runs before launching the script;
                          set up temp files, clear state, plant test PDFs, etc.
            assertions:   Callable() — runs after the process exits;
                          call cx.check() here to verify state was saved
            kill_after:   Seconds to wait before sending SIGTERM (default 1.5)
            timeout:      Seconds to wait for process to exit after SIGTERM (default 15)
            env:          Optional env dict to merge into os.environ for the subprocess
            args:         Optional extra args to pass to the script

        Example:
            def setup():
                open(state_path, 'w').write('{}')
                # plant 5 minimal PDFs in sandbox

            def assert_state():
                cx.check("state file exists", os.path.exists(state_path))
                cx.check("state is valid JSON", is_valid_json(state_path))
                cx.check("state non-empty", json.load(open(state_path)) != {})

            cx.poltergeist(script_path, setup, assert_state, kill_after=1.5)
        """
        setup()
        proc_env = {**os.environ}
        if env:
            proc_env.update(env)
        cmd = [sys.executable, script_path] + (args or [])
        proc = subprocess.Popen(cmd, env=proc_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(kill_after)
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        assertions()

    # ── Execution ─────────────────────────────────────────────────────────────

    def run(self, exit_on_completion: bool = True) -> int:
        """Run all registered layers in order.

        Prints a section header for each layer, runs it, catches exceptions
        (counts as a FAIL for the layer), then prints the final summary.

        Returns:
            0 if all tests passed, 1 if any failed.
        Calls sys.exit() unless exit_on_completion=False.
        """
        print(f"\n{'═' * 54}")
        print(f"  {self.name} Test Suite")
        print(f"  {len(self._layers)} layers registered")
        print(f"{'═' * 54}")

        for layer_id, title, fn in self._layers:
            header = f"Layer {layer_id} — {title}"
            docstring = (fn.__doc__ or "").strip().split("\n")[0]
            if docstring and docstring != title:
                header = f"Layer {layer_id} — {title}: {docstring}"
            print(f"\n── {header} ──")
            self._current_layer = layer_id
            try:
                fn()
            except Exception as e:
                msg = f"Layer {layer_id} raised {type(e).__name__}: {e}"
                self._fails.append(msg)
                self._errors.append(msg)
                print(f"  ✗ EXCEPTION  {msg}")
                traceback.print_exc()

        self._current_layer = None
        return self._print_summary(exit_on_completion)

    def run_layer(self, layer_id: str) -> int:
        """Run a single layer by ID. Useful for targeted debugging."""
        matches = [(lid, t, fn) for lid, t, fn in self._layers if lid == layer_id]
        if not matches:
            print(f"Layer {layer_id!r} not found. Registered: {[l[0] for l in self._layers]}")
            return 1
        saved_layers = self._layers
        self._layers = matches
        result = self.run(exit_on_completion=False)
        self._layers = saved_layers
        return result

    def _print_summary(self, exit_on_completion: bool) -> int:
        total = len(self._passes) + len(self._fails)
        passed = len(self._passes)
        failed = len(self._fails)

        print(f"\n{'═' * 54}")
        print(f"  PASSED: {passed}")
        print(f"  FAILED: {failed}")

        if self._errors:
            print(f"\n  EXCEPTIONS ({len(self._errors)}):")
            for e in self._errors:
                print(f"    • {e}")

        if self._fails and not self._errors:
            print(f"\n  FAILED TESTS:")
            for f in self._fails:
                print(f"    • {f}")

        if failed == 0:
            print(f"\n  ALL {total} TESTS GREEN ✓")
            print(f"{'═' * 54}\n")
            rc = 0
        else:
            print(f"\n  ✗ {failed}/{total} TESTS FAILED")
            print(f"{'═' * 54}\n")
            rc = 1

        if exit_on_completion:
            sys.exit(rc)
        return rc

    # ── Utilities ─────────────────────────────────────────────────────────────

    @property
    def pass_count(self) -> int:
        return len(self._passes)

    @property
    def fail_count(self) -> int:
        return len(self._fails)

    @property
    def all_passed(self) -> bool:
        return self.fail_count == 0 and self.pass_count > 0


# ── Standalone usage without class ────────────────────────────────────────────
# For scripts that prefer the original procedural style:

_default = CodexRunner()

def check(name: str, condition: bool, extra: str = ""):
    """Module-level check() that delegates to the default runner."""
    _default.check(name, condition, extra)

def section(title: str):
    """Print a section header (used in the procedural style)."""
    print(f"\n── {title} ──")

def run_default(exit_on_completion: bool = True) -> int:
    """Run the default module-level runner."""
    return _default.run(exit_on_completion)
