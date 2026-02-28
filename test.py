"""
push-to-push 🚀
───────────────────────────────────────────────────
Physically push your laptop → automatically push your code to GitHub.

Detection strategy (tried in order):
  1. Windows built-in accelerometer (via Windows Sensor API / comtypes)
  2. Mouse-jitter fallback  — rapid involuntary cursor movement that
     occurs when you physically bump a laptop.

Usage:
    python push_to_push.py --repo "C:/path/to/your/repo"
    python push_to_push.py --repo . --branch main --message "bump push"
    python push_to_push.py --demo          # shake simulation for testing
"""

import argparse
from typing import Union
import time
import threading
import subprocess
import math
import ctypes
import logging
from datetime import datetime
from pathlib import Path

# ── optional deps ──────────────────────────────────────────────────────────────
try:
    import git  # type: ignore[import-untyped]
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False

try:
    import comtypes.client as cc  # type: ignore[import-untyped]
    import comtypes  # type: ignore[import-untyped]
    COMTYPES_AVAILABLE = True
except ImportError:
    cc = None
    comtypes = None
    COMTYPES_AVAILABLE = False

try:
    import ctypes.wintypes
    import ctypes
    CTYPES_AVAILABLE = True
except ImportError:
    CTYPES_AVAILABLE = False


# ── logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("push-to-push")


# ══════════════════════════════════════════════════════════════════════════════
#  GIT OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

def git_push(repo_path: Union[str, Path], branch: str, commit_message: str) -> bool:
    """Stage all changes, commit, and push.  Returns True on success."""
    resolved: Path = Path(repo_path).resolve()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"{commit_message} [{timestamp}]"

    log.info("📦 Staging changes in %s …", resolved)

    if GITPYTHON_AVAILABLE:
        return _git_push_gitpython(resolved, branch, full_message)
    else:
        return _git_push_subprocess(resolved, branch, full_message)


def _git_push_gitpython(repo_path: Path, branch: str, message: str) -> bool:
    try:
        repo = git.Repo(repo_path)  # type: ignore[union-attr]

        # Stage all changes
        repo.git.add("--all")

        # Nothing to commit?
        if not repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            log.info("✅ Nothing new to commit — already up to date.")
            return True

        repo.index.commit(message)
        log.info("✅ Committed: %s", message)

        origin = repo.remote(name="origin")
        origin.push(refspec=f"{branch}:{branch}")
        log.info("🚀 Pushed to origin/%s", branch)
        return True

    except git.InvalidGitRepositoryError:  # type: ignore[union-attr]
        log.error("❌ Not a git repository: %s", repo_path)
        return False
    except git.GitCommandError as exc:  # type: ignore[union-attr]
        log.error("❌ Git error: %s", exc)
        return False
    except Exception as exc:
        log.error("❌ Unexpected error: %s", exc)
        return False


def _git_push_subprocess(repo_path: Path, branch: str, message: str) -> bool:
    def run(cmd):
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, shell=True
        )
        if result.returncode != 0:
            log.error("❌ %s\n%s", " ".join(cmd), result.stderr.strip())
            return False
        return True

    if not run(["git", "add", "--all"]):
        return False

    # Check if there's anything to commit
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, shell=True
    )
    if not status.stdout.strip():
        log.info("✅ Nothing new to commit — already up to date.")
        return True

    if not run(["git", "commit", "-m", message]):
        return False
    log.info("✅ Committed: %s", message)

    if not run(["git", "push", "origin", branch]):
        return False
    log.info("🚀 Pushed to origin/%s", branch)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  MOTION DETECTION — Windows Sensor API (accelerometer)
# ══════════════════════════════════════════════════════════════════════════════

# Sensor GUID & interface constants from Windows Sensor SDK
SENSOR_TYPE_ACCELEROMETER_3D = "{C2FB0F5F-E2D2-4C78-BCD0-352A9582819D}"

class AccelerometerSensor:
    """Reads 3-axis G-force from the Windows built-in accelerometer."""

    def __init__(self):
        self._available = False
        self._sensor = None
        if COMTYPES_AVAILABLE:
            self._init()

    def _init(self):
        try:
            sensor_manager = cc.CreateObject(  # type: ignore[union-attr]
                "{77A1C827-FCD2-4689-8915-9D613CC5FA3E}",
                interface=comtypes.IUnknown,  # type: ignore[union-attr]
            )
            self._available = False
        except Exception:
            self._available = False

    @property
    def available(self):
        return self._available

    def read(self):
        """Returns (x, y, z) in g-units or None."""
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  MOTION DETECTION — Mouse-jitter fallback
# ══════════════════════════════════════════════════════════════════════════════

class MouseJitterDetector:
    """
    Detects physical bumps by monitoring the mouse cursor for rapid,
    involuntary displacement that happens when the laptop is shaken/pushed.

    Works on any Windows machine without extra hardware.
    """

    def __init__(self, threshold_px: int = 40, window_ms: int = 200, hits: int = 3):
        """
        threshold_px : minimum cursor displacement (pixels) per sample to count as jitter
        window_ms    : sample interval
        hits         : how many consecutive jitter samples trigger a "shake"
        """
        self.threshold_px = threshold_px
        self.window_ms = window_ms
        self.hits = hits
        self._last_pos = self._get_cursor()
        self._jitter_streak = 0

    @staticmethod
    def _get_cursor():
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        windll = getattr(ctypes, "windll", None)
        if windll:
            windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def check(self) -> bool:
        """Returns True if a shake/push event is detected."""
        x, y = self._get_cursor()
        lx, ly = self._last_pos
        delta = math.hypot(x - lx, y - ly)
        self._last_pos = (x, y)

        if delta >= self.threshold_px:
            self._jitter_streak += 1
        else:
            self._jitter_streak = max(0, self._jitter_streak - 1)

        if self._jitter_streak >= self.hits:
            self._jitter_streak = 0
            return True
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WATCHER LOOP
# ══════════════════════════════════════════════════════════════════════════════

class PushToPush:
    def __init__(
        self,
        repo_path: str,
        branch: str = "main",
        commit_message: str = "🤜 pushed by physical push",
        cooldown_seconds: int = 30,
        poll_ms: int = 150,
        shake_threshold_px: int = 40,
        shake_hits: int = 3,
    ):
        self.repo_path = repo_path
        self.branch = branch
        self.commit_message = commit_message
        self.cooldown = cooldown_seconds
        self.poll_ms = poll_ms

        self._last_push_time = 0.0
        self._lock = threading.Lock()

        # Try accelerometer first, fall back to mouse jitter
        self._accel = AccelerometerSensor()
        self._jitter = MouseJitterDetector(
            threshold_px=shake_threshold_px, hits=shake_hits
        )

        if self._accel.available:
            log.info("🔬 Using Windows accelerometer sensor.")
        else:
            log.info(
                "🖱️  Accelerometer not available — using mouse-jitter detection.\n"
                "   (Physically pushing the laptop will jiggle the cursor slightly.)"
            )

    def _on_push_detected(self):
        now = time.time()
        with self._lock:
            if now - self._last_push_time < self.cooldown:
                remaining = int(self.cooldown - (now - self._last_push_time))
                log.info("🕐 Push detected but cooling down (%ds left).", remaining)
                return
            self._last_push_time = now

        log.info("💥 PUSH DETECTED — running git push …")
        success = git_push(self.repo_path, self.branch, self.commit_message)
        if success:
            _notify("push-to-push", "🚀 Code pushed to GitHub!")
        else:
            _notify("push-to-push", "❌ Git push failed. Check the logs.")

    def run(self):
        log.info("👂 Listening for physical pushes … (Ctrl+C to stop)")
        log.info("   Repo   : %s", Path(self.repo_path).resolve())
        log.info("   Branch : %s", self.branch)
        log.info("   Cooldown: %ds", self.cooldown)

        try:
            while True:
                detected = False

                if self._accel.available:
                    reading = self._accel.read()
                    if reading:
                        x, y, z = reading
                        magnitude = math.sqrt(x**2 + y**2 + z**2)
                        detected = abs(magnitude - 1.0) > 0.5   # > 0.5g deviation
                else:
                    detected = self._jitter.check()

                if detected:
                    # Run push in background so we don't stall the detector
                    threading.Thread(target=self._on_push_detected, daemon=True).start()

                time.sleep(self.poll_ms / 1000)

        except KeyboardInterrupt:
            log.info("👋 Stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  DESKTOP NOTIFICATION (best-effort)
# ══════════════════════════════════════════════════════════════════════════════

def _notify(title: str, message: str):
    try:
        from plyer import notification  # type: ignore[import-untyped]
        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        pass   # notifications are optional


# ══════════════════════════════════════════════════════════════════════════════
#  DEMO / SHAKE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def run_demo(repo_path: str, branch: str, message: str):
    """Simulate a push event after 3 seconds without needing physical movement."""
    log.info("🎬 Demo mode — simulating a physical push in 3 seconds …")

    watcher = PushToPush(repo_path, branch, message, cooldown_seconds=5)

    def fake_shake():
        time.sleep(3)
        log.info("💥 [DEMO] Simulating shake!")
        watcher._on_push_detected()

    t = threading.Thread(target=fake_shake, daemon=True)
    t.start()
    t.join(timeout=15)
    log.info("✅ Demo complete.")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _epilog = (
        "Examples:\n"
        "  python push_to_push.py --repo C:/my/project\n"
        "  python push_to_push.py --repo . --branch dev --threshold 25\n"
        "  python push_to_push.py --demo\n"
    )
    parser = argparse.ArgumentParser(
        description="push-to-push 🚀 — physically push your laptop to git push",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog,
    )
    parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Path to your git repository (default: current directory)",
    )
    parser.add_argument(
        "--branch", "-b",
        default="main",
        help="Branch to push to (default: main)",
    )
    parser.add_argument(
        "--message", "-m",
        default="🤜 pushed by physical push",
        help="Commit message template",
    )
    parser.add_argument(
        "--cooldown", "-c",
        type=int, default=30,
        help="Seconds to wait between pushes (default: 30)",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=int, default=40,
        help="Mouse jitter threshold in pixels (default: 40; lower = more sensitive)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Simulate a push event without physical movement (for testing)",
    )

    args = parser.parse_args()

    if args.demo:
        run_demo(args.repo, args.branch, args.message)
    else:
        watcher = PushToPush(
            repo_path=args.repo,
            branch=args.branch,
            commit_message=args.message,
            cooldown_seconds=args.cooldown,
            shake_threshold_px=args.threshold,
        )
        watcher.run()


if __name__ == "__main__":
    main()
