#!/usr/bin/env python3
"""
Pre-flight check for PiKaraoke.

Runs smoke tests and displays each result live on the pygame splash screen.
If any fail, the screen stays up so the user can read what went wrong.

Exit codes:
  0 — all checks passed
  1 — one or more checks failed (errors shown on screen)
"""
import os
import subprocess
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
YTDLP_TEST_NAMES = {"test_ytdlp_installed", "test_ytdlp_version_recent"}

# Colors
COLOR_BG = (0, 0, 0)
COLOR_TITLE = (10, 114, 186)
COLOR_PASS = (34, 154, 91)
COLOR_FAIL = (255, 70, 70)
COLOR_RUNNING = (255, 200, 80)
COLOR_WHITE = (255, 255, 255)
COLOR_GRAY = (150, 150, 150)


# ---------------------------------------------------------------------------
# Pygame screen management
# ---------------------------------------------------------------------------

def init_screen():
    """Initialize a fullscreen pygame display and return (screen, width, height, fonts)."""
    import pygame
    from lib.get_platform import get_platform

    platform = get_platform()

    pygame.display.init()
    pygame.font.init()
    pygame.mouse.set_visible(0)

    info = pygame.display.Info()
    width, height = info.current_w, info.current_h

    if platform in ("raspberry_pi", "linux"):
        os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
        screen = pygame.display.set_mode([width, height], pygame.NOFRAME)
        pygame.display.toggle_fullscreen()
    else:
        screen = pygame.display.set_mode([width, height], pygame.FULLSCREEN)

    fonts = {
        "title": pygame.font.SysFont(pygame.font.get_default_font(), 50),
        "subtitle": pygame.font.SysFont(pygame.font.get_default_font(), 30),
        "item": pygame.font.SysFont(pygame.font.get_default_font(), 28),
        "footer": pygame.font.SysFont(pygame.font.get_default_font(), 24),
    }

    return screen, width, height, fonts


def render_screen(screen, width, height, fonts, title_text, title_color, results, footer_text=None):
    """Redraw the entire screen with current test results.

    results: list of (test_readable_name, status) where status is "PASSED", "FAILED", or "..."
    """
    import pygame

    screen.fill(COLOR_BG)

    # Title
    title = fonts["title"].render(title_text, True, title_color)
    screen.blit(title, (40, 30))

    # Test results list
    y = 100
    for name, status in results:
        if status == "PASSED":
            symbol = "[OK]"
            color = COLOR_PASS
        elif status == "FAILED":
            symbol = "[FAIL]"
            color = COLOR_FAIL
        else:
            symbol = "[....]"
            color = COLOR_RUNNING

        display_name = name if len(name) < 60 else name[:57] + "..."
        line = f"  {symbol}  {display_name}"
        text = fonts["item"].render(line, True, color)
        screen.blit(text, (40, y))
        y += 36
        if y > height - 80:
            break

    # Footer
    if footer_text:
        footer = fonts["footer"].render(footer_text, True, COLOR_GRAY)
        screen.blit(footer, (40, height - 50))

    pygame.display.update()
    # Process events to keep the window responsive
    for event in pygame.event.get():
        pass


def wait_on_screen(screen, width, height, fonts, title_text, title_color, results, timeout=60):
    """Hold the screen with results, wait for ESC or timeout."""
    import pygame

    footer_text = f"Press ESC to exit (auto-close in {timeout}s)"
    start = time.time()
    while (time.time() - start) < timeout:
        render_screen(screen, width, height, fonts, title_text, title_color, results, footer_text)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return
        pygame.time.wait(200)


# ---------------------------------------------------------------------------
# Test runner with live output
# ---------------------------------------------------------------------------

def parse_test_name(line):
    """Extract (raw_name, readable_name) from a pytest verbose output line."""
    parts = line.split("::") if "::" in line else []
    if len(parts) >= 2:
        raw = parts[-1].split(" ")[0].strip()
        readable = raw.replace("test_", "").replace("_", " ")
        return raw, readable
    return None, None


def run_smoke_tests_live(screen, width, height, fonts):
    """Run smoke tests via pytest, updating the screen live for each result.

    Returns (success, failure_names, results).
    """
    import pygame

    # Collect test names first with --collect-only
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-m", "smoke", "--collect-only", "-q", "--no-header"],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    test_names = []
    for line in collect.stdout.splitlines():
        raw, readable = parse_test_name(line)
        if raw and readable:
            test_names.append((raw, readable))

    if not test_names:
        # Fallback: couldn't collect, run without live display
        test_names = [("smoke_tests", "smoke tests")]

    # Initialize all as pending
    results = [(readable, "...") for _, readable in test_names]
    render_screen(screen, width, height, fonts,
                  "PiKaraoke - Running Pre-flight Checks", COLOR_TITLE, results)

    # Run pytest with verbose output, streaming line by line
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests/", "-m", "smoke", "-v", "--tb=no", "--no-header"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=PROJECT_DIR,
    )

    failure_names = []
    current_index = 0

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue

        raw, readable = parse_test_name(line)
        if raw is None:
            continue

        if "PASSED" in line:
            status = "PASSED"
        elif "FAILED" in line:
            status = "FAILED"
            failure_names.append(raw)
        elif "SKIPPED" in line or "SKIP" in line:
            status = "PASSED"  # skipped is fine
        else:
            continue

        # Update the matching result entry
        for i, (name, _) in enumerate(results):
            if name == readable:
                results[i] = (name, status)
                break

        render_screen(screen, width, height, fonts,
                      "PiKaraoke - Running Pre-flight Checks", COLOR_TITLE, results)
        # Small delay so each result is visible on screen
        pygame.time.wait(150)

    proc.wait()
    success = proc.returncode == 0

    return success, failure_names, results


# ---------------------------------------------------------------------------
# yt-dlp auto-repair
# ---------------------------------------------------------------------------

def has_ytdlp_failures(failure_names):
    return bool(set(failure_names) & YTDLP_TEST_NAMES)


def upgrade_ytdlp(screen, width, height, fonts, results):
    """Attempt to upgrade yt-dlp, showing status on screen."""
    # Show upgrading status
    results.append(("Upgrading yt-dlp...", "..."))
    render_screen(screen, width, height, fonts,
                  "PiKaraoke - Auto-repairing yt-dlp", COLOR_RUNNING, results)

    print("Attempting to upgrade yt-dlp...")
    cmds = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
        ["pip3", "install", "--break-system-packages", "--upgrade", "yt-dlp"],
        ["pip3", "install", "--upgrade", "yt-dlp"],
    ]
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                print("yt-dlp upgraded successfully.")
                results[-1] = ("Upgrading yt-dlp...", "PASSED")
                render_screen(screen, width, height, fonts,
                              "PiKaraoke - Auto-repairing yt-dlp", COLOR_RUNNING, results)
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    print("Failed to upgrade yt-dlp.", file=sys.stderr)
    results[-1] = ("Upgrading yt-dlp...", "FAILED")
    render_screen(screen, width, height, fonts,
                  "PiKaraoke - Auto-repairing yt-dlp", COLOR_RUNNING, results)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Running PiKaraoke pre-flight checks...")

    # Try to initialize screen; fall back to headless if pygame unavailable
    try:
        screen, width, height, fonts = init_screen()
        has_screen = True
    except Exception as e:
        print(f"Could not init display ({e}), running headless.", file=sys.stderr)
        has_screen = False

    if has_screen:
        import pygame
        success, failure_names, results = run_smoke_tests_live(screen, width, height, fonts)
    else:
        # Headless fallback
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-m", "smoke", "-v", "--tb=line", "--no-header", "-q"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        failure_names = []
        results = []
        for line in result.stdout.splitlines():
            raw, readable = parse_test_name(line)
            if raw and ("PASSED" in line or "FAILED" in line):
                status = "PASSED" if "PASSED" in line else "FAILED"
                results.append((readable, status))
                if status == "FAILED":
                    failure_names.append(raw)
        success = result.returncode == 0

    if success:
        if has_screen:
            render_screen(screen, width, height, fonts,
                          "PiKaraoke - All Checks Passed!", COLOR_PASS, results,
                          "Starting karaoke...")
            pygame.time.wait(2000)
            pygame.quit()
        print("All pre-flight checks passed.")
        return 0

    # yt-dlp auto-repair
    if has_ytdlp_failures(failure_names):
        print("yt-dlp issue detected, attempting auto-repair...")
        if has_screen:
            upgraded = upgrade_ytdlp(screen, width, height, fonts, results)
        else:
            upgraded = upgrade_ytdlp_headless()

        if upgraded:
            print("Re-running pre-flight checks after yt-dlp upgrade...")
            if has_screen:
                # Reset results for re-run
                success, failure_names, results = run_smoke_tests_live(screen, width, height, fonts)
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "tests/", "-m", "smoke", "-v", "--tb=line", "--no-header", "-q"],
                    capture_output=True, text=True, cwd=PROJECT_DIR,
                )
                failure_names = []
                results = []
                for line in result.stdout.splitlines():
                    raw, readable = parse_test_name(line)
                    if raw and ("PASSED" in line or "FAILED" in line):
                        status = "PASSED" if "PASSED" in line else "FAILED"
                        results.append((readable, status))
                        if status == "FAILED":
                            failure_names.append(raw)
                success = result.returncode == 0

            if success:
                if has_screen:
                    render_screen(screen, width, height, fonts,
                                  "PiKaraoke - All Checks Passed!", COLOR_PASS, results,
                                  "Starting karaoke...")
                    pygame.time.wait(2000)
                    pygame.quit()
                print("All pre-flight checks passed after yt-dlp upgrade.")
                return 0

    # Still failing — show errors and hold
    failure_lines = [name for name, status in results if status == "FAILED"]
    print(f"Pre-flight check FAILED ({len(failure_lines)} issue(s)):", file=sys.stderr)
    for f in failure_lines:
        print(f"  - {f}", file=sys.stderr)

    if has_screen:
        wait_on_screen(screen, width, height, fonts,
                       "PiKaraoke - Pre-flight Check Failed", COLOR_FAIL, results)
        pygame.quit()

    return 1


def upgrade_ytdlp_headless():
    """Headless yt-dlp upgrade (no screen)."""
    print("Attempting to upgrade yt-dlp...")
    cmds = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
        ["pip3", "install", "--break-system-packages", "--upgrade", "yt-dlp"],
        ["pip3", "install", "--upgrade", "yt-dlp"],
    ]
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                print("yt-dlp upgraded successfully.")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print("Failed to upgrade yt-dlp.", file=sys.stderr)
    return False


if __name__ == "__main__":
    sys.exit(main())
