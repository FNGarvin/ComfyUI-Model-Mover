"""
Tests for core/mover.py — run with: python tests/test_mover.py
(no real ComfyUI required; fake_folder_paths.py stands in for it)

Covers the safety-critical properties from the plan: move/copy semantics,
conflict/symlink rejection during planning, the same-device atomic-rename
fast path, and — most importantly — that a checksum mismatch or a cancel
leaves the source untouched and cleans up only the .part temp file.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_folder_paths import make_fake_folder_paths  # noqa: E402

_fake = make_fake_folder_paths(tempfile.mkdtemp(prefix="mm_test_"))
sys.modules["folder_paths"] = _fake

from core import config_writer, mover  # noqa: E402
from core import directories as directories_module  # noqa: E402


def _make_two_directories(category="checkpoints"):
    root = tempfile.mkdtemp(prefix="mm_test_")
    _fake.base_path = root
    _fake.models_dir = os.path.join(root, "models")
    _fake.folder_names_and_paths = {}

    default_dir = os.path.join(_fake.models_dir, category)
    os.makedirs(default_dir, exist_ok=True)
    _fake.folder_names_and_paths[category] = ([default_dir], {".safetensors"})

    alt_base = os.path.join(root, "alt")
    alt_dir = os.path.join(alt_base, category)
    os.makedirs(alt_dir, exist_ok=True)
    config_writer.add_directory(alt_base, "Alt", {category: [category + "/"]})
    _fake.folder_names_and_paths[category][0].append(alt_dir)  # simulate live registration

    all_dirs = directories_module.discover_directories()
    default_directory = next(s for s in all_dirs if not s.is_managed)
    alt_directory = next(s for s in all_dirs if s.is_managed)
    return default_directory, alt_directory, default_dir, alt_dir


def _wait_for_job(job_id, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        job = mover.get_job(job_id)
        if job.status in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_move_relocates_file_and_removes_source():
    default_directory, alt_directory, default_dir, alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    payload = os.urandom(1024 * 100)
    with open(src, "wb") as f:
        f.write(payload)

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "move",
    }])
    assert plan["items"][0]["dest_path"] == os.path.join(alt_dir, "model.safetensors")

    job = _wait_for_job(mover.create_job(plan["items"], verify=True))
    assert job.status == "done", job.to_dict()
    assert not os.path.exists(src)
    dst = os.path.join(alt_dir, "model.safetensors")
    assert os.path.exists(dst)
    with open(dst, "rb") as f:
        assert f.read() == payload


def test_copy_leaves_source_in_place():
    default_directory, alt_directory, default_dir, alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    with open(src, "wb") as f:
        f.write(os.urandom(1024 * 50))

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "copy",
    }])
    job = _wait_for_job(mover.create_job(plan["items"], verify=True))
    assert job.status == "done", job.to_dict()
    assert os.path.exists(src)
    assert os.path.exists(os.path.join(alt_dir, "model.safetensors"))


def test_create_job_runs_on_complete_after_job_finishes():
    default_directory, alt_directory, default_dir, _alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    with open(src, "wb") as f:
        f.write(os.urandom(1024))

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "move",
    }])

    calls = []
    job_id = mover.create_job(plan["items"], verify=True, on_complete=lambda: calls.append(1))
    job = _wait_for_job(job_id)
    assert job.status == "done", job.to_dict()
    # on_complete runs on the job's own background thread right after
    # _run_job returns, so by the time _wait_for_job observes "done" it may
    # race the callback by a hair — give it a brief moment.
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [1]


def test_create_job_swallows_on_complete_errors():
    default_directory, alt_directory, default_dir, _alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    with open(src, "wb") as f:
        f.write(os.urandom(1024))

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "move",
    }])

    def _boom():
        raise RuntimeError("auto-track failed for some reason")

    job = _wait_for_job(mover.create_job(plan["items"], verify=True, on_complete=_boom))
    # The job's own outcome must not be affected by on_complete blowing up.
    assert job.status == "done", job.to_dict()


def test_delete_removes_source_file():
    default_directory, _alt_directory, default_dir, _alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    with open(src, "wb") as f:
        f.write(os.urandom(1024 * 10))

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "mode": "delete",
    }])
    assert plan["items"][0]["dest_dir_id"] is None
    assert plan["items"][0]["dest_path"] is None

    job = _wait_for_job(mover.create_job(plan["items"], verify=True))
    assert job.status == "done", job.to_dict()
    assert not os.path.exists(src)


def test_plan_rejects_missing_source_for_delete():
    default_directory, _alt_directory, _default_dir, _alt_dir = _make_two_directories()
    try:
        mover.plan_items([{
            "category": "checkpoints", "relpath": "nope.safetensors",
            "source_dir_id": default_directory.id, "mode": "delete",
        }])
        raise AssertionError("expected PlanError for missing source")
    except mover.PlanError:
        pass


def test_plan_rejects_conflict_without_overwrite():
    default_directory, alt_directory, default_dir, alt_dir = _make_two_directories()
    with open(os.path.join(default_dir, "model.safetensors"), "wb") as f:
        f.write(b"source")
    with open(os.path.join(alt_dir, "model.safetensors"), "wb") as f:
        f.write(b"already-here")

    try:
        mover.plan_items([{
            "category": "checkpoints", "relpath": "model.safetensors",
            "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
            "mode": "move",
        }])
        raise AssertionError("expected PlanError for existing destination")
    except mover.PlanError:
        pass

    # overwrite=True should be accepted by planning (execution then replaces it)
    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "move", "overwrite": True,
    }])
    assert plan["items"][0]["dest_existed"] is True


def test_plan_rejects_missing_source():
    default_directory, alt_directory, _default_dir, _alt_dir = _make_two_directories()
    try:
        mover.plan_items([{
            "category": "checkpoints", "relpath": "nope.safetensors",
            "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
            "mode": "move",
        }])
        raise AssertionError("expected PlanError for missing source")
    except mover.PlanError:
        pass


def test_plan_rejects_symlinked_source():
    default_directory, alt_directory, default_dir, _alt_dir = _make_two_directories()
    real = os.path.join(default_dir, "real.safetensors")
    with open(real, "wb") as f:
        f.write(b"data")
    link = os.path.join(default_dir, "link.safetensors")
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return  # symlink creation needs elevated privileges on this platform; nothing to assert

    try:
        mover.plan_items([{
            "category": "checkpoints", "relpath": "link.safetensors",
            "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
            "mode": "move",
        }])
        raise AssertionError("expected PlanError for a symlinked source")
    except mover.PlanError:
        pass


def test_same_device_move_skips_hashing():
    """Both stores live under the same tmp root in this test, so a move
    between them should take the atomic os.replace() fast path and never
    touch hashlib at all."""
    default_directory, alt_directory, default_dir, alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    with open(src, "wb") as f:
        f.write(b"same-device-payload")

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "move",
    }])

    def _boom(*_a, **_kw):
        raise AssertionError("hashlib.sha256 should not be called for a same-device move")

    real_sha256 = mover.hashlib.sha256
    mover.hashlib.sha256 = _boom
    try:
        job = _wait_for_job(mover.create_job(plan["items"], verify=True))
    finally:
        mover.hashlib.sha256 = real_sha256

    assert job.status == "done", job.to_dict()
    assert not os.path.exists(src)
    assert os.path.exists(os.path.join(alt_dir, "model.safetensors"))


def test_checksum_mismatch_leaves_source_untouched_and_cleans_up():
    """The core safety guarantee: if the post-write hash doesn't match the
    source hash, the job errors out, the source file is byte-for-byte
    unchanged, and no partial destination/.part file is left behind."""
    default_directory, alt_directory, default_dir, alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    payload = os.urandom(1024 * 10)
    with open(src, "wb") as f:
        f.write(payload)

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "copy",  # copy path also exercises hashing regardless of same-device
    }])

    import hashlib as real_hashlib_module

    real_sha256 = real_hashlib_module.sha256
    call_count = {"n": 0}

    class TamperedHash:
        """A real sha256 under the hood, except the SECOND instance created
        (the post-write verification pass) reports a bogus digest —
        simulating bytes that got corrupted in transit."""

        def __init__(self):
            call_count["n"] += 1
            self._bogus = call_count["n"] == 2
            self._real = real_sha256()

        def update(self, chunk):
            self._real.update(chunk)

        def hexdigest(self):
            digest = self._real.hexdigest()
            return ("0" * len(digest)) if self._bogus else digest

    mover.hashlib.sha256 = TamperedHash
    try:
        # Force the cross-device (chunked, hashed) path even though the test
        # stores share a device, so verification actually runs.
        real_same_device = mover._same_device
        mover._same_device = lambda *_a, **_kw: False
        try:
            job = _wait_for_job(mover.create_job(plan["items"], verify=True))
        finally:
            mover._same_device = real_same_device
    finally:
        mover.hashlib.sha256 = real_sha256

    assert job.status == "error", job.to_dict()
    assert "checksum mismatch" in (job.items[0].error or "")
    assert os.path.exists(src)
    with open(src, "rb") as f:
        assert f.read() == payload
    dest_path = plan["items"][0]["dest_path"]
    assert not os.path.exists(dest_path)
    assert not os.path.exists(dest_path + ".part")


def test_execute_item_cancel_cleans_up_part_file_and_preserves_source():
    default_directory, alt_directory, default_dir, _alt_dir = _make_two_directories()
    src = os.path.join(default_dir, "model.safetensors")
    payload = os.urandom(1024 * 64)
    with open(src, "wb") as f:
        f.write(payload)

    plan = mover.plan_items([{
        "category": "checkpoints", "relpath": "model.safetensors",
        "source_dir_id": default_directory.id, "dest_dir_id": alt_directory.id,
        "mode": "copy",
    }])
    resolved = plan["items"][0]
    item = mover.JobItem(
        category=resolved["category"], relpath=resolved["relpath"],
        source_dir_id=resolved["source_dir_id"], dest_dir_id=resolved["dest_dir_id"],
        mode=resolved["mode"], overwrite=resolved["overwrite"],
        source_path=resolved["source_path"], dest_path=resolved["dest_path"],
        size=resolved["size"],
    )

    class _CancelAfterFirstCheck:
        def __init__(self):
            self.checks = 0

        @property
        def cancel_requested(self):
            self.checks += 1
            return self.checks > 1

    try:
        mover._execute_item(item, verify=True, job=_CancelAfterFirstCheck())
        raise AssertionError("expected _Cancelled to propagate")
    except mover._Cancelled:
        pass

    assert not os.path.exists(resolved["dest_path"])
    assert not os.path.exists(resolved["dest_path"] + ".part")
    assert os.path.exists(src)
    with open(src, "rb") as f:
        assert f.read() == payload


def test_is_stray_temp_name_matches_widened_patterns():
    # Our own in-progress copies, plus common external downloader/sync tools'
    # leftovers — see mover._TEMP_GLOB_PATTERNS / _RSYNC_TEMP_RE.
    assert mover._is_stray_temp_name("model.safetensors.part") is True
    assert mover._is_stray_temp_name("model.safetensors.aria2") is True
    assert mover._is_stray_temp_name("model.safetensors.tmp") is True
    assert mover._is_stray_temp_name("a1b2c3d4e5f6.incomplete") is True  # huggingface_hub
    assert mover._is_stray_temp_name("model.safetensors.crdownload") is True  # Chrome/Edge
    assert mover._is_stray_temp_name(".model.safetensors.a1B2c3") is True  # rsync-style
    # Ordinary files and legitimate dotfiles must not be swept up.
    assert mover._is_stray_temp_name("model.safetensors") is False
    assert mover._is_stray_temp_name(".gitignore") is False
    assert mover._is_stray_temp_name(".validation_cache.json") is False


def test_cleanup_removes_aria2_and_rsync_style_temp_files():
    default_directory, _alt_directory, default_dir, _alt_dir = _make_two_directories()
    stale_time = time.time() - 7200  # older than the 1hr default cutoff

    aria2_leftover = os.path.join(default_dir, "download.safetensors.aria2")
    rsync_leftover = os.path.join(default_dir, ".model.safetensors.Ab3xY9")
    fresh_part = os.path.join(default_dir, "inflight.safetensors.part")
    for p in (aria2_leftover, rsync_leftover, fresh_part):
        with open(p, "wb") as f:
            f.write(b"x")
    os.utime(aria2_leftover, (stale_time, stale_time))
    os.utime(rsync_leftover, (stale_time, stale_time))
    # fresh_part keeps its current mtime — must NOT be cleaned up (an
    # in-progress transfer shouldn't have its .part yanked out from under it).

    removed = mover.cleanup_stray_temp_files()
    assert set(removed) == {aria2_leftover, rsync_leftover}
    assert os.path.exists(fresh_part)


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
