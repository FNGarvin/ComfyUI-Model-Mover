"""
@author: ComfyUI-Model-Mover
@title: ComfyUI-Model-Mover
@nickname: Model Mover
@version: 0.1.0
@description: Steam-Mover-style GUI for shuffling model files between two or
              more registered storage locations (drives), keeping
              extra_model_paths.yaml in sync.
"""

import logging

# Web directory for the frontend extension (toolbar button + modal dialog).
WEB_DIRECTORY = "./web"

# Empty NODE_CLASS_MAPPINGS — this addon provides no graph nodes, only a web
# extension + API routes. Keeping this present (even empty) avoids ComfyUI
# reporting an "IMPORT FAILED" for the package.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

logger = logging.getLogger("comfyui-model-mover")


def _register_routes() -> bool:
    """Register our aiohttp routes on the running PromptServer, if available.

    Mirrors the pattern used by comfyui-model-linker: custom nodes are fully
    imported before ComfyUI's aiohttp app starts serving, so
    PromptServer.instance already exists by the time this module is imported.
    """
    try:
        from server import PromptServer
    except ImportError as exc:
        logger.debug("Model Mover: could not import PromptServer: %s", exc)
        return False

    if not hasattr(PromptServer, "instance") or PromptServer.instance is None:
        logger.warning("Model Mover: PromptServer.instance not available yet; routes not registered")
        return False

    try:
        from .core.routes import register_routes

        register_routes(PromptServer.instance.routes)
        logger.info("Model Mover: API routes registered successfully")
        return True
    except Exception:
        logger.exception("Model Mover: failed to register API routes")
        return False


_register_routes()
