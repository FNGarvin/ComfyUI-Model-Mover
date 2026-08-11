"""
Applies directory-management changes (add/remove/track/default/reorder) to
the live, in-process folder_paths registry so they take effect immediately,
with no ComfyUI restart — confirmed achievable by tracing folder_paths.py's
own cache-invalidation logic (see the plan's "Key research findings").
Persisting those same changes to extra_model_paths.yaml is config_writer.py's
job; this module only ever mutates process memory.

resync_order() is the one non-obvious piece: rather than trying to patch
list positions incrementally for every possible operation (add/remove/
reorder), it re-derives each category's whole directory order from scratch
every time, as a concatenation of per-directory "segments" in the priority
order directories.discover_directories() already returns.

Each directory's *internal* dir order (e.g. `unet` before `diffusion_models`
within one directory's own segment) is left exactly as-is — segments are
built by filtering the CURRENT list for "dirs under this directory's
base_path", which preserves whatever relative order those dirs already had.
This avoids ever needing to know or reconstruct a "pristine" hardcoded order.
"""

from __future__ import annotations

import os

import folder_paths

from . import directories as directories_module
from .pathutil import is_under


def register_dirs(category: str, base_path: str, relpaths: list[str]) -> None:
    """Create (if needed) and register each relpath under base_path for
    `category`. Always appends (is_default handling is resync_order()'s job,
    not this function's — keeps the two concerns separate)."""
    for relpath in relpaths:
        full = os.path.normpath(os.path.join(base_path, relpath.replace("/", os.sep)))
        os.makedirs(full, exist_ok=True)
        folder_paths.add_model_folder_path(category, full, is_default=False)


def unregister_base_path(base_path: str) -> None:
    """Strip every directory under base_path from every category's live list
    (used when a managed directory is removed). Files are never touched."""
    for dirs, _exts in folder_paths.folder_names_and_paths.values():
        # Some third-party custom node may have registered a category with a
        # tuple instead of ComfyUI's own list convention — those entries
        # can't be reordered/pruned in place, so leave them exactly as-is
        # rather than crash the whole operation over someone else's category.
        if not isinstance(dirs, list):
            continue
        dirs[:] = [d for d in dirs if not is_under(d, base_path)]


def resync_order() -> None:
    ordered_dirs = directories_module.discover_directories()  # already priority-sorted

    for dirs, _exts in folder_paths.folder_names_and_paths.values():
        # Same defensive skip as unregister_base_path() above — a category
        # registered as a tuple by other code can't be reordered in place.
        if not isinstance(dirs, list):
            continue
        claimed: set[str] = set()
        segments: list[list[str]] = []
        for directory in ordered_dirs:
            seg = [d for d in dirs if d not in claimed and is_under(d, directory.base_path)]
            claimed.update(seg)
            segments.append(seg)
        leftover = [d for d in dirs if d not in claimed]
        dirs[:] = [d for seg in segments for d in seg] + leftover
