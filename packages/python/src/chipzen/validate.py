"""Pre-upload bot validation.

Runs the same checks the platform performs during upload and build,
so you can catch problems before submitting.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Literal

# Severity levels for validation results
Severity = Literal["pass", "warn", "fail"]

# Default upload size limit in bytes — hard cap enforced by the platform.
# Recommended ceiling for most bots is ~300 MB; the validator treats
# anything over the 500 MB hard cap as a failure.
DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# Default decide() timeout warning threshold in milliseconds
DEFAULT_TIMEOUT_WARN_MS = 100

# Platform decision timeout
PLATFORM_TIMEOUT_MS = 500

# Blocked Python modules -- the platform sandbox (Docker + seccomp) prevents
# most of these at the syscall level, but importing them will still cause
# build or runtime failures.
BLOCKED_MODULES = frozenset(
    {
        "subprocess",
        "shutil",
        "ctypes",
        "multiprocessing",
        "signal",
        "resource",
        "_thread",
        "http",
        "http.client",
        "http.server",
        "urllib",
        "urllib.request",
        "requests",
        "httpx",
        "aiohttp",
        "flask",
        "django",
        "fastapi",
        "socketserver",
        "xmlrpc",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "pickle",
        "shelve",
        "marshal",
        "tempfile",
        "webbrowser",
        "code",
        "codeop",
        "compileall",
        "py_compile",
    }
)

# Modules that trigger a warning rather than a hard fail
WARN_MODULES = frozenset(
    {
        "os",
        "socket",
        "threading",
    }
)

# Allowed pip packages (subset -- the platform Docker image has these).
# Note: the SDK's published PyPI distribution is `chipzen-bot`; the
# `chipzen-sdk` name is the CLI command (the entry point in pyproject's
# `[project.scripts]`), not a package on PyPI. Earlier versions of this
# allowlist had `chipzen-sdk` instead of `chipzen-bot`, which silently
# rejected the canonical `chipzen-bot` dep until the severity was
# bumped from `warn` to `fail` and the test caught it.
ALLOWED_PACKAGES = frozenset(
    {
        "websockets",
        "numpy",
        "scipy",
        "chipzen-bot",
    }
)

# Allowed entry point filenames
ALLOWED_ENTRY_POINTS = ("main.py", "bot.py")


def validate_bot(
    bot_path: str | Path,
    *,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    timeout_warn_ms: int = DEFAULT_TIMEOUT_WARN_MS,
    entry_point: str | None = None,
    check_connectivity: bool = False,
) -> list[tuple[Severity, str, str]]:
    """Run all validation checks on a bot artifact.

    Args:
        bot_path: Path to a directory or .zip file containing the bot.
        max_upload_bytes: Maximum allowed zip size in bytes.
        timeout_warn_ms: Warn if decide() takes longer than this (ms).
        entry_point: Override the entry point filename (default: auto-detect).
        check_connectivity: When True, after the static + smoke checks pass,
            additionally drive the bot through a canned protocol exchange
            (handshake + one full hand + match_end) using an in-process mock
            WebSocket. Reports protocol-conformance failures as additional
            check entries. Pure connectivity — does not judge bot strength.

    Returns:
        List of (severity, check_name, message) tuples.
        Severity is "pass", "warn", or "fail".
    """
    bot_path = Path(bot_path)
    results: list[tuple[Severity, str, str]] = []

    # Determine working directory
    is_zip = bot_path.suffix.lower() == ".zip"

    if is_zip:
        results.extend(_check_zip_size(bot_path, max_upload_bytes))
        # Extract to temp dir for further checks
        import shutil as _shutil
        import tempfile as _tempfile

        tmpdir = _tempfile.mkdtemp(prefix="chipzen-validate-")
        try:
            with zipfile.ZipFile(bot_path, "r") as zf:
                zf.extractall(tmpdir)
            # Handle nested directory
            contents = list(Path(tmpdir).iterdir())
            if len(contents) == 1 and contents[0].is_dir():
                bot_dir = contents[0]
            else:
                bot_dir = Path(tmpdir)
            results.extend(
                _check_directory(bot_dir, entry_point, timeout_warn_ms, check_connectivity)
            )
        finally:
            _shutil.rmtree(tmpdir, ignore_errors=True)
    elif bot_path.is_dir():
        # Check what the zip size would be
        results.extend(_check_dir_size(bot_path, max_upload_bytes))
        results.extend(_check_directory(bot_path, entry_point, timeout_warn_ms, check_connectivity))
    else:
        results.append(
            ("fail", "file_structure", f"Path not found or not a directory/zip: {bot_path}")
        )

    return results


def _check_zip_size(zip_path: Path, max_bytes: int) -> list[tuple[Severity, str, str]]:
    """Check that the zip is within the upload size limit."""
    size = zip_path.stat().st_size
    mb = size / (1024 * 1024)
    limit_mb = max_bytes / (1024 * 1024)
    if size > max_bytes:
        return [("fail", "size", f"Zip is {mb:.1f}MB, exceeds {limit_mb:.0f}MB upload limit")]
    return [("pass", "size", f"Zip size OK ({mb:.1f}MB / {limit_mb:.0f}MB)")]


def _check_dir_size(dir_path: Path, max_bytes: int) -> list[tuple[Severity, str, str]]:
    """Estimate compressed size of a directory."""
    total = 0
    for f in dir_path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    mb = total / (1024 * 1024)
    limit_mb = max_bytes / (1024 * 1024)
    if total > max_bytes:
        return [
            (
                "fail",
                "size",
                f"Directory is {mb:.1f}MB uncompressed, likely exceeds {limit_mb:.0f}MB limit",
            )
        ]
    return [("pass", "size", f"Size OK ({mb:.1f}MB uncompressed / {limit_mb:.0f}MB limit)")]


def _check_directory(
    bot_dir: Path,
    entry_point: str | None,
    timeout_warn_ms: int,
    check_connectivity: bool = False,
) -> list[tuple[Severity, str, str]]:
    """Run all directory-level checks."""
    results: list[tuple[Severity, str, str]] = []

    # 1. File structure -- find entry point
    ep = _find_entry_point(bot_dir, entry_point)
    if ep is None:
        results.append(
            (
                "fail",
                "file_structure",
                f"No entry point found. Expected one of: {', '.join(ALLOWED_ENTRY_POINTS)}",
            )
        )
        return results  # Can't continue without entry point

    results.append(("pass", "file_structure", f"Entry point found: {ep.name}"))

    # 2. Syntax check
    source = ep.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=ep.name)
        results.append(("pass", "syntax", "Valid Python syntax"))
    except SyntaxError as e:
        results.append(("fail", "syntax", f"Syntax error: {e.msg} (line {e.lineno})"))
        return results  # Can't continue

    # 3. Import check
    results.extend(_check_imports(tree, ep.name))

    # 4. Bot class check
    bot_class_name = _find_bot_class(tree)
    if bot_class_name is None:
        results.append(
            (
                "fail",
                "bot_class",
                "No class inheriting from ChipzenBot or Bot found in entry point",
            )
        )
        return results
    results.append(("pass", "bot_class", f"Found bot class: {bot_class_name}"))

    # 5. Decide method check
    has_decide = _has_decide_method(tree, bot_class_name)
    if not has_decide:
        results.append(
            (
                "fail",
                "decide_method",
                f"{bot_class_name} does not implement decide()",
            )
        )
        return results
    results.append(("pass", "decide_method", f"{bot_class_name}.decide() implemented"))

    # 6. Requirements check
    req_file = bot_dir / "requirements.txt"
    if req_file.exists():
        results.extend(_check_requirements(req_file))
    else:
        results.append(
            ("pass", "requirements", "No requirements.txt (only stdlib + SDK will be available)")
        )

    # 7. Smoke test + timeout check
    results.extend(_smoke_test(bot_dir, ep, bot_class_name, timeout_warn_ms))

    # 8. (Optional) Protocol-conformance check.
    # Only run if the smoke test passed — if the bot can't even be
    # instantiated and called once, the wire-level scenario can't tell
    # us anything new and would just produce a noisier failure.
    if check_connectivity:
        smoke_passed = all(sev != "fail" for sev, name, _ in results if name == "smoke_test")
        if smoke_passed:
            results.extend(_connectivity_check(bot_dir, ep, bot_class_name))
        else:
            results.append(
                (
                    "warn",
                    "connectivity",
                    "skipped — smoke_test did not pass; fix that first then re-run",
                )
            )

    return results


def _connectivity_check(
    bot_dir: Path,
    entry_point: Path,
    class_name: str,
) -> list[tuple[Severity, str, str]]:
    """Drive the bot through canned protocol exchanges via an in-process mock.

    Re-imports the bot fresh (sys.path / sys.modules juggling) so this
    runs against the same code the smoke test exercised and isn't holding
    onto stale state from earlier checks.
    """
    from chipzen.conformance import run_conformance_checks

    bot_dir_str = str(bot_dir)
    added_to_path = False
    if bot_dir_str not in sys.path:
        sys.path.insert(0, bot_dir_str)
        added_to_path = True

    module_name = entry_point.stem
    if module_name in sys.modules:
        del sys.modules[module_name]

    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        bot = cls()
        checks = run_conformance_checks(bot)
        return [(c.severity, c.name, c.message) for c in checks]
    except Exception as exc:  # noqa: BLE001 — surface anything that goes wrong
        return [
            (
                "fail",
                "connectivity",
                f"failed to load bot for connectivity check: {type(exc).__name__}: {exc}",
            )
        ]
    finally:
        if added_to_path and bot_dir_str in sys.path:
            sys.path.remove(bot_dir_str)
        if module_name in sys.modules:
            del sys.modules[module_name]


def _find_entry_point(bot_dir: Path, override: str | None) -> Path | None:
    """Find the bot entry point file."""
    if override:
        p = bot_dir / override
        return p if p.exists() else None
    for name in ALLOWED_ENTRY_POINTS:
        p = bot_dir / name
        if p.exists():
            return p
    return None


def _check_imports(tree: ast.AST, filename: str) -> list[tuple[Severity, str, str]]:
    """Scan AST for blocked imports."""
    results: list[tuple[Severity, str, str]] = []
    found_blocked: list[str] = []
    found_warned: list[str] = []

    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules = [node.module.split(".")[0]]

        for mod in modules:
            if mod in BLOCKED_MODULES and mod not in WARN_MODULES:
                if mod not in found_blocked:
                    found_blocked.append(mod)
            elif mod in WARN_MODULES:
                if mod not in found_warned:
                    found_warned.append(mod)

    if found_blocked:
        results.append(
            (
                "fail",
                "imports",
                f"Blocked imports found: {', '.join(found_blocked)}. "
                "These are not available in the platform sandbox.",
            )
        )
    elif found_warned:
        results.append(
            (
                "warn",
                "imports",
                f"Potentially restricted imports: {', '.join(found_warned)}. "
                "Some functionality may be blocked in the sandbox.",
            )
        )
    else:
        results.append(("pass", "imports", "No blocked imports detected"))

    return results


def _find_bot_class(tree: ast.Module) -> str | None:
    """Find a class that inherits from ChipzenBot/Bot in the AST."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name in (
                    "ChipzenBot",
                    "Bot",
                    "TiltBot",
                ):  # TiltBot kept for legacy SDK compat
                    return node.name
    return None


def _has_decide_method(tree: ast.Module, class_name: str) -> bool:
    """Check if the named class has a decide() method."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == "decide":
                        return True
    return False


def _check_requirements(
    req_file: Path,
) -> list[tuple[Severity, str, str]]:
    """Check that all requirements are in the allowed package list."""
    results: list[tuple[Severity, str, str]] = []
    disallowed: list[str] = []

    for line in req_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Extract package name (strip version specifiers)
        pkg = (
            line.split(">=")[0]
            .split("<=")[0]
            .split("==")[0]
            .split("~=")[0]
            .split("!=")[0]
            .split(">")[0]
            .split("<")[0]
            .split("[")[0]
            .strip()
        )
        if pkg.lower() not in {p.lower() for p in ALLOWED_PACKAGES}:
            disallowed.append(pkg)

    if disallowed:
        results.append(
            (
                "fail",
                "requirements",
                f"Packages not in the platform allow-list: {', '.join(disallowed)}. "
                f"Allowed: {', '.join(sorted(ALLOWED_PACKAGES))}. "
                "The platform sandbox will reject the bot at install time.",
            )
        )
    else:
        results.append(("pass", "requirements", "All requirements are in the allowed package list"))

    return results


def _smoke_test(
    bot_dir: Path,
    entry_point: Path,
    class_name: str,
    timeout_warn_ms: int,
) -> list[tuple[Severity, str, str]]:
    """Try to instantiate the bot, call decide() with a mock state."""
    results: list[tuple[Severity, str, str]] = []

    # Temporarily add bot dir to sys.path
    bot_dir_str = str(bot_dir)
    added_to_path = False
    if bot_dir_str not in sys.path:
        sys.path.insert(0, bot_dir_str)
        added_to_path = True

    module_name = entry_point.stem
    # Remove from sys.modules to force fresh import
    if module_name in sys.modules:
        del sys.modules[module_name]

    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        bot = cls()

        # Build a mock GameState
        from chipzen.models import Action, Card, GameState

        mock_state = GameState(
            hand_number=1,
            phase="preflop",
            hole_cards=[Card.from_str("Ah"), Card.from_str("Kd")],
            board=[],
            pot=150,
            your_stack=9900,
            opponent_stacks=[9850],
            your_seat=0,
            dealer_seat=0,
            to_call=50,
            min_raise=200,
            max_raise=9900,
            valid_actions=["fold", "call", "raise"],
            action_history=[],
        )

        start = time.perf_counter()
        action = bot.decide(mock_state)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Verify return type
        if not isinstance(action, Action):
            results.append(
                (
                    "fail",
                    "smoke_test",
                    f"decide() returned {type(action).__name__}, expected Action",
                )
            )
        elif action.action not in ("fold", "check", "call", "raise"):
            results.append(
                (
                    "fail",
                    "smoke_test",
                    f"decide() returned unknown action: {action.action!r}",
                )
            )
        else:
            results.append(
                (
                    "pass",
                    "smoke_test",
                    f"decide() returned {action.action} successfully",
                )
            )

        # Timeout check
        if elapsed_ms > PLATFORM_TIMEOUT_MS:
            results.append(
                (
                    "fail",
                    "timeout",
                    f"decide() took {elapsed_ms:.0f}ms, exceeds platform limit "
                    f"of {PLATFORM_TIMEOUT_MS}ms",
                )
            )
        elif elapsed_ms > timeout_warn_ms:
            results.append(
                (
                    "warn",
                    "timeout",
                    f"decide() took {elapsed_ms:.0f}ms (platform limit: {PLATFORM_TIMEOUT_MS}ms). "
                    f"Consider optimizing for consistent sub-{timeout_warn_ms}ms performance.",
                )
            )
        else:
            results.append(
                (
                    "pass",
                    "timeout",
                    f"decide() completed in {elapsed_ms:.1f}ms",
                )
            )

    except Exception as e:
        results.append(
            (
                "fail",
                "smoke_test",
                f"Failed to run bot: {type(e).__name__}: {e}",
            )
        )
    finally:
        if added_to_path and bot_dir_str in sys.path:
            sys.path.remove(bot_dir_str)
        # Clean up imported module
        if module_name in sys.modules:
            del sys.modules[module_name]

    return results


def validate_cli(args: list[str] | None = None) -> None:
    """CLI entry point for the validate command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="chipzen-sdk validate",
        description="Validate a bot before uploading to the Chipzen platform",
        epilog=(
            "Checks performed:\n"
            "  file_structure         Entry point file exists and is valid Python\n"
            "  syntax                 Python syntax check\n"
            "  imports                Scan for blocked/restricted imports\n"
            "  bot_class              Class inheriting from ChipzenBot/Bot exists\n"
            "  decide_method          Bot class implements decide()\n"
            "  requirements           Packages are in the platform allow-list\n"
            "  smoke_test             Bot can be instantiated and returns an Action\n"
            "  timeout                decide() completes within time limits\n"
            "  size                   Artifact size within upload limits\n"
            "  connectivity_full_match    (with --check-connectivity) Drive the bot\n"
            "                             through a canned handshake + 1 hand + match_end\n"
            "                             via an in-process mock WebSocket\n"
            "  multi_turn_request_id_echo (with --check-connectivity) Drive 3 turn_requests\n"
            "                             across preflop/flop/turn and verify request_id\n"
            "                             is echoed correctly on each\n"
            "  action_rejected_recovery   (with --check-connectivity) Verify the SDK\n"
            "                             retries with a safe-fallback action when the\n"
            "                             server sends action_rejected\n"
            "  retry_storm_bounded        (with --check-connectivity) Verify the SDK\n"
            "                             responds reactively to 3 back-to-back\n"
            "                             action_rejected messages without hanging\n"
            "\n"
            "The validator is a courtesy linter: it catches the most common\n"
            "upload-blocking issues before you ship. The authoritative gate is\n"
            "server-side -- the platform runs its own seccomp + cap-drop sandbox\n"
            "on bot containers and re-checks size, imports, and conformance.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        help="Path to bot directory or .zip file",
    )
    parser.add_argument(
        "--entry-point",
        default=None,
        help="Override entry point filename (default: auto-detect main.py or bot.py)",
    )
    parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_UPLOAD_BYTES // (1024 * 1024),
        help=(f"Maximum upload size in MB (default: {DEFAULT_MAX_UPLOAD_BYTES // (1024 * 1024)})"),
    )
    parser.add_argument(
        "--timeout-warn-ms",
        type=int,
        default=DEFAULT_TIMEOUT_WARN_MS,
        help=f"Warn if decide() takes longer than this in ms (default: {DEFAULT_TIMEOUT_WARN_MS})",
    )
    parser.add_argument(
        "--check-connectivity",
        action="store_true",
        help=(
            "Drive the bot through 4 canned protocol scenarios via an "
            "in-process mock WebSocket (handshake + 1 hand, multi-turn "
            "request_id echo, action_rejected recovery, retry-storm "
            "reactivity). Pure connectivity / wire-protocol conformance — "
            "no judgement of strategy strength."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    parsed = parser.parse_args(args)

    results = validate_bot(
        parsed.path,
        max_upload_bytes=parsed.max_size_mb * 1024 * 1024,
        timeout_warn_ms=parsed.timeout_warn_ms,
        entry_point=parsed.entry_point,
        check_connectivity=parsed.check_connectivity,
    )

    _print_results(results, color=not parsed.no_color)

    # Exit with non-zero if any failures
    has_failures = any(sev == "fail" for sev, _, _ in results)
    if has_failures:
        sys.exit(1)


def _print_results(results: list[tuple[Severity, str, str]], *, color: bool = True) -> None:
    """Print validation results with optional color."""
    if color and _supports_color():
        GREEN = "\033[92m"
        YELLOW = "\033[93m"
        RED = "\033[91m"
        RESET = "\033[0m"
        BOLD = "\033[1m"
    else:
        GREEN = YELLOW = RED = RESET = BOLD = ""

    icons = {
        "pass": f"{GREEN}PASS{RESET}",
        "warn": f"{YELLOW}WARN{RESET}",
        "fail": f"{RED}FAIL{RESET}",
    }

    print(f"\n{BOLD}Chipzen Bot Validation{RESET}")
    print("=" * 50)

    for severity, check_name, message in results:
        icon = icons.get(severity, severity)
        print(f"  [{icon}] {check_name}: {message}")

    # Summary
    warns = sum(1 for s, _, _ in results if s == "warn")
    fails = sum(1 for s, _, _ in results if s == "fail")

    print()
    if fails == 0:
        if warns > 0:
            print(f"{GREEN}All checks passed{RESET} ({warns} warning{'s' if warns != 1 else ''})")
        else:
            print(f"{GREEN}All checks passed!{RESET} Your bot is ready to upload.")
    else:
        suffix = "s" if fails != 1 else ""
        print(f"{RED}{fails} check{suffix} failed.{RESET} Fix the issues above before uploading.")


def _supports_color() -> bool:
    """Check if the terminal supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False
