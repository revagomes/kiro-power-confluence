# Confluence — Kiro Power

[![Kiro Power](https://img.shields.io/badge/Kiro-Power-blue)](https://kiro.dev)
[![License: GPL v2+](https://img.shields.io/badge/License-GPL%20v2%2B-blue.svg)](LICENSE)

Read and manage Confluence pages, spaces, and content directly from [Kiro](https://kiro.dev) chat sessions via MCP (Model Context Protocol).

Works with **any Confluence instance** (Cloud or Server/Data Center) and **any space** — fully configured via environment variables.

## What It Does

This power exposes 19 MCP tools for interacting with the Confluence REST API:

- **Query:** View pages (body rendered to text), look up by title, search with CQL, full-text search, list pages/children/attachments/labels/spaces/comments
- **Mutate:** Create pages, update body/title (auto version bump), append content, upload/delete attachments, add/list/delete comments, add labels

## Available Tools

| Tool | Purpose |
|------|---------|
| `confluence_view` | View a page with full details (body text, version, labels, ancestors) |
| `confluence_get_by_title` | Fetch a page by exact title within a space |
| `confluence_search` | Search content using CQL |
| `confluence_text_search` | Full-text search within the configured space |
| `confluence_list_pages` | List pages in a space, most-recent first |
| `confluence_children` | List direct child pages of a page |
| `confluence_attachments` | List attachments on a page |
| `confluence_labels` | List labels on a page |
| `confluence_spaces` | List spaces available to the account |
| `confluence_create` | Create a new page (accepts `body_format`: `text`/`storage`/`auto`) |
| `confluence_update` | Update a page's body and/or title (accepts `body_format`, optional `expected_version` to refuse stale writes) |
| `confluence_get_storage` | Get a page's raw, lossless storage-format body (for safe edits) |
| `confluence_append` | Safely append content to a page without rewriting its body |
| `confluence_upload_attachment` | Upload (or replace by filename) a local file as a page attachment; restricted to `CONFLUENCE_UPLOAD_DIRS` (default: server working dir), credential files refused |
| `confluence_delete_attachment` | Delete an attachment from a page by its attachment ID |
| `confluence_add_comment` | Add a comment to a page (accepts `body_format`) |
| `confluence_comments` | List comments on a page (text + raw storage body) |
| `confluence_delete_comment` | Delete a comment from a page by its comment ID |
| `confluence_add_label` | Add a label to a page |

## Requirements

- **Python 3.11+**
- **uvx** (from the [`uv`](https://docs.astral.sh/uv/getting-started/installation/) package manager)
- **Confluence Personal Access Token** with read/write access to your space

The power is fully self-contained — the MCP server lives at `server/confluence_mcp.py` inside this directory. No external project scripts are needed.

## Configuration

All configuration is via environment variables — no Confluence instance or space is hardcoded:

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `CONFLUENCE_PAT` | Yes | Personal access token for authentication |
| `CONFLUENCE_BASE_URL` | Yes | Confluence instance URL (e.g., `https://confluence.example.com`) |
| `CONFLUENCE_SPACE` | Yes | Default space key (e.g., `DOCS`, `TEAM`) |
| `CONFLUENCE_UPLOAD_DIRS` | No | `os.pathsep`-separated directories attachments may be uploaded from (default: server working directory). Set it in the MCP server's `env`; restart the server for changes to take effect. |

### Example configurations

**Atlassian Cloud:**
```bash
export CONFLUENCE_PAT="your-api-token"
export CONFLUENCE_BASE_URL="https://yourcompany.atlassian.net/wiki"
export CONFLUENCE_SPACE="DOCS"
```

**Confluence Server / Data Center (self-hosted):**
```bash
export CONFLUENCE_PAT="your-personal-access-token"
export CONFLUENCE_BASE_URL="https://confluence.internal.company.com"
export CONFLUENCE_SPACE="OPS"
```

**Confluence Server / Data Center (self-hosted, shared with Jira):**
```bash
export CONFLUENCE_PAT="your-personal-access-token"
export CONFLUENCE_BASE_URL="https://confluence.example.com/context/confluence"
export CONFLUENCE_SPACE="YOURSPACE"
```

> Note: on a shared Jira+Confluence instance the same PAT usually authenticates both, but not always — some instances require a Confluence-specific token. Jira and Confluence typically live under distinct context paths (e.g. `.../jira` and `.../confluence`); verify the correct Confluence REST base against your instance.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/revagomes/kiro-power-confluence.git
cd kiro-power-confluence
```

### 2. Set up environment variables

Either export them in your shell profile, or copy `server/.env.example` to `server/.env` and fill in the values (the server reads `server/.env` directly at startup):

```bash
cp server/.env.example server/.env
# then edit server/.env
```

### 3. Update the server path in `mcp.json`

Edit `mcp.json` and replace the path with your local clone location:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "uvx",
      "args": ["--from", "fastmcp", "fastmcp", "run", "/path/to/kiro-power-confluence/server/confluence_mcp.py"],
      "env": {
        "CONFLUENCE_PAT": "${env:CONFLUENCE_PAT}",
        "CONFLUENCE_BASE_URL": "${env:CONFLUENCE_BASE_URL}",
        "CONFLUENCE_SPACE": "${env:CONFLUENCE_SPACE}"
      }
    }
  }
}
```

### 4. Register the power in Kiro

Add to `~/.kiro/powers/registries/user-added.json` inside the `"powers"` array:

```json
{
  "name": "confluence",
  "description": "Confluence content management via MCP",
  "source": {
    "type": "local",
    "path": "/path/to/kiro-power-confluence"
  },
  "autoInstall": false
}
```

Add to `~/.kiro/powers/installed.json` under `"installedPowers"`:

```json
{
  "name": "confluence",
  "registryId": "user-added"
}
```

### 5. Verify

```bash
# Test the server starts (set env vars first)
export CONFLUENCE_PAT="test" CONFLUENCE_BASE_URL="https://confluence.example.com" CONFLUENCE_SPACE="TEST"
uvx --from fastmcp fastmcp inspect /path/to/kiro-power-confluence/server/confluence_mcp.py
```

Expected output: `Tools: 13`

Then restart Kiro — the power should appear in the powers list.

## Usage Examples

Once installed, just talk naturally in your Kiro session:

```
"Show me Confluence page 123456"
"Get the 'Release Process' page from DOCS"
"Search the wiki for pagination"
"List the pages in the DOCS space"
"What are the child pages of 123456?"
"Create a page called 'Sprint 42 Notes' under page 100"
"Add a comment to page 123456 with the deployment status"
```

## How It Works

The power uses [FastMCP](https://github.com/jlowin/fastmcp) to expose Python functions as MCP tools over stdio transport. Each tool maps to one or more Confluence REST API endpoints:

- **Read operations** use `GET /rest/api/content`, `/content/search`, `/content/{id}/child/...`
- **Write operations** use `POST /rest/api/content`, `/content/{id}/label`
- **Update operations** use `PUT /rest/api/content/{id}` (with automatic version bump)

Authentication is via Bearer token (`CONFLUENCE_PAT`). All requests are made server-side — no credentials are exposed to the agent or chat context. Page bodies (Confluence storage-format XHTML) are converted to plain text before being returned, so the agent receives readable content instead of markup noise.

## Security

The server is hardened against common risks:

- **HTTPS enforced** — the base URL must use `https://`; the token is never sent over cleartext.
- **PAT isolation** — the token is read from `server/.env` (or the env block) and never returned to the agent, logged, or echoed.
- **CQL injection safe** — free-text search escapes backslashes and quotes so input cannot break out of the CQL string literal.
- **Input validation & length caps** — page IDs, space keys, labels, titles, and body sizes are validated/bounded to reject malformed or oversized input.
- **Redirect-hardened HTTP client** — refuses scheme-downgrade (→ http) and cross-host redirects on token-bearing requests, and retries transient `429`/`503` with backoff (honouring `Retry-After`).
- **Explicit `body_format`** — create/update/comment take `text` (HTML-escaped), `storage` (verbatim XHTML), or `auto`, avoiding ambiguous content sniffing.

> **Note on scope:** `CONFLUENCE_SPACE` is a *default*, **not a security boundary**. `confluence_search` accepts raw CQL and can reach any space the PAT is permitted to read. Scope access via the PAT's server-side permissions, not this setting.

Never commit `server/.env`. It is covered by `.gitignore` (and `.aiignore`). If a token is ever exposed, rotate it in the Confluence UI.

## Structure

```
kiro-power-confluence/
├── POWER.md                    ← Power manifest (frontmatter + instructions for Kiro)
├── mcp.json                    ← MCP server configuration
├── server/
│   ├── confluence_mcp.py       ← Self-contained MCP server (FastMCP + Confluence REST API)
│   ├── test_confluence_mcp.py  ← Unit tests: security fixes (CQL escaping, caps, redirects…)
│   ├── test_tools_confluence_mcp.py ← Tool tests: all 19 tools via a mocked HTTP layer
│   ├── .env.example            ← Template for local credentials (copy to .env)
│   └── run.sh                  ← Launcher script (resolves own path, runs from anywhere)
├── steering/
│   ├── confluence-lookup.md    ← Page query and search patterns
│   └── confluence-authoring.md ← Page creation and editing guidance
├── _check_setup.ps1            ← Windows setup diagnostics
├── docs/
│   └── OPERATIONS.md           ← Setup/auth notes, gotchas, and verification (sanitized)
├── .gitignore
├── .aiignore                   ← Excludes secrets/artifacts from AI-tool context
├── LICENSE
└── README.md                   ← This file
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Missing required environment variable(s)" | Set `CONFLUENCE_PAT`, `CONFLUENCE_BASE_URL`, and `CONFLUENCE_SPACE` |
| 401 Unauthorized | Token expired — regenerate in your profile |
| 403 Forbidden | Your token lacks permissions — check space permissions |
| 404 Not Found | Verify the page ID/title; may also be a permission restriction on Server |
| Connection timeout | Ensure network/VPN access to your Confluence instance |
| Server won't start | Run `uvx --from fastmcp fastmcp inspect server/confluence_mcp.py` |
| Tools not showing in Kiro | Verify `installed.json` entry and restart Kiro |

## Testing

The server ships with **50 automated tests** in two suites:

- `server/test_confluence_mcp.py` — security fixes (CQL escaping/injection, input length caps, `body_format`, redirect policy, `.env` parsing, retry policy).
- `server/test_tools_confluence_mcp.py` — all 19 tools via a mocked HTTP layer, plus the response summarizers, storage→text conversion, and error/config paths.

No live Confluence instance or token is needed — the HTTP layer is mocked and env is stubbed.

```bash
# Run under pytest
uvx --with pytest --from fastmcp python -m pytest server/ -q

# Or run either suite standalone (self-runner, no pytest required)
uvx --from fastmcp python server/test_confluence_mcp.py
uvx --from fastmcp python server/test_tools_confluence_mcp.py
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/add-page-tree-tool`)
3. Add your tool to `server/confluence_mcp.py` — follow the `@mcp.tool()` pattern
4. Add/extend tests in `server/test_*.py` and run the suites (see [Testing](#testing))
5. Verify the server loads: `uvx --from fastmcp fastmcp inspect server/confluence_mcp.py`
6. Update the tools table in `POWER.md` and `README.md`
7. Submit a pull request

## License

This project is licensed under the GNU General Public License v2.0 or later (GPL-2.0-or-later).
See [LICENSE](LICENSE) for the full text.
