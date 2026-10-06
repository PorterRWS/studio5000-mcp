"""FastMCP entrypoint for the studio5000-mcp server.

The actual ``mcp = FastMCP("studio5000-mcp")`` instance is owned by
``logix_mcp._common`` (to avoid a circular import once tool modules start
importing ``mcp`` to register with). This file re-exports it, eagerly imports
every per-area tool module so each module's ``@mcp.tool()`` /
``@mcp.resource()`` decorators run against the same registry, and exposes
``main()`` for ``python -m logix_mcp.server`` and the legacy
``l5x_acd_server.py`` shim.

Phase 1 ships before all tool modules exist. Each per-module import is
wrapped in ``try / except ImportError`` so the server still starts cleanly
even with zero ``logix_mcp.tools.*`` files present; Phase 2 agents will
populate the missing ones without needing changes here.
"""
from __future__ import annotations

import logging

from logix_mcp._common import mcp

__all__ = ["mcp", "main"]


# ---------------------------------------------------------------------------
# Eager tool / resource module imports.
#
# Order is alphabetical for predictability; each module is independent. The
# whole point of these imports is the side effect of ``@mcp.tool()`` decorator
# evaluation, so we deliberately don't bind the imported module names.
# ---------------------------------------------------------------------------

_LOG = logging.getLogger("logix_mcp.server")

_TOOL_MODULES = (
    "logix_mcp.tools.project_io",
    "logix_mcp.tools.online",
    "logix_mcp.tools.build",
    "logix_mcp.tools.tags",
    "logix_mcp.tools.program",
    "logix_mcp.tools.xref",
    "logix_mcp.tools.partial",
    "logix_mcp.tools.protection",
    "logix_mcp.tools.safety",
    "logix_mcp.tools.sd_card",
    "logix_mcp.tools.resources",
)

for _mod_name in _TOOL_MODULES:
    try:
        __import__(_mod_name)
    except ImportError as _exc:
        # Phase 1: the module may not exist yet. That's fine — log and move on.
        _LOG.debug("tool module %s not available yet: %s", _mod_name, _exc)


def main() -> None:
    """Run the FastMCP server over stdio (no banner — keeps Claude Desktop quiet)."""
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
