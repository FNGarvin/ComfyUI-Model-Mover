"""
Tests for core/config_writer.py — run with: python tests/test_config_writer.py
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


def _fresh_root():
    d = tempfile.mkdtemp(prefix="mm_test_")
    _fake.base_path = d
    return d


def test_add_directory_creates_block_and_label():
    root = _fresh_root()
    yaml_key = config_writer.add_directory(
        os.path.join(root, "alt"), "Alt Drive", {"checkpoints": ["checkpoints/"]}
    )
    assert yaml_key.startswith(config_writer.MANAGED_KEY_PREFIX)
    directories = config_writer.read_managed_directories()
    assert len(directories) == 1
    assert directories[0]["yaml_key"] == yaml_key
    assert directories[0]["label"] == "Alt Drive"
    assert directories[0]["categories"]["checkpoints"] == ["checkpoints/"]


def test_user_comments_and_unrelated_blocks_survive_a_write():
    root = _fresh_root()
    yaml_path = os.path.join(root, "extra_model_paths.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(
            "# my hand-written note\n"
            "a1111:\n"
            "    base_path: /somewhere\n"
            "    checkpoints: models/Stable-diffusion  # keep me\n"
        )
    config_writer.add_directory(os.path.join(root, "alt"), "Alt", {"loras": ["loras/"]})
    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "# my hand-written note" in content
    assert "a1111:" in content
    assert "# keep me" in content
    assert config_writer.MANAGED_KEY_PREFIX in content


def test_remove_directory_deletes_block_and_label():
    root = _fresh_root()
    key = config_writer.add_directory(os.path.join(root, "alt"), "Alt", {"vae": ["vae/"]})
    config_writer.remove_directory(key)
    assert config_writer.read_managed_directories() == []


def test_reorder_directories_changes_file_order():
    root = _fresh_root()
    key1 = config_writer.add_directory(os.path.join(root, "a"), "A", {"vae": ["vae/"]})
    key2 = config_writer.add_directory(os.path.join(root, "b"), "B", {"vae": ["vae/"]})
    assert [d["yaml_key"] for d in config_writer.read_managed_directories()] == [key1, key2]

    config_writer.reorder_directories(
        [key2, key1],
        default_base_path=os.path.join(root, "models"),
        default_label="Default (ComfyUI install)",
    )
    assert [d["yaml_key"] for d in config_writer.read_managed_directories()] == [key2, key1]


def test_reorder_directories_with_default_sentinel_creates_and_positions_anchor():
    root = _fresh_root()
    default_base = os.path.join(root, "models")
    key1 = config_writer.add_directory(os.path.join(root, "a"), "A", {"vae": ["vae/"]})
    key2 = config_writer.add_directory(os.path.join(root, "b"), "B", {"vae": ["vae/"]})

    config_writer.reorder_directories(
        [key1, config_writer.DEFAULT_DIR_SENTINEL, key2],
        default_base_path=default_base,
        default_label="Default (ComfyUI install)",
    )
    directories = config_writer.read_managed_directories()
    keys_in_order = [d["yaml_key"] for d in directories]
    assert keys_in_order[0] == key1
    assert keys_in_order[2] == key2
    anchor_key = keys_in_order[1]
    assert anchor_key not in (key1, key2)
    anchor = next(d for d in directories if d["yaml_key"] == anchor_key)
    assert anchor["base_path"] == os.path.normpath(default_base)

    # A second reorder naming the same default base_path reuses the same
    # anchor block rather than creating a second one.
    config_writer.reorder_directories(
        [config_writer.DEFAULT_DIR_SENTINEL, key1, key2],
        default_base_path=default_base,
        default_label="Default (ComfyUI install)",
    )
    directories2 = config_writer.read_managed_directories()
    assert [d["yaml_key"] for d in directories2][0] == anchor_key


def test_reorder_directories_strips_stale_is_default_flag():
    root = _fresh_root()
    yaml_path = os.path.join(root, "extra_model_paths.yaml")
    key = f"{config_writer.MANAGED_KEY_PREFIX}legacy"
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(f"{key}:\n    base_path: {os.path.join(root, 'alt')}\n    is_default: true\n    vae: vae/\n")

    config_writer.reorder_directories(
        [key],
        default_base_path=os.path.join(root, "models"),
        default_label="Default (ComfyUI install)",
    )
    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "is_default" not in content


def test_track_subdir_appends_to_existing_block():
    root = _fresh_root()
    key = config_writer.add_directory(os.path.join(root, "alt"), "Alt", {"vae": ["vae/"]})
    returned_key = config_writer.track_subdir(
        yaml_key=key,
        base_path=os.path.join(root, "alt"),
        label="Alt",
        category="seedvr2",
        relpath="seedvr2/",
    )
    assert returned_key == key
    directories = config_writer.read_managed_directories()
    assert directories[0]["categories"]["seedvr2"] == ["seedvr2/"]
    # original category untouched
    assert directories[0]["categories"]["vae"] == ["vae/"]


def test_track_subdir_for_default_directory_creates_companion_block():
    root = _fresh_root()
    base = os.path.join(root, "models")
    returned_key = config_writer.track_subdir(
        yaml_key=None,
        base_path=base,
        label="Default (ComfyUI install)",
        category="seedvr2",
        relpath="seedvr2/",
    )
    directories = config_writer.read_managed_directories()
    assert len(directories) == 1
    assert directories[0]["yaml_key"] == returned_key
    assert directories[0]["base_path"] == os.path.normpath(base)
    assert directories[0]["categories"]["seedvr2"] == ["seedvr2/"]

    # A second, different category for the same default base_path should
    # reuse the same companion block rather than creating another one.
    returned_key_2 = config_writer.track_subdir(
        yaml_key=None,
        base_path=base,
        label="Default (ComfyUI install)",
        category="ultralytics",
        relpath="ultralytics/",
    )
    assert returned_key_2 == returned_key
    directories = config_writer.read_managed_directories()
    assert len(directories) == 1
    assert directories[0]["categories"]["ultralytics"] == ["ultralytics/"]


def test_read_managed_directories_ignores_foreign_blocks():
    root = _fresh_root()
    yaml_path = os.path.join(root, "extra_model_paths.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("a1111:\n    base_path: /somewhere\n    checkpoints: models/Stable-diffusion\n")
    assert config_writer.read_managed_directories() == []


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
