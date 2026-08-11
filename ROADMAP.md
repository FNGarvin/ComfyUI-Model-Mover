# Roadmap / Wishlist

- [x] **WSL/Linux install support.** Done. Test the full suite of tools on 
      GNU/Linux systems.  
- [ ] **Directory-tree browse picker for "Add Directory".** V1 is a plain
      absolute-path text input. A server-driven folder browser (list
      subdirectories, click to descend) would be friendlier, especially for
      less path-savvy users.
- [ ] **Content-hash-based duplicate detection.** V1 only matches rows by
      identical relative path (`category/relpath`). Detecting the same model
      under two different names/locations would need a hash index — doable,
      but a real cost (hashing every file at least once) worth its own opt-in
      toggle rather than being on by default.
- [ ] **Automatic/rule-based placement policy.** E.g. "keep a full mirror on
      the slow drive, auto-promote/demote residency on the fast one by last-
      used date or size." The move/copy pipeline (`core/mover.py`) is already
      the right primitive for it: a policy layer would just decide *which* 
      items to plan and execute, using the exact same `plan_items`/`create_job`
      calls the UI uses today.
- [ ] **Handling linked entries instead of just flagging them.** Symlinks,
      NTFS junctions, and hard links are detected and excluded from
      move/copy today (see `core/scanner.py`'s link detection and
      `mover.plan_items`'s rejection of linked sources). Actually relinking
      (e.g. "move the target, repoint the link") is a reasonable follow-up
      but needs deliberate UX since the "right" behavior is genuinely
      ambiguous per link.
- [ ] **macOS testing.** Nothing in the code is Windows- or Linux-specific by
      design (`pathlib`/`os` throughout; the one Windows-only bit,
      `st_file_attributes`/reparse-point detection in
      `scanner.category_dir_link_info`, is behind an `os.name == "nt"`
      guard) — but it's genuinely untested on macOS since no Mac was
      available during development.
- [ ] **Partial-batch conflict handling.** A bulk Move/Copy currently plans
      and validates the whole batch up front — the first conflicting or
      missing item aborts the entire request rather than executing the valid
      items and reporting the rest. The grid's `both` badges make this
      fairly rare in practice, but a "skip conflicts and continue" mode for
      bulk actions would be a nice follow-up.
- [ ] **Stray file cleanup preview.** Currently the UI shows a count of stray temp/partial files. A confirmation modal listing individual file paths, sizes, and age before deletion would give users full visibility before purging files.
- [ ] **ComfyUI-Manager submission.** `pyproject.toml` is filled out to
      registry conventions and `TESTING.md` has a submission-readiness
      checklist, but actually opening the PR against
      `Comfy-Org/ComfyUI-Manager`'s `custom-node-list.json` is pending.


