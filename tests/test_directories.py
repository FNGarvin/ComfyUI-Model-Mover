"""
Tests for core/directories.py — run with: python tests/test_directories.py
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

from core import config_writer  # noqa: E402
from core import directories as directories_module  # noqa: E402


def _fresh_root():
    d = tempfile.mkdtemp(prefix="mm_test_")
    _fake.base_path = d
    _fake.models_dir = os.path.join(d, "models")
    os.makedirs(_fake.models_dir, exist_ok=True)
    return d


def test_companion_block_for_default_dir_is_not_a_separate_directory():
    """track_subdir(yaml_key=None, ...) creates a yaml block sharing the
    default directory's own base_path purely to persist extra category
    registrations (see config_writer.track_subdir). discover_directories()
    must not surface that block as if it were an independent, removable
    directory — it's the same location as "default", just carrying
    bookkeeping."""
    _fresh_root()
    config_writer.track_subdir(
        yaml_key=None,
        base_path=_fake.models_dir,
        label="Default (ComfyUI install)",
        category="onnx",
        relpath="onnx/",
    )

    dirs = directories_module.discover_directories()
    assert len(dirs) == 1
    assert dirs[0].id == directories_module.DEFAULT_DIR_ID
    assert directories_module.get_directory("model_mover_directory_default_comfyui_install_categories") is None


def test_real_managed_directory_still_appears_normally():
    root = _fresh_root()
    config_writer.add_directory(os.path.join(root, "alt"), "Alt", {"vae": ["vae/"]})

    dirs = directories_module.discover_directories()
    ids = {d.id for d in dirs}
    assert directories_module.DEFAULT_DIR_ID in ids
    assert len(ids) == 2  # default + the one real managed directory


def test_discover_directories_defaults_base_install_first_without_reorder():
    root = _fresh_root()
    key = config_writer.add_directory(os.path.join(root, "alt"), "Alt", {"vae": ["vae/"]})
    dirs = directories_module.discover_directories()
    assert [d.id for d in dirs] == [directories_module.DEFAULT_DIR_ID, key]


def test_discover_directories_reflects_reorder_including_base_install():
    root = _fresh_root()
    key1 = config_writer.add_directory(os.path.join(root, "a"), "A", {"vae": ["vae/"]})
    key2 = config_writer.add_directory(os.path.join(root, "b"), "B", {"vae": ["vae/"]})

    config_writer.reorder_directories(
        [key1, key2, directories_module.DEFAULT_DIR_ID],
        default_base_path=_fake.models_dir,
        default_label="Default (ComfyUI install)",
    )
    dirs = directories_module.discover_directories()
    assert [d.id for d in dirs] == [key1, key2, directories_module.DEFAULT_DIR_ID]


def test_discover_directories_still_reads_legacy_store_prefixed_blocks():
    """Blocks written by a pre-rename version of this tool (key prefix
    model_mover_store_ instead of model_mover_directory_) must keep working
    without any manual migration — see config_writer's _LEGACY_KEY_PREFIX."""
    root = _fresh_root()
    yaml_path = os.path.join(root, "extra_model_paths.yaml")
    legacy_key = "model_mover_store_alt"
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(f"{legacy_key}:\n    base_path: {os.path.join(root, 'alt')}\n    vae: vae/\n")

    dirs = directories_module.discover_directories()
    ids = {d.id for d in dirs}
    assert legacy_key in ids
    assert directories_module.DEFAULT_DIR_ID in ids


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
