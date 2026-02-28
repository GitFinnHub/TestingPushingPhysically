"""
push-to-push
---------------------------------------------------
Physically push your laptop BACKWARD -> automatically push your code to GitHub.

Detection strategy:
  DepthAnything V2 (via Hugging Face transformers) - estimates depth from the
  built-in webcam every few seconds. When the scene suddenly gets farther
  away (average depth jumps), it means the camera moved backward -> git push!

Usage:
    python test.py --repo "C:/path/to/your/repo"
    python test.py --repo . --branch main --sensitivity 0.12
    python test.py --demo          # simulates a push without moving
"""

import argparse
from typing import Union, Any, cast
import time
import threading
import subprocess
import math
import logging
import sys
from datetime import datetime
from pathlib import Path

# --- optional deps --------------------------------------------------------------
try:
    import git  # type: ignore[import-untyped]
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False

try:
    import cv2  # type: ignore[import-untyped]
    OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None
    OPENCV_AVAILABLE = False

try:
    import torch  # type: ignore[import-untyped]
    TORCH_AVAILABLE = True
except ImportError:
    torch = None
    TORCH_AVAILABLE = False

try:
    from PIL import Image  # type: ignore[import-untyped]
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

try:
    from transformers import pipeline as hf_pipeline  # type: ignore[import-untyped]
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    hf_pipeline = None
    TRANSFORMERS_AVAILABLE = False

try:
    import numpy as np  # type: ignore[import-untyped]
    NUMPY_AVAILABLE = True
except ImportError:
    np = None
    NUMPY_AVAILABLE = False


# --- logging setup --------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("push-to-push")


# ==============================================================================
#  GIT OPERATIONS
# ==============================================================================

def git_push(repo_path: Union[str, Path], branch: str, commit_message: str) -> bool:
    """Stage all changes, commit, and push. Returns True on success."""
    resolved: Path = Path(repo_path).resolve()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"{commit_message} [{timestamp}]"

    log.info("[GIT] Staging changes in %s ...", resolved)

    if GITPYTHON_AVAILABLE:
        return _git_push_gitpython(resolved, branch, full_message)
    else:
        return _git_push_subprocess(resolved, branch, full_message)


def _git_push_gitpython(repo_path: Path, branch: str, message: str) -> bool:
    try:
        repo = git.Repo(repo_path)  # type: ignore[union-attr]

        repo.git.add("--all")

        if not repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            log.info("[OK] Nothing new to commit - already up to date.")
            return True

        repo.index.commit(message)
        log.info("[OK] Committed: %s", message)

        origin = repo.remote(name="origin")
        origin.push(refspec=f"{branch}:{branch}")
        log.info("[PUSH] Pushed to origin/%s", branch)
        return True

    except git.InvalidGitRepositoryError:  # type: ignore[union-attr]
        log.error("[ERROR] Not a git repository: %s", repo_path)
        return False
    except git.GitCommandError as exc:  # type: ignore[union-attr]
        log.error("[ERROR] Git error: %s", exc)
        return False
    except Exception as exc:
        log.error("[ERROR] Unexpected error: %s", exc)
        return False


def _git_push_subprocess(repo_path: Path, branch: str, message: str) -> bool:
    def run(cmd):
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, shell=True
        )
        if result.returncode != 0:
            log.error("[ERROR] %s\n%s", " ".join(cmd), result.stderr.strip())
            return False
        return True

    if not run(["git", "add", "--all"]):
        return False

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, shell=True
    )
    if not status.stdout.strip():
        log.info("[OK] Nothing new to commit - already up to date.")
        return True

    if not run(["git", "commit", "-m", message]):
        return False
    log.info("[OK] Committed: %s", message)

    if not run(["git", "push", "origin", branch]):
        return False
    log.info("[PUSH] Pushed to origin/%s", branch)
    return True


# ==============================================================================
#  MOTION DETECTION - Depth-Anything V2 webcam detector
# ==============================================================================

DEPTH_MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

class DepthCameraDetector:
    """
    Detects backward movement of the laptop by watching how the scene depth
    changes over time using DepthAnything V2.

    Algorithm
    ---------
    1. Capture a webcam frame every `poll_ms` ms.
    2. Run DepthAnything V2 to get a depth map (higher value = farther away).
    3. Compute the mean depth of the central 50% crop.
    4. Maintain an exponential moving average (EMA) as a rolling baseline.
    5. If `current_mean > ema_baseline + sensitivity`, fire a push event.

    Parameters
    ----------
    sensitivity  : depth-unit delta required to trigger (default 0.15).
                   Lower -> more sensitive. Typical range 0.08 - 0.30.
    camera_index : which webcam to use (0 = built-in default).
    ema_alpha    : EMA smoothing factor (0 < alpha < 1).
    """

    def __init__(
        self,
        sensitivity: float = 0.15,
        camera_index: int = 0,
        ema_alpha: float = 0.3,
    ):
        self.sensitivity = sensitivity
        self.camera_index = camera_index
        self.ema_alpha = ema_alpha

        self._available = False
        self._depth_pipe: Any = None
        self._cap: Any = None
        self._baseline: Union[float, None] = None  # EMA baseline

        self._check_deps()

    # --- dependency check -----------------------------------------------------

    def _check_deps(self):
        missing = []
        if not OPENCV_AVAILABLE:
            missing.append("opencv-python")
        if not TORCH_AVAILABLE:
            missing.append("torch")
        if not PIL_AVAILABLE:
            missing.append("Pillow")
        if not TRANSFORMERS_AVAILABLE:
            missing.append("transformers")
        if not NUMPY_AVAILABLE:
            missing.append("numpy")

        if missing:
            log.error(
                "[ERROR] Missing packages for webcam depth detection: %s\n"
                "   Run: python -m pip install %s",
                ", ".join(missing),
                " ".join(missing),
            )
            return

        self._available = True

    # --- public interface -----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    def start(self):
        """Open webcam and load model. Call once before the loop."""
        if not self._available:
            return

        # Open webcam
        log.info("[CAM] Opening webcam (index %d) ...", self.camera_index)
        local_cv2 = cast(Any, cv2)
        if local_cv2 is None:
            log.error("[ERROR] OpenCV is not available.")
            self._available = False
            return

        self._cap = local_cv2.VideoCapture(self.camera_index)
        cap = self._cap
        if cap is None or not cap.isOpened():
            log.error(
                "[ERROR] Cannot open webcam %d. Check that no other app is using it.",
                self.camera_index,
            )
            self._available = False
            return
        log.info("[CAM] Webcam opened on index %d", self.camera_index)

        # Load depth model
        log.info("[AI] Loading DepthAnything V2 model (%s) ...", DEPTH_MODEL_ID)
        log.info("   (First run downloads ~100 MB to the HuggingFace cache.)")
        try:
            local_torch = torch
            device = 0 if (TORCH_AVAILABLE and local_torch is not None and local_torch.cuda.is_available()) else -1
            
            local_hf_pipeline = hf_pipeline
            if local_hf_pipeline is None:
                log.error("[ERROR] HuggingFace transformers pipeline is not available.")
                self._available = False
                return
            
            self._depth_pipe = local_hf_pipeline(
                task="depth-estimation",
                model=DEPTH_MODEL_ID,
                device=device,
            )
            log.info("[AI] Depth model loaded (%s) [device=%s]",
                     DEPTH_MODEL_ID, "GPU" if device == 0 else "CPU")
        except Exception as exc:
            log.error("[ERROR] Failed to load depth model: %s", exc)
            self._available = False

    def stop(self):
        """Release webcam resources."""
        if self._cap is not None and self._cap.isOpened():
            self._cap.release()

    def check(self) -> bool:
        """
        Grab one frame, run depth estimation, compare to EMA baseline.
        Returns True if a backward-push is detected.
        """
        if not self._available or self._cap is None or self._depth_pipe is None:
            return False

        # Read frame
        cap = cast(Any, self._cap)
        ret, frame_bgr = cap.read()
        if not ret or frame_bgr is None:
            log.warning("[WARN] Webcam read failed - skipping frame.")
            return False

        # Convert BGR -> PIL RGB for the HF pipeline
        local_cv2 = cast(Any, cv2)
        local_image = cast(Any, Image)
        if local_cv2 is None or local_image is None:
            log.warning("[WARN] OpenCV or Pillow not available for image conversion.")
            # This should technically be caught by self._available, but helps lints
            return False
            
        frame_rgb = local_cv2.cvtColor(frame_bgr, local_cv2.COLOR_BGR2RGB)
        pil_img = local_image.fromarray(frame_rgb)

        # Run depth estimation
        try:
            pipe = self._depth_pipe
            assert pipe is not None
            result = pipe(pil_img)
            depth_tensor = result["depth"]  # PIL Image or numpy
        except Exception as exc:
            log.warning("[WARN] Depth inference failed: %s", exc)
            return False

        # Convert depth to numpy array
        local_np = cast(Any, np)
        if not NUMPY_AVAILABLE or local_np is None:
            log.warning("[WARN] NumPy not available for depth processing.")
            return False
            
        try:
            depth_np = local_np.array(depth_tensor, dtype=float)
        except Exception:
            log.warning("[WARN] Failed to convert depth tensor to numpy array.")
            return False

        # Normalise to [0, 1] range so sensitivity is model-agnostic
        d_min, d_max = depth_np.min(), depth_np.max()
        if d_max - d_min < 1e-6:
            log.debug("[DEPTH] Flat depth map - skipping frame.")
            return False  # flat depth map - boring frame, skip
        depth_norm = (depth_np - d_min) / (d_max - d_min)

        # Centre-crop (middle 50% height x 50% width)
        h, w = depth_norm.shape
        cy, cx = h // 2, w // 2
        crop = depth_norm[cy // 2: cy + cy // 2, cx // 2: cx + cx // 2]
        current_mean = float(crop.mean())

        log.debug("[DEPTH] mean=%.4f baseline=%.4f", current_mean,
                  self._baseline if self._baseline is not None else 0)

        # Initialise EMA baseline on first good frame
        if self._baseline is None:
            self._baseline = current_mean
            log.info("[DEPTH] Baseline initialised at %.4f", self._baseline)
            return False

        # EMA update - adapts slowly to gradual lighting / position changes
        prev_baseline = cast(float, self._baseline) # Type assertion
        self._baseline = (
            self.ema_alpha * current_mean + (1 - self.ema_alpha) * prev_baseline
        )

        # Fire if depth jumped sharply above the old baseline
        delta = current_mean - prev_baseline
        if delta > self.sensitivity:
            log.info(
                "[EVENT] Depth spike! mean=%.4f baseline=%.4f delta=+%.4f (threshold %.4f)",
                current_mean, prev_baseline, delta, self.sensitivity,
            )
            # Reset baseline so we don't retrigger immediately
            self._baseline = current_mean
            return True

        return False


# ==============================================================================
#  MAIN WATCHER LOOP
# ==============================================================================

class PushToPush:
    def __init__(
        self,
        repo_path: str,
        branch: str = "main",
        commit_message: str = "pushed by physical push",
        cooldown_seconds: int = 30,
        poll_ms: int = 2000,
        sensitivity: float = 0.15,
        camera_index: int = 0,
    ):
        self.repo_path = repo_path
        self.branch = branch
        self.commit_message = commit_message
        self.cooldown = cooldown_seconds
        self.poll_ms = poll_ms

        self._last_push_time = 0.0
        self._lock = threading.Lock()

        self._detector = DepthCameraDetector(
            sensitivity=sensitivity,
            camera_index=camera_index,
        )

    def _on_push_detected(self):
        now = time.time()
        with self._lock:
            if now - self._last_push_time < self.cooldown:
                remaining = int(self.cooldown - (now - self._last_push_time))
                log.info("[INFO] Push detected but cooling down (%ds left).", remaining)
                return
            self._last_push_time = now

        log.info("[EVENT] PUSH DETECTED -- running git push ...")
        success = git_push(self.repo_path, self.branch, self.commit_message)
        if success:
            _notify("push-to-push", "Code pushed to GitHub!")
        else:
            _notify("push-to-push", "Git push failed. Check the logs.")

    def run(self):
        if not self._detector.available:
            log.error("[ERROR] Depth detector unavailable. Install missing packages and retry.")
            return

        self._detector.start()

        if not self._detector.available:
            return  # start() flagged a problem

        log.info("[INFO] Listening for physical pushes ... (Ctrl+C to stop)")
        log.info("   Repo        : %s", Path(self.repo_path).resolve())
        log.info("   Branch      : %s", self.branch)
        log.info("   Cooldown    : %ds", self.cooldown)
        log.info("   Sensitivity : %.2f depth units", self._detector.sensitivity)
        log.info("   Poll        : %d ms", self.poll_ms)
        log.info("")
        log.info("   -> Push the laptop BACKWARD to trigger a git push!")

        try:
            while True:
                detected = self._detector.check()

                if detected:
                    threading.Thread(
                        target=self._on_push_detected, daemon=True
                    ).start()

                time.sleep(self.poll_ms / 1000)

        except KeyboardInterrupt:
            log.info("[INFO] Stopped.")
        finally:
            self._detector.stop()


# ==============================================================================
#  DESKTOP NOTIFICATION (best-effort)
# ==============================================================================

def _notify(title: str, message: str):
    try:
        from plyer import notification  # type: ignore[import-untyped]
        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        pass   # notifications are optional


# ==============================================================================
#  DEMO / SHAKE SIMULATION
# ==============================================================================

def run_demo(repo_path: str, branch: str, message: str):
    """Simulate a push event after 3 seconds without needing physical movement."""
    log.info("[DEMO] Demo mode - simulating a physical push in 3 seconds ...")

    watcher = PushToPush(repo_path, branch, message, cooldown_seconds=5)

    def fake_push():
        time.sleep(3)
        log.info("[DEMO] Simulating backward push!")
        watcher._on_push_detected()

    t = threading.Thread(target=fake_push, daemon=True)
    t.start()
    t.join(timeout=30)
    log.info("[OK] Demo complete.")


# ==============================================================================
#  CLI ENTRY POINT
# ==============================================================================

def main():
    _epilog = (
        "Examples:\n"
        "  python test.py --repo C:/my/project\n"
        "  python test.py --repo . --branch dev --sensitivity 0.10\n"
        "  python test.py --repo . --camera 1    # use external webcam\n"
        "  python test.py --demo\n"
    )
    parser = argparse.ArgumentParser(
        description="push-to-push - physically push your laptop BACKWARD to git push",
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
        default="pushed by physical push",
        help="Commit message template",
    )
    parser.add_argument(
        "--cooldown", "-c",
        type=int, default=30,
        help="Seconds to wait between pushes (default: 30)",
    )
    parser.add_argument(
        "--sensitivity", "-s",
        type=float, default=0.15,
        help=(
            "Depth-change sensitivity (default: 0.15).  "
            "Lower = more sensitive (e.g. 0.08).  "
            "Higher = requires a bigger push (e.g. 0.30)."
        ),
    )
    parser.add_argument(
        "--camera",
        type=int, default=0,
        help="Webcam device index (default: 0 = built-in camera)",
    )
    parser.add_argument(
        "--poll",
        type=int, default=2000,
        help="Milliseconds between depth samples (default: 2000 ms)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Simulate a push event without physical movement (for testing)",
    )

    try:
        args = parser.parse_args()
    except SystemExit:
        return

    if args.demo:
        run_demo(args.repo, args.branch, args.message)
    else:
        watcher = PushToPush(
            repo_path=args.repo,
            branch=args.branch,
            commit_message=args.message,
            cooldown_seconds=args.cooldown,
            sensitivity=args.sensitivity,
            camera_index=args.camera,
            poll_ms=args.poll,
        )
        watcher.run()


if __name__ == "__main__":
    main()
