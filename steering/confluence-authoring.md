---
applyTo: ""
---

# Confluence Authoring Workflow

## When Creating a Page

When the user wants to create a new Confluence page:

1. **Confirm placement** — Which space (defaults to the configured `CONFLUENCE_SPACE`) and, optionally, which parent page. If they name a parent by title, resolve it first with `confluence_get_by_title` to get its ID for `parent_id`.
2. **Draft the body** — Prefer Confluence storage format (XHTML). Plain text is also accepted and will be wrapped in a paragraph automatically.
3. **Create** — Call `confluence_create(title=..., body=..., parent_id=...)`.
4. **Report** — Return the new page's title, ID, and URL.

## When Editing a Page

When the user wants to change an existing page:

1. **Read current content first** — Call `confluence_view(page_id)` so you understand the existing body before overwriting it. `confluence_update` REPLACES the body — it does not append.
2. **Apply the edit** — Call `confluence_update(page_id, body=..., title=...)`. The version number is bumped automatically.
3. To only rename, pass `title` without `body`. To only change content, pass `body` without `title`.

## Storage Format Reference

Confluence stores page bodies as XHTML ("storage format"). Common elements:

| Element | Storage format |
|---------|----------------|
| Paragraph | `<p>text</p>` |
| Heading | `<h2>Heading</h2>` |
| Bold | `<strong>text</strong>` |
| Italic | `<em>text</em>` |
| Bullet list | `<ul><li>item</li></ul>` |
| Numbered list | `<ol><li>item</li></ol>` |
| Link | `<a href="URL">text</a>` |
| Inline code | `<code>text</code>` |
| Code block (macro) | `<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[...]]></ac:plain-text-body></ac:structured-macro>` |
| Info panel (macro) | `<ac:structured-macro ac:name="info"><ac:rich-text-body><p>...</p></ac:rich-text-body></ac:structured-macro>` |

If you only have plain text, pass it directly — the server wraps it in a `<p>` for you. For any real structure, emit storage-format XHTML.

## Comments and Labels

- **Add a comment** → `confluence_add_comment(page_id, body)` (plain text or storage-format XHTML)
- **Add a label** → `confluence_add_label(page_id, label)` — useful for tagging pages into categories

## Safety

- Editing is destructive to the previous body (it replaces, not appends). Always read the page first and confirm large rewrites with the user.
- Creating/updating pages is a write to a shared knowledge base. For bulk operations or edits to widely-used pages, confirm with the user before proceeding.
