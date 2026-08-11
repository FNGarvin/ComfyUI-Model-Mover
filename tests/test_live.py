"""
Tests for core/live.py — run with: python tests/test_live.py
(no real ComfyUI required; fake_folder_paths.py stands in for it)

On an install with many third-party custom nodes, a category's directory
list isn't guaranteed to be a plain list — some code may register one as a
tuple instead of ComfyUI's own convention. resync_order()/unregister_base_path()
must skip those categories rather than crash the whole directory-management
request over someone else's non-standard entry.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_folder_paths import make_fake_folder_paths  # noqa: E402

_fake = make_fake_folder_paths(tempfile.mkdtemp(prefix="mm_test_"))
sys.modules["folder_paths"] = _fake

from core import config_writer, live  # noqa: E402
from core import directories as directories_module  # noqa: E402


def _reset(base_path):
    _fake.base_path = base_path
    _fake.models_dir = os.path.join(base_path, "models")
    _fake.folder_names_and_paths = {}


def test_resync_order_skips_tuple_registered_categories_without_crashing():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)

    default_dir = os.path.join(_fake.models_dir, "checkpoints")
    os.makedirs(default_dir)
    _fake.folder_names_and_paths["checkpoints"] = ([default_dir], {".safetensors"})

    # Simulate a third-party custom node that registered its category with a
    # tuple instead of a list.
    weird_dir = os.path.join(_fake.models_dir, "weird")
    os.makedirs(weird_dir)
    _fake.folder_names_and_paths["weird"] = ((weird_dir,), set())

    alt_base = os.path.join(root, "alt")
    os.makedirs(os.path.join(alt_base, "checkpoints"))
    config_writer.add_directory(alt_base, "Alt", {"checkpoints": ["checkpoints/"]})
    live.register_dirs("checkpoints", alt_base, ["checkpoints/"])

    # Must not raise, and must leave the tuple-registered category untouched.
    live.resync_order()

    assert isinstance(_fake.folder_names_and_paths["weird"][0], tuple)
    assert _fake.folder_names_and_paths["weird"][0] == (weird_dir,)


def test_resync_order_moves_managed_directory_ahead_of_base_install_on_reorder():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)

    default_dir = os.path.join(_fake.models_dir, "checkpoints")
    os.makedirs(default_dir)
    _fake.folder_names_and_paths["checkpoints"] = ([default_dir], {".safetensors"})

    alt_base = os.path.join(root, "alt")
    os.makedirs(os.path.join(alt_base, "checkpoints"))
    key = config_writer.add_directory(alt_base, "Alt", {"checkpoints": ["checkpoints/"]})
    alt_dir = os.path.join(alt_base, "checkpoints")
    live.register_dirs("checkpoints", alt_base, ["checkpoints/"])
    live.resync_order()
    # Nothing has been reordered yet — base install stays implicitly first.
    assert _fake.folder_names_and_paths["checkpoints"][0] == [default_dir, alt_dir]

    config_writer.reorder_directories(
        [key, config_writer.DEFAULT_DIR_SENTINEL],
        default_base_path=_fake.models_dir,
        default_label="Default (ComfyUI install)",
    )
    live.resync_order()
    assert _fake.folder_names_and_paths["checkpoints"][0] == [alt_dir, default_dir]


def test_unregister_base_path_skips_tuple_registered_categories():
    root = tempfile.mkdtemp(prefix="mm_test_")
    _reset(root)

    weird_dir = os.path.join(_fake.models_dir, "weird")
    os.makedirs(weird_dir)
    _fake.folder_names_and_paths["weird"] = ((weird_dir,), set())

    live.unregister_base_path(root)  # must not raise
    assert _fake.folder_names_and_paths["weird"][0] == (weird_dir,)


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
