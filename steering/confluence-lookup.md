---
applyTo: ""
---

# Confluence Page Lookup

## When to Use

When the user asks about a Confluence page — its content, status, who last edited it, or wants to find pages on a topic — use the MCP tools to fetch the information directly.

## Lookup Patterns

### By Page ID

When the user gives a numeric page ID or a Confluence page URL containing `pageId=` (e.g., "show me page 123456"):

1. Call `confluence_view(page_id="123456")`
2. Present a concise summary: title, space, last updated + by whom, labels
3. Include body highlights (first section or key bullet points) — do NOT dump the entire body
4. Mention ancestors (breadcrumb) and attachments if relevant

### By Title

When the user names a page (e.g., "get the 'Release Process' page"):

1. Call `confluence_get_by_title(title="Release Process")` — add `space="KEY"` if they specify a space
2. If not found, fall back to `confluence_search` with a `title ~ "..."` CQL to find near matches

### Search Queries

When the user asks a question that implies searching (e.g., "find docs about onboarding", "any runbooks for deploys?"):

1. Prefer `confluence_text_search(text="...")` for simple free-text within the configured space
2. Use `confluence_search(cql="...")` when the user needs filters (space, label, date, type)
3. Present results as a concise list: title, space, last updated, and the page ID (needed for follow-up `confluence_view`)

### Common CQL Translations

| User says | CQL |
|-----------|-----|
| "pages about X" | `text ~ "X" ORDER BY lastmodified DESC` |
| "pages titled X" | `title ~ "X"` |
| "in the DOCS space" | `space=DOCS AND type=page` |
| "updated this week" | `lastmodified >= startOfWeek()` |
| "labelled runbook" | `label = "runbook"` |
| "my pages" | `creator = currentUser()` |
| "recently created" | `created >= -7d ORDER BY created DESC` |

### Browsing

- "What's under this page?" → `confluence_children(page_id="...")`
- "What's attached?" → `confluence_attachments(page_id="...")`
- "What spaces are there?" → `confluence_spaces()`

## Response Format

Keep responses concise and scannable:

- **Single page:** Title, space, last updated (+ by whom), labels, then body highlights
- **Search results:** Bullet list with title + space + page ID + last-updated
- Always surface the **page ID** in search results so the user can ask you to open a specific one

Do NOT dump raw API output or the full storage-format body. Synthesize and summarize for the user. If the user explicitly asks for the full content, provide the converted plain text.
