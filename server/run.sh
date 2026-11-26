#!/usr/bin/env bash
# Launch the Confluence MCP server from the power's own directory.
# Works regardless of CWD — resolves path relative to this script.
#
# Usage (manual testing / standalone):
#   CONFLUENCE_PAT=... CONFLUENCE_BASE_URL=... CONFLUENCE_SPACE=... ./server/run.sh
#
# Note: mcp.json uses uvx directly with an absolute path.
# This script is provided for manual testing and debugging only.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uvx --from fastmcp fastmcp run "${SCRIPT_DIR}/confluence_mcp.py"
