"""mac-bridge-mcp: an MCP server that lets an AI control a macOS machine."""

from .server import mcp, main

__version__ = "0.1.0"
__all__ = ["mcp", "main", "__version__"]
