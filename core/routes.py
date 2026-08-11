"""aiohttp route handlers for /model_mover/*, registered onto the running
PromptServer's route table from __init__.py."""

from __future__ import annotations

import logging
import os

from aiohttp import web

from . import config_writer
from . import live
from . import mover
from . import scanner
from . import directories as directories_module

logger = logging.getLogger("comfyui-model-mover")


def _safe_live_apply(fn, *args, **kwargs) -> None:
    """Run a live-registry update (live.py) without letting it turn an
    already-persisted yaml change into a reported failure. The yaml write is
    the source of truth and survives a restart regardless; live application
    is a no-restart-needed bonus on top of it, so a hiccup here is logged and
    swallowed rather than propagated as a 500 for a change that actually
    succeeded."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception(
            "Model Mover: live registry update failed after a successful config write "
            "(the change is saved and will take effect on next restart)"
        )


def _auto_track_all_directories() -> None:
    """Every physical subdirectory of every directory is registered as a
    category the moment it's seen — no manual "track this folder" step.
    The category filter chips in the grid are what keep a long list
    manageable, not a gate on whether something shows up at all. Cheap once
    converged (unregistered_subdirs is a single os.scandir per directory);
    only writes to disk for a folder that's genuinely new since the last
    check."""
    for directory in directories_module.discover_directories():
        for name in scanner.unregistered_subdirs(directory):
            relpath = name + "/"
            try:
                config_writer.track_subdir(
                    yaml_key=directory.yaml_key,
                    base_path=directory.base_path,
                    label=directory.label,
                    category=name,
                    relpath=relpath,
                )
            except config_writer.ConfigWriterUnavailable:
                return  # can't persist yet; try again on the next request
            except Exception:
                logger.exception("Model Mover: auto-track failed for %s/%s", directory.id, name)
                continue
            _safe_live_apply(live.register_dirs, name, directory.base_path, [relpath])
    _safe_live_apply(live.resync_order)


def register_routes(routes) -> None:
    @routes.get("/model_mover/directories")
    async def list_directories(request):
        try:
            _auto_track_all_directories()
            data = [d.to_dict() for d in directories_module.discover_directories()]
            return web.json_response({
                "directories": data,
                "config_writable": config_writer.ruamel_available(),
                "stale_temp_count": mover.count_stray_temp_files(),
            })
        except Exception as exc:
            logger.exception("Model Mover: list_directories failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/model_mover/directories")
    async def add_directory(request):
        try:
            body = await request.json()
            base_path = str(body.get("base_path", "")).strip()
            label = str(body.get("label") or base_path).strip()
            if not base_path or not os.path.isabs(base_path):
                return web.json_response({"error": "base_path must be an absolute path"}, status=400)

            os.makedirs(base_path, exist_ok=True)

            # No category seeding needed here: whatever subdirectories
            # already exist under base_path (a pre-populated backup drive,
            # for instance) get picked up automatically by
            # _auto_track_all_directories() on the very next GET /directories
            # — which the frontend calls immediately after this succeeds.
            yaml_key = config_writer.add_directory(base_path, label, {})
            _safe_live_apply(live.resync_order)

            directory = directories_module.get_directory(yaml_key)
            return web.json_response({"directory": directory.to_dict() if directory else None})
        except config_writer.ConfigWriterUnavailable as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except OSError as exc:
            return web.json_response({"error": f"could not create/access base_path: {exc}"}, status=400)
        except Exception as exc:
            logger.exception("Model Mover: add_directory failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.delete("/model_mover/directories/{dir_id}")
    async def remove_directory(request):
        try:
            directory = directories_module.get_directory(request.match_info["dir_id"])
            if directory is None:
                return web.json_response({"error": "unknown directory"}, status=404)
            if not directory.is_managed:
                return web.json_response(
                    {"error": "the default install directory can't be removed"}, status=400
                )
            config_writer.remove_directory(directory.yaml_key)
            _safe_live_apply(live.unregister_base_path, directory.base_path)
            _safe_live_apply(live.resync_order)
            return web.json_response({"ok": True})
        except config_writer.ConfigWriterUnavailable as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            logger.exception("Model Mover: remove_directory failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/model_mover/track")
    async def track_subdir(request):
        # Manual tracking is superseded by _auto_track_all_directories()
        # (every directory's physical subdirectories are registered the
        # moment GET /directories runs), but this endpoint stays as a direct
        # way to force it for one directory without waiting on that pass.
        try:
            body = await request.json()
            directory = directories_module.get_directory(str(body.get("dir_id", "")))
            category = str(body.get("category", "")).strip()
            relpath = str(body.get("relpath") or (category + "/")).strip()
            if directory is None or not category:
                return web.json_response({"error": "dir_id and category are required"}, status=400)

            yaml_key = config_writer.track_subdir(
                yaml_key=directory.yaml_key,
                base_path=directory.base_path,
                label=directory.label,
                category=category,
                relpath=relpath,
            )
            _safe_live_apply(live.register_dirs, category, directory.base_path, [relpath])
            _safe_live_apply(live.resync_order)
            return web.json_response({"ok": True, "yaml_key": yaml_key})
        except config_writer.ConfigWriterUnavailable as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            logger.exception("Model Mover: track_subdir failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/model_mover/inventory")
    async def inventory(request):
        try:
            dir_a = directories_module.get_directory(request.query.get("dir_a", ""))
            dir_b = directories_module.get_directory(request.query.get("dir_b", ""))
            if dir_a is None or dir_b is None:
                return web.json_response({"error": "dir_a and dir_b are required"}, status=400)

            cats_param = request.query.get("categories", "")
            categories = {c.strip() for c in cats_param.split(",") if c.strip()} or None

            data = scanner.build_inventory(dir_a, dir_b, categories)
            data["dir_a"] = dir_a.to_dict()
            data["dir_b"] = dir_b.to_dict()
            return web.json_response(data)
        except Exception as exc:
            logger.exception("Model Mover: inventory failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/model_mover/plan")
    async def plan(request):
        try:
            body = await request.json()
            result = mover.plan_items(body.get("items", []))
            return web.json_response(result)
        except mover.PlanError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            logger.exception("Model Mover: plan failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/model_mover/execute")
    async def execute(request):
        try:
            body = await request.json()
            verify = bool(body.get("verify", True))
            plan_result = mover.plan_items(body.get("items", []))
            # A move/copy can land a file in a destination directory that
            # wasn't a known category dir yet — re-run auto-tracking once the
            # job finishes so that directory (and the file in it) doesn't sit
            # invisible to inventory scanning until something else happens to
            # call GET /directories.
            job_id = mover.create_job(
                plan_result["items"], verify=verify, on_complete=_auto_track_all_directories
            )
            return web.json_response({"job_id": job_id})
        except mover.PlanError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            logger.exception("Model Mover: execute failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/model_mover/jobs/{job_id}")
    async def job_status(request):
        job = mover.get_job(request.match_info["job_id"])
        if job is None:
            return web.json_response({"error": "unknown job"}, status=404)
        return web.json_response(job.to_dict())

    @routes.post("/model_mover/jobs/{job_id}/cancel")
    async def job_cancel(request):
        ok = mover.cancel_job(request.match_info["job_id"])
        if not ok:
            return web.json_response({"error": "unknown job"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/model_mover/directories/reorder")
    async def reorder(request):
        try:
            body = await request.json()
            ordered_ids = [str(i) for i in body.get("dir_ids", [])]
            current = directories_module.discover_directories()
            current_ids = {d.id for d in current}
            if set(ordered_ids) != current_ids or len(ordered_ids) != len(current):
                return web.json_response(
                    {"error": "dir_ids must be a permutation of every current directory"},
                    status=400,
                )
            default_dir = next(d for d in current if not d.is_managed)
            config_writer.reorder_directories(
                ordered_ids,
                default_base_path=default_dir.base_path,
                default_label=default_dir.label,
            )
            _safe_live_apply(live.resync_order)
            return web.json_response({"ok": True})
        except config_writer.ConfigWriterUnavailable as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            logger.exception("Model Mover: reorder failed")
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/model_mover/cleanup")
    async def cleanup(request):
        try:
            removed = mover.cleanup_stray_temp_files()
            return web.json_response({"removed": removed})
        except Exception as exc:
            logger.exception("Model Mover: cleanup failed")
            return web.json_response({"error": str(exc)}, status=500)
