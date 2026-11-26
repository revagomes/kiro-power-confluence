---
name: "confluence"
displayName: "Confluence"
description: "Read and manage Confluence pages, spaces, and content directly from chat sessions. View, search, create, update, comment on, and label pages using the Confluence REST API. Works with any Confluence instance and space."
keywords: ["confluence", "wiki", "page", "space", "documentation", "docs", "cql", "search", "knowledge base", "attachment", "label", "comment"]
author: "Renato Vasconcellos Gomes"
---

# Confluence

## Overview

This Power provides Confluence content management capabilities via MCP tools. It connects to any Confluence instance (Cloud or Server/Data Center) and exposes page lifecycle operations — from lookup and search through to creation, updates, comments, and labels.

**Key capabilities:**

- **Page lookup** — View full page content (rendered to text), version, labels, ancestors
- **Title lookup** — Resolve a page by its exact title in a space
- **CQL search** — Query content with any CQL expression
- **Text search** — Simple full-text search within the configured space
- **Browse** — List pages in a space, list child pages, list attachments
- **Page creation** — Create new pages (optionally nested under a parent)
- **Page updates** — Edit body and/or title with automatic version bump
- **Comments** — Add comments to pages
- **Labels** — List and add labels
- **Spaces** — List available spaces

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **CONFLUENCE_PAT** | Personal access token for your Confluence instance |
| **CONFLUENCE_BASE_URL** | Base URL of your Confluence instance (e.g., `https://confluence.example.com`) |
| **CONFLUENCE_SPACE** | Default space key (e.g., `DOCS`, `TEAM`, `MYSPACE`) |
| **Python 3.11+** | Required for the MCP server |
| **uvx** | Used to run the FastMCP server (part of `uv` toolchain) |

The MCP server is **self-contained** inside this power (`server/confluence_mcp.py`). No project-specific scripts are required.

Configuration is entirely via environment variables:

| Variable | Required | Default |
|----------|----------|---------|
| `CONFLUENCE_PAT` | Yes | — |
| `CONFLUENCE_BASE_URL` | Yes | — |
| `CONFLUENCE_SPACE` | Yes | — |

If any required variable is missing, the server returns an actionable error message.

## Available MCP Tools

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
| `confluence_add_comment` | Add a comment to a page (accepts `body_format`) |
| `confluence_add_label` | Add a label to a page |

## Activation Keywords

This power activates when you mention:
- confluence, wiki, page, space, documentation, docs
- CQL, search pages, knowledge base
- create page, update page, comment on page
- attachment, label

## Steering

Load the appropriate steering based on the user's intent:

- **Looking up / reading pages** → `readPowerSteering("confluence", "confluence-lookup.md")`
- **Creating or editing content** → `readPowerSteering("confluence", "confluence-authoring.md")`

## Skills

| Skill | When to use |
|-------|-------------|
| `confluence-editing` (`skills/confluence-editing/SKILL.md`) | Authoring/editing pages with the safe read-storage → modify → write workflow; appending sections without data loss; fixing storage-format (XHTML/macro/glob) rendering issues; and the REST API fallback for capabilities the MCP does not expose (raw storage retrieval, version history, page moves, attachment upload, label removal). |

The skill complements the steering: steering covers the day-to-day authoring flow, while the skill adds the safe-append pattern, storage-format survival rules, and the API fallback map for MCP gaps.

## Quick Usage Examples

### Look up a page by ID

User: "Show me Confluence page 123456"
→ Call `confluence_view(page_id="123456")` and summarize the result.

### Look up a page by title

User: "Get the 'Release Process' page from DOCS"
→ Call `confluence_get_by_title(title="Release Process", space="DOCS")`

### Search with CQL

User: "Find pages about onboarding updated this month"
→ Call `confluence_search(cql='text ~ "onboarding" AND lastmodified >= startOfMonth()')`

### Full-text search in the configured space

User: "Search the wiki for pagination"
→ Call `confluence_text_search(text="pagination")`

### Create a page

User: "Create a page called 'Sprint 42 Notes' under page 100"
→ Call `confluence_create(title="Sprint 42 Notes", body="...", parent_id="100")`

Tip: pass `body_format="text"` for plain text (auto HTML-escaped), or `body_format="storage"` when supplying Confluence storage-format XHTML verbatim. The default `auto` treats the body as storage only if it contains `<`.

### Comment on a page

User: "Add a note to page 123456"
→ Call `confluence_add_comment(page_id="123456", body="...")` (also accepts `body_format`)

## CQL Quick Reference

| Pattern | Example |
|---------|---------|
| Pages in a space | `space=DOCS AND type=page` |
| Text search | `text ~ "pagination"` |
| Title contains | `title ~ "release"` |
| Updated recently | `lastmodified >= -7d` |
| By label | `label = "runbook"` |
| By creator | `creator = currentUser()` |
| Recently created | `created >= startOfWeek()` |
| Order by recency | `... ORDER BY lastmodified DESC` |

Note: The configured `CONFLUENCE_SPACE` is automatically applied by the shortcut tools (`confluence_text_search`, `confluence_list_pages`, and the default `space` arg of `confluence_get_by_title` / `confluence_create`). For `confluence_search`, include `space=KEY` in your CQL if you want to constrain the space.

> **Scope is a default, not a security boundary.** `confluence_search` accepts raw CQL and can reach any space the `CONFLUENCE_PAT` is permitted to read — `CONFLUENCE_SPACE` does not restrict it. Access is governed by the PAT's server-side permissions. Free-text input in `confluence_text_search` is escaped so it cannot break out of the CQL string literal.

## Troubleshooting

### Authentication Errors (401)

The `CONFLUENCE_PAT` token has expired or is invalid. Generate a new one from your Confluence/Jira profile → Personal Access Tokens.

### Permission Denied (403)

Your token doesn't have permission for the requested operation. Check that your account has the required space permissions.

### Page Not Found (404)

Verify the page ID or title. Use `confluence_search` / `confluence_get_by_title` to resolve titles to IDs. On some Server instances a page may exist but be restricted — a 404 can also indicate a permission restriction.

### Connection Timeout

Ensure network connectivity to your Confluence instance. If behind a VPN, make sure it's connected.

### Missing Configuration

If you see "Missing required environment variable(s)", update the `env` block in your `mcp.json` or export the variables in your shell.
