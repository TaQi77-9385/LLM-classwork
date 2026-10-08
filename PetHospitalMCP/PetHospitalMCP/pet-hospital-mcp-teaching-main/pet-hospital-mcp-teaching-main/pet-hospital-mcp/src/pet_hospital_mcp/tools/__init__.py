"""MCP tools for the Pet Hospital MCP server.

Each tool lives in its own module so future stages can add new tools by
dropping a module in here and registering it from
:mod:`pet_hospital_mcp.server`. Stage 1 ships only :mod:`list_pets`.
"""

from __future__ import annotations

__all__ = ["list_pets"]
