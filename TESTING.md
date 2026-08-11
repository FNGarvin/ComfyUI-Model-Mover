# Testing

Two tiers: automated pure-logic tests (no ComfyUI needed) and a manual
acceptance checklist to run against a real ComfyUI server before any release.

## 1. Automated tests

No real ComfyUI install is needed — `tests/fake_folder_paths.py` provides a
minimal stand-in for the one ComfyUI module the core code depends on
(`folder_paths`), installed into `sys.modules` before any `core.*` module is
imported. Each file is plain `assert`-based (same style as
`comfyui-model-linker/tests`), runnable directly or via `pytest`:

```
<ComfyUI>/.venv/Scripts/python.exe tests/test_config_writer.py
<ComfyUI>/.venv/Scripts/python.exe tests/test_directories.py
<ComfyUI>/.venv/Scripts/python.exe tests/test_scanner.py
<ComfyUI>/.venv/Scripts/python.exe tests/test_mover.py
<ComfyUI>/.venv/Scripts/python.exe tests/test_live.py
```

What each file actually covers:

- **test_config_writer.py** — yaml round-trip correctness: the user's own
  comments and unrelated blocks (e.g. `a1111:`) survive a write untouched;
  block order changes on `reorder_directories`, including placing the default
  install's anchor block anywhere via its sentinel id; `track_subdir` appends
  to an existing block or creates a companion block for the default
  directory; foreign (non-`model_mover_directory_*`) blocks are never read
  back as ours.
- **test_directories.py** — directory discovery ordering, including that a
  pre-rename yaml block (key prefix `model_mover_store_`, from before the
  store→directory terminology rename) is still recognized and surfaced
  correctly, with no manual migration needed.
- **test_scanner.py** — directory/category dir matching by path prefix;
  unregistered-subdirectory detection; file scanning reports size and
  correctly flags symlinked/hard-linked files (skipped gracefully if the
  test environment can't create links without elevation); inventory rows
  flag both-sides-present and missing sides correctly; symlinked *directories* are
  detected separately from symlinked files.
- **test_mover.py** — the safety-critical properties: move relocates and
  removes the source, copy leaves it in place; planning rejects a conflicting
  destination unless `overwrite` is set, a missing source, and a symlinked
  source; a same-device move takes the atomic `os.replace()` path and never
  touches `hashlib` at all; **a simulated checksum mismatch leaves the source
  byte-for-byte untouched and removes only the `.part` temp file**; a
  mid-transfer cancel does the same; stray temp-file detection matches
  `.part`/`.aria2`/`.tmp`/`.incomplete`/`.crdownload`/rsync-style leftovers
  without false-positiving on ordinary dotfiles.

- **test_live.py** — the live, no-restart registry updates: reordering
  correctly moves a newly-defaulted directory's paths ahead of the base
  install, and `resync_order()`/`unregister_base_path()` skip any category
  whose directory list isn't a plain list (some third-party custom node code
  registers one as a tuple instead) rather than crash the whole request over
  someone else's non-standard entry.

All five currently pass end-to-end (41/41).

## 2. Manual acceptance checklist

Run this against a real, running ComfyUI before trusting a change (or before
a release). Use small dummy files first — not real multi-GB models — for a
fast loop; only try a real model at the very end.

1. `pip install -r requirements.txt` inside ComfyUI's own `.venv` (adds
   `ruamel.yaml`). Confirm ComfyUI still starts cleanly even if you skip this
   step (it should log a warning, not crash).
2. Launch ComfyUI. Confirm the **Model Mover** toolbar button appears and
   opens the dialog (or the floating-button fallback, on an older frontend).
   With fewer than two directories registered it should land on the
   **Directories** tab automatically.
3. Use **Add Directory** to register a second location (e.g. `E:\models` or
   any scratch folder). Confirm a new `model_mover_directory_*` block appears
   in `extra_model_paths.yaml`, and that a node like CheckpointLoader's
   dropdown picks up a file placed there **without restarting the server**
   (ComfyUI's own "Refresh" action, or a page reload, is enough — no server
   restart). Switch to the grid via the header toggle.
4. Drop a couple of small dummy files into `models/loras`, and a folder with
   no matching registered category (e.g. `models/some_new_thing/`). Confirm
   the grid shows the tracked file, and the Directories tab offers **Track**
   for the untracked folder. Drop a dotfile too (e.g. `.leftover.json`) and
   confirm it's hidden by default; unchecking **Hide dotfiles** reveals it.
5. Move one dummy file A→B via its row button. Confirm it's physically
   relocated, and still resolvable in a LoraLoader node's dropdown (B stays
   registered). Confirm the row's action buttons sit next to their own
   side's column, delete innermost/closest to the filename on both sides
   (A's ←/⇐/Del on the left, B's Del/⇒/→ on the right), and that Copy
   buttons are visually distinct (double arrow, accent color) from Move.
   Confirm the footer mirrors the same left-to-right order (← Move, ⇐ Copy,
   Del, Del, Copy ⇒, Move →) and no longer wraps to a second line.
6. Create a same-named file in both directories. Confirm the grid shows a
   `both` badge next to the filename, and that attempting to move/copy over
   it prompts for overwrite confirmation rather than silently replacing it.
7. Create a symlink and (elevated, if your OS requires it) a hard link among
   the dummy files. Confirm both show a `linked` badge and their row's
   Move/Copy buttons are disabled.
8. Start a multi-file bulk move (ideally with a large/slow file so there's
   time to observe) and confirm the persistent progress+Cancel widget
   appears in the corner of the screen. Close the Model Mover dialog mid-
   transfer and confirm the widget stays visible and Cancel still works;
   reopen the dialog and confirm the footer shows the same in-progress state
   rather than a blank bar. Then cancel it mid-batch and confirm the
   in-flight item's `.part` file is gone and already-completed items are
   untouched and correct.
9. Put a same-named dummy file with *different contents* in both
   directories; use the ↑ button to move Directory B above Directory A and
   confirm a small "Priority saved" confirmation appears, and that Directory
   B's copy now resolves first for a node loading it; reorder back and
   confirm Directory A wins again — both without restarting. Restart ComfyUI
   entirely and confirm the reordered priority held.
10. Only after all of the above pass, try one real (non-critical, already
    backed-up) model file before trusting it with your full library.

## 3. ComfyUI-Manager submission readiness

Checklist before opening a PR to `Comfy-Org/ComfyUI-Manager`'s
`custom-node-list.json`:

- [x] `pyproject.toml` has the fields Comfy Registry expects (`name`,
      `version`, `description`, `license`, `requires-python`,
      `[project.urls]`, `[tool.comfy]` with `PublisherId`/`DisplayName`).
- [x] `NODE_CLASS_MAPPINGS = {}` + `WEB_DIRECTORY` convention (no graph
      nodes, web-extension only) — the same pattern `comfyui-model-linker`
      already uses successfully.
- [x] Loads cleanly under ComfyUI-Manager's "Use local DB" validation option.
- [x] Does **not** crash ComfyUI startup if `ruamel.yaml` isn't installed yet
      (verified: `core/config_writer.py` imports it in a guarded `try/except`
      and only raises `ConfigWriterUnavailable` when a write is actually
      attempted).
- [x] `requirements.txt` present so ComfyUI-Manager can auto-install
      `ruamel.yaml`.
- [x] `README.md`, `LICENSE` present.
