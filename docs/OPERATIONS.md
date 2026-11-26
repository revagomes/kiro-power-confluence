# kiro-power-confluence — Operations

_Generic notes for anyone deploying this power against a Confluence instance._

## Project location & layout
- Power lives at: `<repo-root>/kiro-power-confluence`.
- Key files: `mcp.json` (server config), `server/confluence_mcp.py` (FastMCP server), `server/.env` (secrets — gitignored), `server/.env.example` (template), `README.md`, `POWER.md`, `skills/confluence-editing/SKILL.md`.
- Registration: the server is provided via the power registration (Kiro powers registry), so a global `~/.kiro/settings/mcp.json` entry is not required.
- Naming: keep the directory name singular (`kiro-power-confluence`) to match the sibling power-naming convention. Do NOT rename to the plural.

## Environment (`server/.env`)
- `CONFLUENCE_PAT` — Bearer personal access token. Never commit; keep in the gitignored `server/.env` only.
- `CONFLUENCE_BASE_URL` — for a self-hosted instance the application context suffix (e.g. `/<context>/confluence`) is usually REQUIRED. Verify against your own instance.
- `CONFLUENCE_SPACE` — your default space key.
- Auth model: Confluence Server / Data Center typically uses **Bearer PAT**. Confluence Cloud uses email + API token (Basic auth) — adjust to your instance if you are on Cloud.

## Confluence and Jira may need SEPARATE tokens
- On a shared instance where Jira and Confluence are **separate applications**, a **Jira PAT may NOT authenticate against Confluence** — it can return **401 on every Confluence endpoint** while the identical token returns **200** on Jira.
- Illustrative symptom (same token):
  - `…/<jira-context>/rest/api/2/myself` → **200**
  - `…/<confluence-context>/rest/api/user/current` → **401**
- Fix: generate a dedicated Confluence PAT in the Confluence web UI → Profile → Settings → Personal Access Tokens → Create. Then `GET {base}/rest/api/user/current` returns 200.

## Base URL gotcha (IMPORTANT)
- Point `CONFLUENCE_BASE_URL` at the REAL Confluence REST API root (the correct application context returns proper JSON — e.g. a 401 error object when unauthenticated).
- A wrong/legacy path may be a **302 redirect to an error gateway**. It can masquerade as HTTP 200 in some clients and yield empty/null API bodies. Confirm the base URL returns a genuine JSON API response before trusting it.

## Confluence REST notes
- View a page by numeric id with expand:
  `GET {base}/rest/api/content/{pageId}?expand=body.storage,version,space,ancestors,metadata.labels,history` — returns full body + version + breadcrumb + labels.
- The page body comes back as Confluence **storage format (XHTML)**. The MCP `confluence_view` tool converts it to plain text (strip tags, unescape entities, collapse blank lines) before returning — so the agent gets readable content, **not** raw markup. To obtain raw storage (needed for safe section edits) call the REST API with `expand=body.storage` directly. See `skills/confluence-editing/SKILL.md`.
- CQL search: `GET {base}/rest/api/content/search?cql=...&expand=version,space`.
- Resolve a title → id: `GET {base}/rest/api/content?spaceKey=KEY&title=...`.

## Editing safety (see the skill for the full workflow)
- `confluence_update` **replaces** the whole body; there is no append mode.
- `confluence_view` returns rendered text, not storage — never round-trip an edit through it or you will destroy markup. Fetch raw storage via the API first, splice, then write back with `body_format="storage"`.
- Wrap globs / code / anything containing `*` in `<code>` or a code macro so Confluence's renderer does not alter it.

## Shell notes (cross-platform)
- Prefer writing a small script file and running it over long inline one-liners (inline variable interpolation is unreliable in some shells).
- **PowerShell:** `$pid` is a read-only automatic variable — do not use it as a local variable name (use `$pageId`).
- Large storage bodies can break console output streaming — dump to a file and read the file when inspecting big pages.
- To load `.env` into a script: read lines, strip a leading `export `, split on the first `=`, trim quotes, then set the variable in the process environment. Never echo the values.

## Verification procedure (server health)
1. Load `.env`, then run: `uvx --from fastmcp fastmcp inspect server/confluence_mcp.py`
   - Expected: Server Name **"Confluence <SPACE>"**, and the full tool set registered (13 tools at time of writing).
   - Tip: set `_CONFLUENCE_MCP_SKIP_ENV=1` with dummy env vars to inspect without a live token.
2. Auth smoke test via REST: `GET {base}/rest/api/user/current` with header `Authorization: Bearer {PAT}`. HTTP 200 + a username = auth OK.

## Cleanup & secrets
- Generated/transient artifacts (local inventories, screenshots, exports, temp probe scripts, page dumps) should be gitignored and deleted once done.
- Secrets live only in `server/.env` (gitignored). If a token is ever exposed, rotate it in the Confluence UI and update `server/.env`.
- Never place a real token, hostname, space key, or page content from a private instance into tracked files (README, POWER.md, this file, tests, or the skill). Use placeholders.
