"""
Directory discovery.

A "directory" (formerly called a "store" — renamed for clarity, see
ROADMAP.md/README.md) is a folder that mirrors ComfyUI's own models/ layout:
for every category ComfyUI (or any loaded custom node) knows about, the
directory may have a matching subdirectory. There are two kinds:

- The "default" directory: ComfyUI's own models_dir. Always present, never
  removable. It has no yaml block of its own (it's wired up in Python before
  any yaml loads), so there's nothing to edit *for it* directly — but new
  categories can still be added under it (e.g. a seedvr2/ folder some custom
  node expects), and its priority (see below) can be changed: both are
  carried by a companion managed block that shares the default directory's
  base_path, found or created on demand by
  config_writer._find_or_create_companion_block(). See track_subdir() and
  reorder_directories().
- "Managed" directories: anything we've registered ourselves, tracked as a
  dedicated top-level block in extra_model_paths.yaml (key prefix
  MANAGED_KEY_PREFIX) so we never touch blocks that belong to the user or to
  another tool.

Priority (which directory's copy of a same-named file `folder_paths` finds
first) is expressed purely as list order — no separate "default" flag.
discover_directories() returns directories already sorted highest-priority
first, derived from managed-block order in the yaml (see
read_managed_directories), with the default install's position taken from its
companion block if it has one, or implicitly first (matching ComfyUI's own
startup behavior) if it doesn't.

Category/subdirectory discovery is intentionally NOT a hardcoded schema — see
core/scanner.py, which reads folder_paths.folder_names_and_paths live and also
walks each directory's physical directory tree, so custom-node-contributed
categories (and folders belonging to not-currently-loaded nodes) are never
invisible to this tool.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

import folder_paths

from . import config_writer

DEFAULT_DIR_ID = "default"


@dataclass
class Directory:
    id: str
    label: str
    base_path: str
    is_managed: bool  # False for the default ComfyUI install directory
    yaml_key: Optional[str] = None  # None for the default directory
    free_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    error: Optional[str] = None  # e.g. path no longer exists / not readable

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "base_path": self.base_path,
            "is_managed": self.is_managed,
            "yaml_key": self.yaml_key,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "error": self.error,
        }


def _disk_usage(path: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    try:
        usage = shutil.disk_usage(path)
        return usage.free, usage.total, None
    except OSError as exc:
        return None, None, str(exc)


def get_default_directory() -> Directory:
    base_path = os.path.normpath(folder_paths.models_dir)
    free, total, error = _disk_usage(base_path)
    return Directory(
        id=DEFAULT_DIR_ID,
        label="Default (ComfyUI install)",
        base_path=base_path,
        is_managed=False,
        yaml_key=None,
        free_bytes=free,
        total_bytes=total,
        error=error,
    )


def discover_directories() -> list[Directory]:
    """Return every directory — default install plus managed — freshly read
    from extra_model_paths.yaml and sorted highest-priority first. Always
    call this fresh (no caching) — it's cheap (no file-content scanning here,
    just yaml + disk_usage) and must reflect concurrent edits made through
    this same UI a moment ago."""
    default_dir = get_default_directory()
    managed = config_writer.read_managed_directories()  # file order == priority order

    default_priority: Optional[int] = None
    ordered: list[tuple[int, Directory]] = []
    position = 0
    for entry in managed:
        base_path = os.path.normpath(entry["base_path"])
        if os.path.normcase(base_path) == os.path.normcase(default_dir.base_path):
            # The default install's companion block (see module docstring) —
            # marks its position in priority order rather than being an
            # independent directory in its own right.
            default_priority = position
            position += 1
            continue
        free, total, error = _disk_usage(base_path)
        directory = Directory(
            id=entry["yaml_key"],
            label=entry.get("label") or entry["yaml_key"],
            base_path=base_path,
            is_managed=True,
            yaml_key=entry["yaml_key"],
            free_bytes=free,
            total_bytes=total,
            error=error,
        )
        ordered.append((position, directory))
        position += 1

    # No companion block yet means the default install has never been
    # explicitly reordered — it stays implicitly first, matching ComfyUI's
    # own startup behavior (models_dir is registered before anything in
    # extra_model_paths.yaml).
    ordered.append((-1 if default_priority is None else default_priority, default_dir))
    ordered.sort(key=lambda pair: pair[0])
    return [directory for _, directory in ordered]


def get_directory(dir_id: str) -> Optional[Directory]:
    for directory in discover_directories():
        if directory.id == dir_id:
            return directory
    return None


# Note: yaml_key uniqueness is handled inside config_writer.add_directory()
# itself (it owns the yaml document at write time, so it's the only safe
# place to resolve collisions without a race).
