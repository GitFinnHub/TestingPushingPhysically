"""
push-to-push - Extension Background Worker
---------------------------------------------------
This script is managed by the VS Code extension.
It listens for webcam depth changes and logs event strings that the 
extension parses to trigger git pushes.
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
    import git
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None
    OPENCV_AVAILABLE = False

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    torch = None
    TORCH_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

try:
    from transformers import pipeline as hf_pipeline
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    hf_pipeline = None
    TRANSFORMERS_AVAILABLE = False

try:
    import numpy as np
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

    def _check_deps(self):
        missing = []
        if not OPENCV_AVAILABLE: missing.append("opencv-python")
        if not TORCH_AVAILABLE: missing.append("torch")
        if not PIL_AVAILABLE: missing.append("Pillow")
        if not TRANSFORMERS_AVAILABLE: missing.append("transformers")
        if not NUMPY_AVAILABLE: missing.append("numpy")

        if missing:
            log.error("[ERROR] Missing packages: %s", ", ".join(missing))
            return

        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def start(self):
        if not self._available: return

        log.info("[CAM] Opening webcam (index %d) ...", self.camera_index)
        local_cv2 = cast(Any, cv2)
        if local_cv2 is None:
            log.error("[ERROR] OpenCV is not available.")
            self._available = False
            return

        self._cap = local_cv2.VideoCapture(self.camera_index)
        cap = self._cap
        if cap is None or not cap.isOpened():
            log.error("[ERROR] Cannot open webcam %d.", self.camera_index)
            self._available = False
            return
        log.info("[CAM] Webcam opened on index %d", self.camera_index)

        log.info("[AI] Loading DepthAnything V2 model (%s) ...", DEPTH_MODEL_ID)
        try:
            local_torch = torch
            device = 0 if (TORCH_AVAILABLE and local_torch is not None and local_torch.cuda.is_available()) else -1
            local_hf_pipeline = hf_pipeline
            if local_hf_pipeline is None:
                log.error("[ERROR] Transformers not available.")
                self._available = False
                return
            
            self._depth_pipe = local_hf_pipeline(
                task="depth-estimation", model=DEPTH_MODEL_ID, device=device
            )
            log.info("[AI] Depth model loaded [device=%s]", "GPU" if device == 0 else "CPU")
        except Exception as exc:
            log.error("[ERROR] Failed to load model: %s", exc)
            self._available = False

    def stop(self):
        if self._cap is not None and self._cap.isOpened():
            self._cap.release()

    def check(self) -> bool:
        if not self._available or self._cap is None or self._depth_pipe is None:
            return False

        cap = cast(Any, self._cap)
        ret, frame_bgr = cap.read()
        if not ret or frame_bgr is None:
            log.warning("[WARN] Webcam read failed.")
            return False

        local_cv2 = cast(Any, cv2)
        local_image = cast(Any, Image)
        if local_cv2 is None or local_image is None: return False
            
        frame_rgb = local_cv2.cvtColor(frame_bgr, local_cv2.COLOR_BGR2RGB)
        pil_img = local_image.fromarray(frame_rgb)

        try:
            pipe = self._depth_pipe
            assert pipe is not None
            result = pipe(pil_img)
            depth_tensor = result["depth"]
        except Exception as exc:
            log.warning("[WARN] Depth inference failed: %s", exc)
            return False

        local_np = cast(Any, np)
        if not NUMPY_AVAILABLE or local_np is None: return False
            
        try:
            depth_np = local_np.array(depth_tensor, dtype=float)
        except Exception: return False

        d_min, d_max = depth_np.min(), depth_np.max()
        if d_max - d_min < 1e-6: return False
        depth_norm = (depth_np - d_min) / (d_max - d_min)

        h, w = depth_norm.shape
        cy, cx = h // 2, w // 2
        crop = depth_norm[cy // 2: cy + cy // 2, cx // 2: cx + cx // 2]
        current_mean = float(crop.mean())

        if self._baseline is None:
            self._baseline = current_mean
            log.info("[DEPTH] Baseline initialised at %.4f", self._baseline)
            return False

        prev_baseline = cast(float, self._baseline)
        self._baseline = (
            self.ema_alpha * current_mean + (1 - self.ema_alpha) * prev_baseline
        )

        delta = current_mean - prev_baseline
        if delta > self.sensitivity:
            log.info("[EVENT] PUSH DETECTED! delta=+%.4f", delta)
            self._baseline = current_mean
            return True

        return False


# ==============================================================================
#  MAIN LOOP
# ==============================================================================

class PushToPush:
    def __init__(self, repo_path: str, branch: str, commit_message: str, 
                 cooldown_seconds: int, poll_ms: int, sensitivity: float, camera_index: int):
        self.repo_path = repo_path
        self.branch = branch
        self.commit_message = commit_message
        self.cooldown = cooldown_seconds
        self.poll_ms = poll_ms
        self._last_push_time = 0.0
        self._lock = threading.Lock()
        self._detector = DepthCameraDetector(sensitivity=sensitivity, camera_index=camera_index)

    def _on_push_detected(self):
        now = time.time()
        with self._lock:
            if now - self._last_push_time < self.cooldown:
                log.info("[INFO] Cooling down.")
                return
            self._last_push_time = now

        log.info("[EVENT] PUSH DETECTED -- running git push ...")
        success = git_push(self.repo_path, self.branch, self.commit_message)
        if success:
            log.info("[PUSH] Pushed to origin/%s", self.branch)

    def run(self):
        if not self._detector.available: return
        self._detector.start()
        if not self._detector.available: return

        log.info("[INFO] Listening for pushes...")
        try:
            while True:
                if self._detector.check():
                    threading.Thread(target=self._on_push_detected, daemon=True).start()
                time.sleep(self.poll_ms / 1000)
        except KeyboardInterrupt:
            log.info("[INFO] Stopped.")
        finally:
            self._detector.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="pushed by physical push")
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--sensitivity", type=float, default=0.15)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--poll", type=int, default=2000)
    
    args = parser.parse_args()
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
