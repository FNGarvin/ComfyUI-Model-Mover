"""
Tests for core/scanner.py — run with: python tests/test_scanner.py
(no real ComfyUI required; fake_folder_paths.py stands in for it)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_folder_paths import make_fake_folder_paths  # noqa: E402

_fake = make_fake_folder_paths(tempfile.mkdtemp(prefix="mm_test_"))
sys.modules["folder_paths"] = _fake

from core import scanner  # noqa: E402
from core.directories import Directory  # noqa: E402


def _reset(base_path):
    _fake.base_path = base_path
    _fake.models_dir = os.path.join(base_path, "models")
    _fake.folder_names_and_paths = {}


def _directory(dir_id, base_path, is_managed=False):
    os.makedirs(base_path, exist_ok=True)
    return Directory(id=dir_id, label=dir_id, base_path=base_path, is_managed=is_managed)


def test_known_category_dirs_for_directory_matches_by_prefix():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    directory_a = _directory("a", os.path.join(root, "a"))
    directory_b = _directory("b", os.path.join(root, "b"))
    dir_a = os.path.join(directory_a.base_path, "checkpoints")
    dir_b = os.path.join(directory_b.base_path, "checkpoints")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    _fake.folder_names_and_paths["checkpoints"] = ([dir_a, dir_b], {".safetensors"})

    assert scanner.known_category_dirs_for_directory(directory_a) == {"checkpoints": [dir_a]}
    assert scanner.known_category_dirs_for_directory(directory_b) == {"checkpoints": [dir_b]}


def test_unregistered_subdirs_finds_untracked_folders_only():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    directory = _directory("a", os.path.join(root, "a"))
    tracked = os.path.join(directory.base_path, "checkpoints")
    untracked = os.path.join(directory.base_path, "seedvr2")
    os.makedirs(tracked)
    os.makedirs(untracked)
    _fake.folder_names_and_paths["checkpoints"] = ([tracked], {".safetensors"})

    assert scanner.unregistered_subdirs(directory) == ["seedvr2"]


def test_unregistered_subdirs_includes_dot_prefixed_folders():
    # Deliberately not excluded — a cache dir can hold as much disk space as
    # any real model category, which is exactly what this tool manages.
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    directory = _directory("a", os.path.join(root, "a"))
    os.makedirs(os.path.join(directory.base_path, "sam2"))
    os.makedirs(os.path.join(directory.base_path, ".cache", "huggingface"))

    assert scanner.unregistered_subdirs(directory) == [".cache", "sam2"]


def test_scan_category_dir_reports_size_and_extension_filter():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    d = os.path.join(root, "checkpoints")
    os.makedirs(d)
    with open(os.path.join(d, "model.safetensors"), "wb") as f:
        f.write(b"x" * 1000)
    with open(os.path.join(d, "notes.txt"), "wb") as f:
        f.write(b"ignored, wrong extension")

    entries = scanner.scan_category_dir(d, {".safetensors"})
    assert set(entries.keys()) == {"model.safetensors"}
    assert entries["model.safetensors"].size == 1000
    assert entries["model.safetensors"].is_symlink is False
    assert entries["model.safetensors"].is_hardlink is False


def test_scan_category_dir_flags_symlinked_file():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    d = os.path.join(root, "checkpoints")
    os.makedirs(d)
    real = os.path.join(d, "real.safetensors")
    with open(real, "wb") as f:
        f.write(b"data")
    link = os.path.join(d, "link.safetensors")
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return  # needs elevated privileges on this platform; nothing to assert

    entries = scanner.scan_category_dir(d, {".safetensors"})
    assert entries["link.safetensors"].is_symlink is True
    assert entries["real.safetensors"].is_symlink is False


def test_scan_category_dir_flags_hardlinked_file():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    d = os.path.join(root, "checkpoints")
    os.makedirs(d)
    original = os.path.join(d, "original.safetensors")
    with open(original, "wb") as f:
        f.write(b"data")
    linked = os.path.join(d, "linked.safetensors")
    try:
        os.link(original, linked)
    except (OSError, NotImplementedError):
        return  # hardlinks unsupported/unprivileged on this platform; nothing to assert

    entries = scanner.scan_category_dir(d, {".safetensors"})
    assert entries["original.safetensors"].is_hardlink is True
    assert entries["linked.safetensors"].is_hardlink is True
    assert entries["original.safetensors"].nlink >= 2


def test_build_inventory_flags_both_and_missing_sides():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    directory_a = _directory("a", os.path.join(root, "a"))
    directory_b = _directory("b", os.path.join(root, "b"))
    dir_a = os.path.join(directory_a.base_path, "loras")
    dir_b = os.path.join(directory_b.base_path, "loras")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    _fake.folder_names_and_paths["loras"] = ([dir_a, dir_b], {".safetensors"})

    with open(os.path.join(dir_a, "shared.safetensors"), "wb") as f:
        f.write(b"a")
    with open(os.path.join(dir_b, "shared.safetensors"), "wb") as f:
        f.write(b"b")
    with open(os.path.join(dir_a, "only_a.safetensors"), "wb") as f:
        f.write(b"only a")

    data = scanner.build_inventory(directory_a, directory_b)
    rows = {r["relpath"]: r for r in data["rows"]}

    assert rows["shared.safetensors"]["both"] is True
    assert rows["shared.safetensors"]["a"] is not None
    assert rows["shared.safetensors"]["b"] is not None

    assert rows["only_a.safetensors"]["both"] is False
    assert rows["only_a.safetensors"]["a"] is not None
    assert rows["only_a.safetensors"]["b"] is None


def test_build_inventory_omits_empty_registered_categories():
    """A category can be a real, registered directory (e.g. a stock `clip/`
    folder, or a `.cache` that just got emptied by a delete) without holding
    any files right now — it shouldn't linger in the reported categories
    list, which drives the UI's filter chips."""
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    dir_a = _directory("a", os.path.join(root, "a"))
    dir_b = _directory("b", os.path.join(root, "b"))
    populated_dir = os.path.join(dir_a.base_path, "loras")
    empty_dir = os.path.join(dir_a.base_path, "clip")
    os.makedirs(populated_dir)
    os.makedirs(empty_dir)
    _fake.folder_names_and_paths["loras"] = ([populated_dir], {".safetensors"})
    _fake.folder_names_and_paths["clip"] = ([empty_dir], {".safetensors"})

    with open(os.path.join(populated_dir, "model.safetensors"), "wb") as f:
        f.write(b"data")

    data = scanner.build_inventory(dir_a, dir_b)
    assert data["categories"] == ["loras"]


def test_build_inventory_categories_field_ignores_the_row_filter():
    """Requesting rows for just one category must not make the reported
    categories list shrink to that same one category — the filter chips need
    to keep showing every populated category regardless of which one is
    currently selected."""
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    dir_a = _directory("a", os.path.join(root, "a"))
    dir_b = _directory("b", os.path.join(root, "b"))
    loras_dir = os.path.join(dir_a.base_path, "loras")
    ckpt_dir = os.path.join(dir_a.base_path, "checkpoints")
    os.makedirs(loras_dir)
    os.makedirs(ckpt_dir)
    _fake.folder_names_and_paths["loras"] = ([loras_dir], {".safetensors"})
    _fake.folder_names_and_paths["checkpoints"] = ([ckpt_dir], {".safetensors"})
    with open(os.path.join(loras_dir, "l.safetensors"), "wb") as f:
        f.write(b"x")
    with open(os.path.join(ckpt_dir, "c.safetensors"), "wb") as f:
        f.write(b"x")

    data = scanner.build_inventory(dir_a, dir_b, categories={"loras"})
    assert data["categories"] == ["checkpoints", "loras"]
    assert {r["category"] for r in data["rows"]} == {"loras"}


def test_category_dir_link_info_detects_symlinked_directory():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)
    real_dir = os.path.join(root, "real_checkpoints")
    os.makedirs(real_dir)
    link_dir = os.path.join(root, "linked_checkpoints")
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # needs elevated privileges on this platform; nothing to assert

    info = scanner.category_dir_link_info(link_dir)
    assert info is not None
    assert scanner.category_dir_link_info(real_dir) is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
