"""
Reads and writes our own blocks in extra_model_paths.yaml.

Uses ruamel.yaml in round-trip mode so the user's own hand-edited file
(comments, other UI blocks like `a1111:`) is never reformatted or stripped —
we only touch top-level keys we own (prefixed MANAGED_KEY_PREFIX).

Friendly labels are NOT stored in the yaml itself (any extra key inside a
block would be misread by ComfyUI's own loader as a bogus category — see
utils/extra_config.py: everything except base_path/is_default is treated as a
category). Labels live in a small sidecar JSON file next to the yaml instead.

All read functions degrade gracefully (return empty / fall back) if
ruamel.yaml isn't installed yet or the yaml file doesn't exist. Write
functions raise ConfigWriterUnavailable in that case; callers (routes.py)
turn that into a clear, actionable API error rather than a crash.

Naming note: this tool originally called a registered location a "store"
(Steam-Mover-style terminology). It's since been renamed to "directory"
throughout the UI and code as the clearer term. New blocks are written with
MANAGED_KEY_PREFIX ("model_mover_directory_"); blocks already on disk from
before the rename (prefixed with _LEGACY_KEY_PREFIX, "model_mover_store_")
are still recognized on read so nobody's existing extra_model_paths.yaml
breaks — they just never get a fresh block under the new prefix unless
re-created.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

import folder_paths

try:
    from ruamel.yaml import YAML

    _RUAMEL_AVAILABLE = True
except ImportError:
    YAML = None
    _RUAMEL_AVAILABLE = False

MANAGED_KEY_PREFIX = "model_mover_directory_"
_LEGACY_KEY_PREFIX = "model_mover_store_"

_LOCK = threading.Lock()


class ConfigWriterUnavailable(RuntimeError):
    pass


def ruamel_available() -> bool:
    return _RUAMEL_AVAILABLE


def _require_ruamel() -> None:
    if not _RUAMEL_AVAILABLE:
        raise ConfigWriterUnavailable(
            "ruamel.yaml is not installed. Run "
            "\"pip install -r requirements.txt\" inside ComfyUI's Python "
            "environment (see custom_nodes/ComfyUI-Model-Mover), then restart "
            "ComfyUI."
        )


def _is_managed_key(key: str) -> bool:
    return key.startswith(MANAGED_KEY_PREFIX) or key.startswith(_LEGACY_KEY_PREFIX)


def _strip_managed_prefix(key: str) -> str:
    for prefix in (MANAGED_KEY_PREFIX, _LEGACY_KEY_PREFIX):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def _yaml_path() -> str:
    return os.path.join(folder_paths.base_path, "extra_model_paths.yaml")


def _labels_path() -> str:
    return os.path.join(folder_paths.base_path, "model_mover_labels.json")


def _new_yaml() -> "YAML":
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # don't let ruamel line-wrap long paths
    y.indent(mapping=4, sequence=4, offset=2)
    return y


def _load_doc():
    """Returns (yaml, data). Creates an empty, freshly-commented document if
    the file doesn't exist yet — it is NOT written until a save follows."""
    _require_ruamel()
    y = _new_yaml()
    path = _yaml_path()
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = y.load(f)
        if data is None:
            data = y.map()
    else:
        data = y.map()
        data.yaml_set_start_comment(
            "Created by ComfyUI-Model-Mover. Feel free to hand-edit — this "
            "tool only ever touches its own model_mover_directory_* blocks.\n"
        )
    return y, data


def _save_doc(y, data) -> None:
    path = _yaml_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        y.dump(data, f)
    os.replace(tmp_path, path)


def _load_labels() -> dict:
    path = _labels_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_labels(labels: dict) -> None:
    path = _labels_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(labels, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _resolve_base_path(raw: str, yaml_dir: str) -> str:
    p = os.path.expandvars(os.path.expanduser(str(raw)))
    if not os.path.isabs(p):
        p = os.path.abspath(os.path.join(yaml_dir, p))
    return os.path.normpath(p)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_").lower()
    return slug or "directory"


def read_managed_directories() -> list[dict]:
    """Read our blocks from extra_model_paths.yaml, in file order (file order
    is what determines search priority — see folder_paths.add_model_folder_path
    and directories.discover_directories(), which also uses this order to
    place the default install's anchor block among managed directories).
    Recognizes both the current key prefix and the pre-rename legacy one (see
    module docstring). Never raises: returns [] if ruamel isn't installed or
    the file doesn't exist."""
    if not _RUAMEL_AVAILABLE:
        return []
    path = _yaml_path()
    if not os.path.isfile(path):
        return []
    try:
        y = _new_yaml()
        with open(path, "r", encoding="utf-8") as f:
            data = y.load(f) or {}
    except Exception:
        return []

    labels = _load_labels()
    yaml_dir = os.path.dirname(path)
    result = []
    for key in data:
        if not isinstance(key, str) or not _is_managed_key(key):
            continue
        block = data[key] or {}
        raw_base = block.get("base_path")
        if not raw_base:
            continue
        base_path = _resolve_base_path(raw_base, yaml_dir)
        categories = {}
        for cat_key, cat_val in block.items():
            # "is_default" is a leftover key from an older version of this
            # tool (priority is now expressed purely by block order — see
            # reorder_directories()); skip it here too, in case an old yaml
            # still has one lying around.
            if cat_key in ("base_path", "is_default") or cat_val is None:
                continue
            categories[str(cat_key)] = [line for line in str(cat_val).split("\n") if line]
        result.append(
            {
                "yaml_key": key,
                "label": labels.get(key, _strip_managed_prefix(key).replace("_", " ")),
                "base_path": base_path,
                "categories": categories,
            }
        )
    return result


def add_directory(base_path: str, label: str, categories: dict[str, list[str]]) -> str:
    """Create a new managed block. `base_path` must already be absolute
    (routes.py validates this before calling in). Returns the new yaml_key."""
    if not os.path.isabs(base_path):
        raise ValueError("base_path must be absolute")

    with _LOCK:
        y, data = _load_doc()

        existing_keys = {k for k in data if isinstance(k, str)}
        base_slug = MANAGED_KEY_PREFIX + slugify(label)
        yaml_key = base_slug
        n = 2
        while yaml_key in existing_keys:
            yaml_key = f"{base_slug}_{n}"
            n += 1

        block = y.map()
        block["base_path"] = base_path
        for cat, relpaths in categories.items():
            block[cat] = "\n".join(relpaths)
        data[yaml_key] = block

        _save_doc(y, data)

        labels = _load_labels()
        labels[yaml_key] = label
        _save_labels(labels)

    return yaml_key


def remove_directory(yaml_key: str) -> None:
    if not _is_managed_key(yaml_key):
        raise ValueError("refusing to remove a block Model Mover doesn't own")

    with _LOCK:
        y, data = _load_doc()
        if yaml_key in data:
            del data[yaml_key]
            _save_doc(y, data)

        labels = _load_labels()
        if yaml_key in labels:
            del labels[yaml_key]
            _save_labels(labels)


def _find_or_create_companion_block(y, data, base_path: str, label: str) -> str:
    """Find (or create) the managed block that shares `base_path` — used for
    the default ComfyUI-install directory, which owns no block of its own but
    sometimes needs one, either to carry extra category registrations
    (track_subdir) or to act as its priority-order anchor among managed
    directories (reorder_directories). Caller holds _LOCK and an open
    (y, data) doc."""
    yaml_dir = os.path.dirname(_yaml_path())
    norm_base = os.path.normpath(base_path)
    for key in data:
        if not isinstance(key, str) or not _is_managed_key(key):
            continue
        block = data[key] or {}
        raw = block.get("base_path")
        if raw and _resolve_base_path(raw, yaml_dir) == norm_base:
            return key

    target_key = f"{MANAGED_KEY_PREFIX}{slugify(label)}_categories"
    n = 2
    while target_key in data:
        target_key = f"{MANAGED_KEY_PREFIX}{slugify(label)}_categories_{n}"
        n += 1
    block = y.map()
    block["base_path"] = base_path
    data[target_key] = block
    labels = _load_labels()
    labels.setdefault(target_key, f"{label} (additional categories)")
    _save_labels(labels)
    return target_key


def track_subdir(
    *,
    yaml_key: Optional[str],
    base_path: str,
    label: str,
    category: str,
    relpath: str,
) -> str:
    """Add an explicit yaml rule for one physical subdirectory.

    If yaml_key is given, the category line is added/appended to that
    existing managed block. If yaml_key is None (this is the default
    ComfyUI-install directory, which owns no block of its own), a managed
    block sharing that same base_path is found or created to carry the extra
    category registration. Returns the yaml_key that now owns this category.
    """
    with _LOCK:
        y, data = _load_doc()

        target_key = yaml_key if yaml_key is not None else _find_or_create_companion_block(
            y, data, base_path, label
        )

        block = data[target_key]
        existing = block.get(category)
        lines = [line for line in str(existing).split("\n") if line] if existing else []
        if relpath not in lines:
            lines.append(relpath)
        block[category] = "\n".join(lines)

        _save_doc(y, data)

    return target_key


DEFAULT_DIR_SENTINEL = "default"  # matches directories.DEFAULT_DIR_ID


def reorder_directories(
    ordered_ids: list[str], *, default_base_path: str, default_label: str
) -> None:
    """Rewrite block order to match ordered_ids exactly — the complete
    current set of directory ids (routes.py validates this), using
    DEFAULT_DIR_SENTINEL for the base ComfyUI install in place of a yaml_key.
    Block order is what determines search priority for everyone (see
    read_managed_directories/directories.discover_directories/
    live.resync_order); the base install gets/reuses its companion block as
    a position anchor for this, the same block track_subdir() may already use
    to carry extra category registrations."""
    with _LOCK:
        y, data = _load_doc()
        keys = [
            _find_or_create_companion_block(y, data, default_base_path, default_label)
            if sid == DEFAULT_DIR_SENTINEL
            else sid
            for sid in ordered_ids
        ]
        for key in keys:
            if key not in data:
                continue
            block = data[key]
            if isinstance(block, dict) and "is_default" in block:
                del block["is_default"]  # stale flag from an older version of this tool
            value = data.pop(key)
            data[key] = value
        _save_doc(y, data)
