"""
Background move/copy execution.

Safety model (see plan): copy to a `.part` temp file in the destination
directory, hash the source while streaming, re-hash the written `.part` and
compare, THEN atomically os.replace() it into place, and only THEN — for
move, never copy — delete the source. Any failure anywhere leaves the source
untouched and cleans up only our own `.part` file. A same-filesystem move
skips all of that in favor of a single atomic os.replace(), which is strictly
safer (no data is copied at all, so nothing can go wrong mid-transfer).

Jobs run one at a time (ThreadPoolExecutor(max_workers=1)) — friendlier to a
slow/spinning drive than concurrent I/O, and progress is polled by the
frontend the same way comfyui-model-linker's download progress is.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import scanner
from . import directories as directories_module

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-mover")
_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()


class PlanError(ValueError):
    """Raised for a batch validation failure — turned into a 400 by routes.py."""


class _Cancelled(Exception):
    pass


@dataclass
class JobItem:
    category: str
    relpath: str
    source_dir_id: str
    dest_dir_id: Optional[str]  # None for delete
    mode: str  # "move" | "copy" | "delete"
    overwrite: bool = False
    source_path: Optional[str] = None
    dest_path: Optional[str] = None
    size: Optional[int] = None
    status: str = "pending"  # pending|copying|verifying|finalizing|deleting|done|error|cancelled
    bytes_done: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "relpath": self.relpath,
            "source_dir_id": self.source_dir_id,
            "dest_dir_id": self.dest_dir_id,
            "mode": self.mode,
            "overwrite": self.overwrite,
            "size": self.size,
            "status": self.status,
            "bytes_done": self.bytes_done,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    items: list[JobItem]
    verify: bool = True
    status: str = "pending"  # pending|running|done|error|cancelled
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        total = sum(i.size or 0 for i in self.items)
        done = sum(i.bytes_done for i in self.items)
        return {
            "id": self.id,
            "status": self.status,
            "verify": self.verify,
            "total_bytes": total,
            "bytes_done": done,
            "percent": round(done / total * 100.0, 1) if total else 100.0,
            "items": [i.to_dict() for i in self.items],
        }


# --------------------------------------------------------------------------
# Planning (validation only — no filesystem mutation beyond stat())
# --------------------------------------------------------------------------


def _find_source_path(directory, category: str, relpath: str) -> Optional[str]:
    native = os.path.normpath(relpath.replace("/", os.sep))
    for d in scanner.known_category_dirs_for_directory(directory).get(category, []):
        candidate = os.path.join(d, native)
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_dest_path(directory, category: str, relpath: str) -> tuple[str, bool]:
    """Returns (dest_path, already_exists)."""
    native = os.path.normpath(relpath.replace("/", os.sep))
    dirs = scanner.known_category_dirs_for_directory(directory).get(category, [])
    for d in dirs:
        candidate = os.path.join(d, native)
        if os.path.isfile(candidate):
            return candidate, True
    target_dir = dirs[0] if dirs else os.path.join(directory.base_path, category)
    return os.path.join(target_dir, native), False


def plan_items(specs: list[dict]) -> dict:
    """Validate a proposed batch of {category, relpath, source_dir_id,
    mode, dest_dir_id?, overwrite?} dicts. `dest_dir_id` is required for
    move/copy and ignored for delete. Raises PlanError on the first problem
    found. Returns resolved items + free-space warnings."""
    all_dirs = {d.id: d for d in directories_module.discover_directories()}
    resolved = []
    dest_totals: dict[str, int] = {}

    for spec in specs:
        category = spec["category"]
        relpath = spec["relpath"]
        mode = spec.get("mode", "move")
        if mode not in ("move", "copy", "delete"):
            raise PlanError(f"invalid mode {mode!r} for {category}/{relpath}")

        source_dir = all_dirs.get(spec.get("source_dir_id"))
        if source_dir is None:
            raise PlanError(f"unknown source directory for {category}/{relpath}")

        source_path = _find_source_path(source_dir, category, relpath)
        if source_path is None:
            raise PlanError(f"source file not found: {category}/{relpath} in {source_dir.label}")

        try:
            st = os.lstat(source_path)
        except OSError as exc:
            raise PlanError(f"cannot stat source {source_path}: {exc}") from exc
        if os.path.islink(source_path) or (getattr(st, "st_nlink", 1) or 1) > 1:
            raise PlanError(
                f"{category}/{relpath} is a symlink or hard link in {source_dir.label} — "
                "Model Mover doesn't move/copy/delete linked entries automatically"
            )
        size = st.st_size

        if mode == "delete":
            resolved.append(
                {
                    "category": category,
                    "relpath": relpath,
                    "source_dir_id": source_dir.id,
                    "dest_dir_id": None,
                    "mode": mode,
                    "overwrite": False,
                    "source_path": source_path,
                    "dest_path": None,
                    "size": size,
                    "dest_existed": False,
                }
            )
            continue

        dest_dir = all_dirs.get(spec.get("dest_dir_id"))
        if dest_dir is None:
            raise PlanError(f"unknown destination directory for {category}/{relpath}")
        if source_dir.id == dest_dir.id:
            raise PlanError(f"source and destination are the same directory for {category}/{relpath}")

        dest_path, exists = _find_dest_path(dest_dir, category, relpath)
        overwrite = bool(spec.get("overwrite", False))
        if exists and not overwrite:
            raise PlanError(
                f"{category}/{relpath} already exists in {dest_dir.label} "
                "(retry with overwrite=true to replace it)"
            )

        dest_totals[dest_dir.id] = dest_totals.get(dest_dir.id, 0) + size

        resolved.append(
            {
                "category": category,
                "relpath": relpath,
                "source_dir_id": source_dir.id,
                "dest_dir_id": dest_dir.id,
                "mode": mode,
                "overwrite": overwrite,
                "source_path": source_path,
                "dest_path": dest_path,
                "size": size,
                "dest_existed": exists,
            }
        )

    warnings = []
    dest_free = {}
    for dir_id, needed in dest_totals.items():
        directory = all_dirs[dir_id]
        dest_free[dir_id] = directory.free_bytes
        if directory.free_bytes is not None and directory.free_bytes < needed:
            warnings.append(
                f"{directory.label} may not have enough free space "
                f"({directory.free_bytes:,} bytes free, {needed:,} bytes needed)"
            )

    return {
        "items": resolved,
        "total_bytes": sum(i["size"] for i in resolved),
        "dest_free_bytes": dest_free,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _same_device(dir_a: str, dir_b: str) -> bool:
    try:
        return os.stat(dir_a).st_dev == os.stat(dir_b).st_dev
    except OSError:
        return False


def _execute_item(item: JobItem, *, verify: bool, job: "Job") -> None:
    src = item.source_path

    if item.mode == "delete":
        if job.cancel_requested:
            raise _Cancelled()
        item.status = "deleting"
        os.remove(src)
        item.bytes_done = item.size or 0
        return

    dst = item.dest_path
    dest_dir = os.path.dirname(dst)
    os.makedirs(dest_dir, exist_ok=True)

    if item.mode == "move" and _same_device(os.path.dirname(src), dest_dir):
        # Atomic rename: no data is copied, so there is no partial-transfer
        # state to worry about — strictly safer than the streaming path.
        if job.cancel_requested:
            raise _Cancelled()
        item.status = "finalizing"
        os.replace(src, dst)
        item.bytes_done = item.size or 0
        return

    tmp_dst = dst + ".part"
    try:
        item.status = "copying"
        src_hash = hashlib.sha256() if verify else None
        bytes_copied = 0
        with open(src, "rb") as fsrc, open(tmp_dst, "wb") as fdst:
            while True:
                if job.cancel_requested:
                    raise _Cancelled()
                chunk = fsrc.read(_CHUNK_SIZE)
                if not chunk:
                    break
                fdst.write(chunk)
                if src_hash is not None:
                    src_hash.update(chunk)
                bytes_copied += len(chunk)
                item.bytes_done = bytes_copied

        try:
            st = os.stat(src)
            os.utime(tmp_dst, (st.st_atime, st.st_mtime))
        except OSError:
            pass  # cosmetic only — never fail a transfer over this

        if verify:
            item.status = "verifying"
            dst_hash = hashlib.sha256()
            with open(tmp_dst, "rb") as f:
                while True:
                    if job.cancel_requested:
                        raise _Cancelled()
                    chunk = f.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst_hash.update(chunk)
            if dst_hash.hexdigest() != src_hash.hexdigest():
                raise RuntimeError(
                    "checksum mismatch after copy — source and destination both "
                    "left as-is, partial file removed"
                )

        item.status = "finalizing"
        os.replace(tmp_dst, dst)

        if item.mode == "move":
            os.remove(src)
    except BaseException:
        # Whatever went wrong, the only thing we ever clean up is our own
        # temp file — the source is never touched until the copy above is
        # fully verified and finalized.
        if os.path.exists(tmp_dst):
            try:
                os.remove(tmp_dst)
            except OSError:
                pass
        raise


def _run_job(job: Job) -> None:
    job.status = "running"
    for item in job.items:
        if job.cancel_requested:
            item.status = "cancelled"
            continue
        try:
            _execute_item(item, verify=job.verify, job=job)
            item.status = "done"
        except _Cancelled:
            item.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 — surfaced via item.error
            item.status = "error"
            item.error = str(exc)

    statuses = {i.status for i in job.items}
    if "error" in statuses:
        job.status = "error"
    elif job.cancel_requested or "cancelled" in statuses:
        job.status = "cancelled"
    else:
        job.status = "done"


def create_job(
    resolved_items: list[dict],
    verify: bool = True,
    on_complete: Optional[Callable[[], None]] = None,
) -> str:
    """`on_complete`, if given, runs on the job's own background thread right
    after it finishes (whether it succeeded, partially failed, or was
    cancelled). It exists so routes.py can re-run the auto-track pass once a
    move/copy has actually landed a file in a destination directory that
    didn't exist as a known category dir before the job started — otherwise
    that file would be invisible to inventory scanning until something else
    happens to hit GET /directories. Exceptions from it are swallowed: it's a
    best-effort convenience registration, not part of the job's own result."""
    items = [
        JobItem(
            category=i["category"],
            relpath=i["relpath"],
            source_dir_id=i["source_dir_id"],
            dest_dir_id=i["dest_dir_id"],
            mode=i["mode"],
            overwrite=i["overwrite"],
            source_path=i["source_path"],
            dest_path=i["dest_path"],
            size=i["size"],
        )
        for i in resolved_items
    ]
    job = Job(id=uuid.uuid4().hex, items=items, verify=verify)
    with _jobs_lock:
        _jobs[job.id] = job

    def _run_and_notify() -> None:
        _run_job(job)
        if on_complete is not None:
            try:
                on_complete()
            except Exception:
                pass

    _executor.submit(_run_and_notify)
    return job.id


def get_job(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        job.cancel_requested = True
        return True


# --------------------------------------------------------------------------
# Stray temp-file cleanup
# --------------------------------------------------------------------------

# Patterns for temp/partial files left behind by us or by common external
# downloaders/sync tools, so "Clean temp files" is useful for more than just
# our own leftovers — a registered directory is often also where aria2/wget/
# curl/rsync/huggingface_hub/a browser's own download land files directly.
#   *.part            — our own in-progress copies (see _execute_item above),
#                        and also Firefox's own partial-download convention
#   *.aria2           — aria2c's control file for an in-progress download
#   *.tmp             — wget --tmp/curl -o pattern, and a common generic
#                        convention for any interrupted-write temp file
#   *.incomplete      — huggingface_hub's own partial-download convention
#                        (confirmed against the installed package's
#                        file_download.py: `incomplete_path = blob_path +
#                        ".incomplete"`), living right next to the blob it
#                        will become
#   *.crdownload      — Chrome/Chromium/Edge's own partial-download convention
#   .*.XXXXXX-style   — rsync's temp file naming: a dot-prefixed original
#                        name plus a random 6-char suffix, matched by regex
#                        below rather than fnmatch since the suffix is
#                        alphanumeric, not a fixed extension
_TEMP_GLOB_PATTERNS = ("*.part", "*.aria2", "*.tmp", "*.incomplete", "*.crdownload")
# rsync default: ".<original-name>.XXXXXX" where X is any of [0-9A-Za-z_].
# Anchored so it only matches rsync's own convention, not any dotfile that
# happens to end in six word characters.
_RSYNC_TEMP_RE = re.compile(r"^\..+\.[0-9A-Za-z_]{6}$")


def _is_stray_temp_name(fname: str) -> bool:
    if any(fnmatch.fnmatch(fname, pat) for pat in _TEMP_GLOB_PATTERNS):
        return True
    return bool(_RSYNC_TEMP_RE.match(fname))


def _iter_stray_temp_files(max_age_seconds: float):
    """Yield full paths of stray temp/partial files (see _is_stray_temp_name)
    older than max_age_seconds across every known directory's category
    directories — covers anything left behind by a crash (a clean cancel
    already removes its own .part immediately) or by an external downloader/
    sync tool that was interrupted mid-transfer."""
    now = time.time()
    seen_dirs: set[str] = set()
    for directory in directories_module.discover_directories():
        for dirs in scanner.known_category_dirs_for_directory(directory).values():
            for d in dirs:
                if d in seen_dirs or not os.path.isdir(d):
                    continue
                seen_dirs.add(d)
                for root, _subdirs, files in os.walk(d):
                    for fname in files:
                        if not _is_stray_temp_name(fname):
                            continue
                        full = os.path.join(root, fname)
                        try:
                            if now - os.path.getmtime(full) >= max_age_seconds:
                                yield full
                        except OSError:
                            pass


def count_stray_temp_files(max_age_seconds: float = 3600) -> int:
    """Cheap dry-run count, so the UI can tell whether there's anything for
    "Clean temp files" to actually do before the user clicks it."""
    return sum(1 for _ in _iter_stray_temp_files(max_age_seconds))


def cleanup_stray_temp_files(max_age_seconds: float = 3600) -> list[str]:
    """Remove stray temp/partial files (see _is_stray_temp_name) older than
    max_age_seconds across every known directory's category directories."""
    removed: list[str] = []
    for full in _iter_stray_temp_files(max_age_seconds):
        try:
            os.remove(full)
            removed.append(full)
        except OSError:
            pass
    return removed
