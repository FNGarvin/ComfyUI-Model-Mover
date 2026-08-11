"""
Inventory scanning: builds the rows the grid displays for a pair of
directories.

Category discovery is live and adaptive (see directories.py's module
docstring for why) — everything here reads folder_paths.folder_names_and_paths
fresh, and also looks for physical subdirectories that exist on disk but
aren't registered as any category yet ("unregistered" folders, surfaced
separately so the UI can offer to track them).

Reuses folder_paths.recursive_search + filter_files_extensions rather than
hand-rolling a file walk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import folder_paths

from .pathutil import is_under
from .directories import Directory

# Windows-only reparse-point attribute; junctions/mount points carry this even
# though os.path.islink() historically only guarantees detecting true
# symlinks. Checking both is a safe superset.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass
class FileEntry:
    size: int
    mtime: float
    is_symlink: bool
    is_hardlink: bool
    nlink: int

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "mtime": self.mtime,
            "is_symlink": self.is_symlink,
            "is_hardlink": self.is_hardlink,
            "nlink": self.nlink,
            "linked": self.is_symlink or self.is_hardlink,
        }


def known_category_dirs_for_directory(directory: Directory) -> dict[str, list[str]]:
    """Categories (and their registered physical dirs) that live under this
    directory's base_path, per the live folder_paths registry."""
    result: dict[str, list[str]] = {}
    for cat, (dirs, _exts) in folder_paths.folder_names_and_paths.items():
        matched = [d for d in dirs if is_under(d, directory.base_path)]
        if matched:
            result[cat] = matched
    return result


def unregistered_subdirs(directory: Directory) -> list[str]:
    """Immediate subdirectories of the directory that aren't part of any
    currently-registered category yet. Used internally to auto-register
    every physical subdirectory as a category the moment it's seen — see
    routes.py's list_directories handler — rather than requiring a manual
    "track this folder" step. Deliberately includes dot-prefixed directories
    (e.g. a `.cache/huggingface` download cache): they can hold as much disk
    space as any real model category, which is exactly what this tool is
    for; the category filter chips are what keep a long list manageable."""
    base = directory.base_path
    if not os.path.isdir(base):
        return []

    known_dirs = set()
    for dirs, _exts in folder_paths.folder_names_and_paths.values():
        for d in dirs:
            known_dirs.add(os.path.normcase(os.path.normpath(d)))

    result = []
    try:
        with os.scandir(base) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                full = os.path.normcase(os.path.normpath(entry.path))
                if full in known_dirs:
                    continue
                result.append(entry.name)
    except OSError:
        pass
    return sorted(result)


def category_dir_link_info(directory: str) -> Optional[dict]:
    """If `directory` itself is a symlink or reparse point (NTFS junction),
    return info about it — a common manual setup worth surfacing distinctly
    from individual linked files."""
    try:
        is_symlink = os.path.islink(directory)
    except OSError:
        is_symlink = False

    is_reparse = False
    if os.name == "nt":
        try:
            st = os.lstat(directory)
            attrs = getattr(st, "st_file_attributes", 0)
            is_reparse = bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            pass

    if not (is_symlink or is_reparse):
        return None

    target = None
    if is_symlink:
        try:
            target = os.readlink(directory)
        except OSError:
            pass
    return {"target": target}


def scan_category_dir(directory: str, extensions) -> dict[str, FileEntry]:
    """relpath (forward-slash normalized) -> FileEntry, for one physical
    category directory."""
    if not os.path.isdir(directory):
        return {}

    files, _dirs = folder_paths.recursive_search(directory, excluded_dir_names=[".git"])
    files = folder_paths.filter_files_extensions(files, extensions)

    result: dict[str, FileEntry] = {}
    for relpath in files:
        full = os.path.join(directory, relpath)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        is_symlink = os.path.islink(full)
        nlink = getattr(st, "st_nlink", 1) or 1
        is_hardlink = (not is_symlink) and nlink > 1
        norm_relpath = relpath.replace(os.sep, "/")
        result[norm_relpath] = FileEntry(
            size=st.st_size,
            mtime=st.st_mtime,
            is_symlink=is_symlink,
            is_hardlink=is_hardlink,
            nlink=nlink,
        )
    return result


def build_inventory(
    dir_a: Directory, dir_b: Directory, categories: Optional[set[str]] = None
) -> dict:
    """Scan both directories and return the grid's data: one row per
    (category, relpath) present in either directory, plus whole-category-
    directory link flags per directory.

    Every registered category is scanned regardless of the `categories`
    filter — it only restricts which categories' rows are included in the
    response. This is deliberate: the returned "categories" list reports only
    categories that actually have a file in one of the two directories right
    now (an empty-but-registered category like a stock `clip/` folder, or a
    `.cache` that was just emptied out by a delete, shouldn't linger in the
    UI's filter chips), and that has to come from a real scan, not just which
    directories happen to be registered."""
    dirs_a = known_category_dirs_for_directory(dir_a)
    dirs_b = known_category_dirs_for_directory(dir_b)
    all_known_categories = set(dirs_a) | set(dirs_b)

    category_links: dict[str, dict[str, dict]] = {dir_a.id: {}, dir_b.id: {}}
    rows = []
    populated_categories: set[str] = set()

    for cat in sorted(all_known_categories):
        extensions = folder_paths.folder_names_and_paths.get(cat, ([], set()))[1]

        files_a: dict[str, FileEntry] = {}
        for d in dirs_a.get(cat, []):
            link_info = category_dir_link_info(d)
            if link_info:
                category_links[dir_a.id][cat] = link_info
            files_a.update(scan_category_dir(d, extensions))

        files_b: dict[str, FileEntry] = {}
        for d in dirs_b.get(cat, []):
            link_info = category_dir_link_info(d)
            if link_info:
                category_links[dir_b.id][cat] = link_info
            files_b.update(scan_category_dir(d, extensions))

        if not files_a and not files_b:
            continue
        populated_categories.add(cat)

        if categories and cat not in categories:
            continue
        for relpath in sorted(set(files_a) | set(files_b)):
            a_entry = files_a.get(relpath)
            b_entry = files_b.get(relpath)
            rows.append(
                {
                    "category": cat,
                    "relpath": relpath,
                    "a": a_entry.to_dict() if a_entry else None,
                    "b": b_entry.to_dict() if b_entry else None,
                    "both": a_entry is not None and b_entry is not None,
                }
            )

    return {
        "categories": sorted(populated_categories),
        "rows": rows,
        "category_links": category_links,
    }
