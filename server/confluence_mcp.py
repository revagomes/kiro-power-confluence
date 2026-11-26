#!/usr/bin/env python3
"""
Confluence MCP Server — generic, space-agnostic.

Exposes Confluence page/space management as MCP tools over stdio transport.
Works with any Confluence instance (Cloud or Server/Data Center) via the
REST API (``/rest/api``).

Configuration via environment variables:
    CONFLUENCE_PAT       — (required) Personal access token (Bearer)
    CONFLUENCE_BASE_URL  — (required) Confluence instance URL,
                           e.g. https://confluence.example.com
    CONFLUENCE_SPACE     — (required) Default space key, e.g. MYSPACE

Run via:
    uvx --from fastmcp fastmcp run server/confluence_mcp.py
"""

import html
import json
import mimetypes
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from fastmcp import FastMCP


# ── Environment bootstrap ─────────────────────────────────────────────────────
def _unquote(value: str) -> str:
    """Strip balanced surrounding quotes from a value (single or double)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        return value[1:-1]
    return value


def _parse_env_value(raw: str, key: str = "") -> str:
    """Parse a .env value: strip inline comments (outside quotes), then unquote.

    Inline comments are recognised as ' #' (space + hash) only when the value
    is not quoted.  Quoted values are returned verbatim (minus the quotes).

    A value that opens a quote but never closes it is malformed; we warn on
    stderr (rather than silently accepting a mangled value) and return the
    remainder after the opening quote as a best effort.
    """
    raw = raw.strip()
    # If the value starts with a quote, find the matching closing quote.
    if raw and raw[0] in ("\"", "'"):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
        # No closing quote — malformed. Warn and best-effort strip the quote.
        print(
            f"[confluence-mcp] WARNING: malformed .env value for "
            f"{key or 'a key'}: unterminated {quote} quote; "
            f"using best-effort value.",
            file=sys.stderr,
        )
        return raw[1:]
    # Unquoted value: strip trailing inline comment (space + #).
    if " #" in raw:
        raw = raw[: raw.index(" #")]
    return raw.strip()


def _load_env() -> None:
    """Bootstrap environment variables from a .env file next to this script.

    Resolution order for each variable:
      1. Already set in os.environ (e.g. shell export or MCP env block)
      2. Loaded from a .env file next to this script
      3. Known aliases (CONFLUENCE_URL -> CONFLUENCE_BASE_URL)

    Supports:
      - Comments (lines starting with #)
      - Blank lines
      - export prefix (``export KEY=VALUE``)
      - Balanced single/double quoting
      - Inline comments (`` # ...``) for unquoted values

    Will NOT overwrite variables already present in the environment.

    Trust assumption:
      The .env file is trusted local-only configuration. It MUST NOT be
      writable by untrusted parties. The file grants control over
      CONFLUENCE_BASE_URL and CONFLUENCE_PAT — an attacker with write access
      to server/.env can redirect API calls and capture the token.
    """
    env_path = pathlib.Path(__file__).parent / ".env"
    if env_path.is_file():
        print(
            f"[confluence-mcp] Loading configuration from {env_path}",
            file=sys.stderr,
        )
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip optional 'export' prefix.
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            key = key.strip()
            value = _parse_env_value(raw_value, key)
            # Don't overwrite vars already in the environment.
            if key and key not in os.environ:
                os.environ[key] = value

    # Fallback: CONFLUENCE_URL -> CONFLUENCE_BASE_URL (common alias).
    if not os.environ.get("CONFLUENCE_BASE_URL") and os.environ.get(
        "CONFLUENCE_URL"
    ):
        os.environ["CONFLUENCE_BASE_URL"] = os.environ["CONFLUENCE_URL"]


# Only bootstrap .env when running as the MCP server (not when imported as a
# library, e.g. in tests).
if os.environ.get("_CONFLUENCE_MCP_SKIP_ENV") != "1":
    _load_env()

# ── Config (all from environment) ─────────────────────────────────────────────
# Normalise the base URL: strip trailing slashes so path concatenation never
# produces a double slash (e.g. ".../confluence//rest/api"), which some
# reverse proxies handle inconsistently.
CONFLUENCE_BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
CONFLUENCE_BASE = (
    f"{CONFLUENCE_BASE_URL}/rest/api" if CONFLUENCE_BASE_URL else ""
)
CONFLUENCE_VIEW = (
    f"{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId="
    if CONFLUENCE_BASE_URL
    else ""
)
SPACE = os.environ.get("CONFLUENCE_SPACE", "")

# Regex for validating numeric content/page IDs.
PAGE_ID_RE = re.compile(r"^\d+$")

# Attachment content IDs are either the ``att`` prefixed form (e.g. att12345)
# or a bare numeric content ID. Nothing else is a valid Confluence content ID.
ATTACHMENT_ID_RE = re.compile(r"^(?:att)?\d+$")
# Regex for validating a space key (letters, digits, underscore).
SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")

# ── Input length caps (defence against resource-abuse / oversized payloads) ────
# Free-text inputs are bounded before they are URL-encoded and shipped to
# Confluence. Values are generous for legitimate use but reject megabyte-scale
# abuse from a hostile MCP client.
MAX_CQL_LEN = 4096
MAX_TEXT_LEN = 2048
MAX_TITLE_LEN = 255      # Confluence's own page-title limit.
MAX_LABEL_LEN = 255
MAX_BODY_LEN = 5_000_000  # ~5 MB of storage-format XHTML.
MAX_ATTACHMENT_BYTES = 104_857_600  # 100 MB cap for attachment uploads.


def _upload_roots() -> list[pathlib.Path]:
    """Directories uploads may be read from (resolved, symlinks followed).

    Configured via CONFLUENCE_UPLOAD_DIRS (os.pathsep-separated). Defaults to
    the server's current working directory when unset.
    """
    raw = os.environ.get("CONFLUENCE_UPLOAD_DIRS", "").strip()
    entries = [e for e in raw.split(os.pathsep) if e.strip()] or [os.getcwd()]
    return [pathlib.Path(e).expanduser().resolve() for e in entries]


def _resolve_upload_path(file_path: str) -> pathlib.Path:
    """Resolve and authorize a local path for upload.

    Rejects paths outside the allowed upload directories (checked after
    resolving symlinks) and any credential file (``.env*`` files or anything
    inside the server's own directory that is a dotfile), so the PAT can never
    be uploaded to a shared page.
    """
    path = pathlib.Path(file_path).expanduser().resolve()
    server_dir = pathlib.Path(__file__).resolve().parent
    if path.name.lower().startswith(".env") or path == server_dir / ".env":
        raise ValueError("Refusing to upload credential/environment files.")
    if not any(path.is_relative_to(root) for root in _upload_roots()):
        raise ValueError(
            "File is outside the allowed upload directories. Set "
            "CONFLUENCE_UPLOAD_DIRS to authorize a directory."
        )
    if path.is_relative_to(server_dir) and path.name.startswith("."):
        raise ValueError("Refusing to upload hidden files from the server dir.")
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    return path

EXPAND_FULL = "body.storage,version,space,ancestors,metadata.labels,history"
EXPAND_LIST = "version,space"

# ── MCP Server ────────────────────────────────────────────────────────────────
_server_name = f"Confluence {SPACE}" if SPACE else "Confluence"
mcp = FastMCP(_server_name)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _check_config() -> None:
    """Validate that all required config is available.

    Checks the module-level constants (which are the values actually used by
    API calls) rather than live os.environ, to stay self-consistent.
    Also enforces HTTPS to prevent credential leakage over cleartext.
    """
    missing = []
    if not os.environ.get("CONFLUENCE_PAT"):
        missing.append("CONFLUENCE_PAT")
    if not CONFLUENCE_BASE_URL:
        missing.append("CONFLUENCE_BASE_URL")
    if not SPACE:
        missing.append("CONFLUENCE_SPACE")
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Set them in the mcp.json env block or export in your shell.\n"
            f"  Example:\n"
            f"    export CONFLUENCE_PAT=your-token\n"
            f"    export CONFLUENCE_BASE_URL=https://confluence.example.com\n"
            f"    export CONFLUENCE_SPACE=MYSPACE"
        )
    # Enforce HTTPS to prevent transmitting the Bearer token in cleartext.
    if CONFLUENCE_BASE_URL and not CONFLUENCE_BASE_URL.startswith("https://"):
        raise RuntimeError(
            "CONFLUENCE_BASE_URL must use HTTPS to protect credentials in "
            f"transit. Got: {CONFLUENCE_BASE_URL!r}. "
            "Change it to start with https:// ."
        )


def _validate_page_id(page_id: str) -> str:
    """Validate and normalize a Confluence numeric content/page ID."""
    normalized = str(page_id).strip()
    if not PAGE_ID_RE.match(normalized):
        raise ValueError(
            f"Invalid page ID: '{page_id}'. Expected a numeric content ID, "
            f"e.g. 123456. Use confluence_search or confluence_get_by_title "
            f"to resolve a title to an ID."
        )
    return normalized


def _validate_attachment_id(attachment_id: str) -> str:
    """Validate and normalize a Confluence attachment content ID.

    Accepts the ``att``-prefixed form (e.g. att12345) or a bare numeric
    content ID. Rejects anything else to prevent path traversal or injection
    into the REST path.
    """
    normalized = str(attachment_id).strip()
    if not ATTACHMENT_ID_RE.match(normalized):
        raise ValueError(
            f"Invalid attachment ID: '{attachment_id}'. Expected a content "
            f"ID like 'att12345' or a numeric ID. Use confluence_attachments "
            f"to list attachment IDs for a page."
        )
    return normalized


def _validate_space_key(space: str) -> str:
    """Validate and normalize a Confluence space key."""
    normalized = space.strip()
    if not SPACE_KEY_RE.match(normalized):
        raise ValueError(
            f"Invalid space key: '{space}'. Expected letters, digits, or "
            f"underscore only, e.g. MYSPACE."
        )
    return normalized


def _validate_cql(cql: str) -> str:
    """Validate a CQL string: reject control characters, null bytes, oversize.

    CQL is parsed server-side by Confluence. This client-side check prevents
    obviously malformed input from reaching the API.

    SECURITY NOTE: ``confluence_search`` accepts raw CQL by design. The
    configured ``CONFLUENCE_SPACE`` is a *default*, NOT a security boundary —
    a caller can query any space the PAT is permitted to read. Scoping to a
    single space must be enforced by the PAT's server-side permissions, or by
    using the space-scoped helpers (``_scope_cql_to_space``). See README.
    """
    if len(cql) > MAX_CQL_LEN:
        raise ValueError(
            f"CQL too long: {len(cql)} chars (max {MAX_CQL_LEN})."
        )
    if "\x00" in cql or any(
        c != "\n" and c != "\r" and c != "\t" and ord(c) < 32 for c in cql
    ):
        raise ValueError(
            "Invalid CQL: contains control characters or null bytes."
        )
    return cql


def _escape_cql_value(value: str) -> str:
    """Escape a user string for safe embedding inside a double-quoted CQL literal.

    Confluence CQL string literals use backslash escaping. The backslash MUST
    be escaped first, otherwise a trailing user backslash would combine with
    our added quote-escape and allow breakout from the quoted literal
    (e.g. input ending in ``\\`` -> ``\\\\"`` closes the string). Order:
      1. ``\\`` -> ``\\\\``
      2. ``"``  -> ``\\"``
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _scope_cql_to_space(text: str, space_key: str) -> str:
    """Build a space-scoped full-text CQL query from free text, injection-safe.

    The space key is validated (alphanumeric/underscore only) and the free
    text is escaped for a quoted CQL literal, so neither can break out of the
    query structure.
    """
    safe_space = _validate_space_key(space_key)
    safe_text = _escape_cql_value(_validate_text(text))
    return (
        f'space={safe_space} AND text ~ "{safe_text}" '
        f"ORDER BY lastmodified DESC"
    )


def _validate_text(text: str, max_len: int = MAX_TEXT_LEN) -> str:
    """Validate a free-text search string: length cap + reject null bytes."""
    if len(text) > max_len:
        raise ValueError(
            f"Search text too long: {len(text)} chars (max {max_len})."
        )
    if "\x00" in text:
        raise ValueError("Invalid search text: contains null bytes.")
    return text


def _validate_title(title: str) -> str:
    """Validate a page/comment title: length cap + reject control chars."""
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(
            f"Title too long: {len(title)} chars (max {MAX_TITLE_LEN})."
        )
    if "\x00" in title or any(ord(c) < 32 and c not in "\t" for c in title):
        raise ValueError("Invalid title: contains control characters.")
    return title


def _validate_label(label: str) -> str:
    """Validate a label name: length cap + reject whitespace/control chars.

    Confluence labels may not contain spaces; enforce a conservative charset.
    """
    label = label.strip()
    if not label:
        raise ValueError("Label must not be empty.")
    if len(label) > MAX_LABEL_LEN:
        raise ValueError(
            f"Label too long: {len(label)} chars (max {MAX_LABEL_LEN})."
        )
    if any(c.isspace() for c in label) or "\x00" in label:
        raise ValueError(
            "Invalid label: must not contain whitespace or null bytes."
        )
    return label


def _validate_body(body: str) -> str:
    """Validate a page/comment body: length cap only (content may be XHTML)."""
    if len(body) > MAX_BODY_LEN:
        raise ValueError(
            f"Body too long: {len(body)} bytes (max {MAX_BODY_LEN})."
        )
    return body


def _wrap_body(body: str, body_format: str = "auto") -> str:
    """Convert a body to Confluence storage format based on an explicit format.

    Args:
        body: The content.
        body_format: One of:
            - "text":    always treat as plain text (HTML-escaped, wrapped in <p>).
            - "storage": always treat as storage-format XHTML (sent verbatim).
            - "auto":    legacy heuristic — treat as storage iff it contains
                         '<', else plain text. Retained for backward-compat but
                         "text"/"storage" are preferred and unambiguous.
    """
    body = _validate_body(body)
    if body_format == "text":
        return f"<p>{html.escape(body)}</p>"
    if body_format == "storage":
        return body
    if body_format == "auto":
        return body if "<" in body else f"<p>{html.escape(body)}</p>"
    raise ValueError(
        f"Invalid body_format: {body_format!r}. "
        "Expected 'text', 'storage', or 'auto'."
    )


def _get_pat() -> str:
    """Get Confluence PAT from environment."""
    _check_config()
    return os.environ["CONFLUENCE_PAT"]


def _quote_path(value: str) -> str:
    """URL-encode a value for safe inclusion in a URL path or query parameter.

    Applies percent-encoding to all special characters (including /, ?, #, &)
    so the value cannot break out of its path segment or query parameter slot.
    """
    return urllib.parse.quote(value, safe="")


class _NoRedirectSameHostHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses unsafe redirects for a token-bearing client.

    The Authorization: Bearer header carries the PAT. A malicious or
    misconfigured endpoint could issue a 3xx to an http:// URL or a different
    host to capture that token. We block:
      - any scheme that is not https, and
      - any redirect to a different host than the original request.
    Same-host https redirects (e.g. trailing-slash normalisation) are allowed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(newurl)
        if new.scheme != "https":
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Refusing redirect to non-HTTPS URL (scheme={new.scheme!r}) "
                "on a token-bearing request.",
                headers, fp,
            )
        if new.netloc and new.netloc != old.netloc:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Refusing cross-host redirect ({old.netloc} -> {new.netloc}) "
                "on a token-bearing request.",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# A module-level opener that enforces the safe-redirect policy above.
_OPENER = urllib.request.build_opener(_NoRedirectSameHostHandler())

# Retry policy for transient responses (rate limiting / temporary outage).
_RETRY_STATUSES = {429, 503}
_MAX_RETRIES = 4


def _api_request(
    url: str, method: str = "GET", payload: dict | None = None
) -> dict | list:
    """Authenticated request to the Confluence REST API.

    Uses a redirect-blocking opener (no scheme downgrade / host change) and
    retries transient 429/503 responses with exponential backoff, honouring a
    ``Retry-After`` header when present.
    """
    pat = _get_pat()
    headers = {"Authorization": f"Bearer {pat}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    backoff = 1.0
    for attempt in range(_MAX_RETRIES + 1):
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers
        )
        try:
            # 30-second timeout bounds worst-case hang when Confluence is slow
            # or unreachable. Adequate for all normal API calls.
            with _OPENER.open(req, timeout=30) as resp:
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as e:
            # Transient? Back off and retry (unless we've exhausted attempts).
            if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else backoff
                except (TypeError, ValueError):
                    wait = backoff
                print(
                    f"[confluence-mcp] {e.code} transient; retrying in "
                    f"{wait:.1f}s (attempt {attempt + 1}/{_MAX_RETRIES}).",
                    file=sys.stderr,
                )
                time.sleep(wait)
                backoff *= 2
                continue
            body = e.read().decode() if e.fp else ""
            try:
                err = json.loads(body)
                # Surface only Confluence's user-facing error message, not raw
                # internals.
                msg = err.get("message", "")
                if msg:
                    raise RuntimeError(
                        f"Confluence API error ({e.code}): {msg}"
                    ) from e
                raise RuntimeError(
                    f"Confluence API error ({e.code}): request failed"
                ) from e
            except (json.JSONDecodeError, AttributeError):
                # Do not leak raw response body — it may contain internals.
                raise RuntimeError(
                    f"Confluence API error ({e.code}): request failed "
                    f"(non-JSON response from server)"
                ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection failed: {e.reason}") from e
    # Retries exhausted on a transient status.
    raise RuntimeError(
        "Confluence API error: request failed after "
        f"{_MAX_RETRIES} retries (rate limited or temporarily unavailable)."
    )


def _conf_get(path: str) -> dict | list:
    return _api_request(f"{CONFLUENCE_BASE}{path}")


def _conf_post(path: str, payload: dict) -> dict:
    return _api_request(f"{CONFLUENCE_BASE}{path}", method="POST", payload=payload)


def _conf_put(path: str, payload: dict) -> dict:
    return _api_request(f"{CONFLUENCE_BASE}{path}", method="PUT", payload=payload)


def _conf_delete(path: str) -> dict | list:
    return _api_request(f"{CONFLUENCE_BASE}{path}", method="DELETE")


def _api_multipart(
    path: str,
    fields: dict,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> dict | list:
    """Authenticated multipart/form-data POST for binary attachment uploads.

    Confluence's attachment endpoint requires multipart/form-data with a
    ``file`` part, plus the ``X-Atlassian-Token: no-check`` header (XSRF opt-out
    for API clients). Reuses the same redirect-blocking opener and Bearer auth
    as ``_api_request``; errors are sanitised the same way (no raw body leak).

    Args:
        path: API path beginning with '/', e.g. '/content/123/child/attachment'.
        fields: Extra text form fields (e.g. {"comment": "..."}).
        filename: Attachment file name shown in Confluence.
        file_bytes: Raw file content.
        content_type: MIME type of the file part.
    """
    pat = _get_pat()
    url = f"{CONFLUENCE_BASE}{path}"
    # Harden the file part against header/multipart injection: a filename
    # containing a quote, CR, LF or NUL could break out of the
    # Content-Disposition header. Strip those characters defensively.
    safe_filename = re.sub(r'["\r\n\x00]', "", filename) or "attachment"
    # Content-Type is attacker-influenced only via mimetypes guessing, but
    # validate it is a simple token/subtype to be safe.
    if not re.match(r"^[\w.+-]+/[\w.+-]+$", content_type):
        content_type = "application/octet-stream"
    # Build a multipart body with a random, collision-resistant boundary.
    boundary = f"----confluenceMCP{os.urandom(16).hex()}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        safe_name = re.sub(r'["\r\n\x00]', "", str(name))
        parts.append(f"--{boundary}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{safe_name}"'.encode()
        )
        parts.append(b"")
        # Form-field values must not contain the boundary; they are short,
        # validated strings (e.g. comment), so encode verbatim.
        parts.append(str(value).encode("utf-8"))
    # The file part.
    parts.append(f"--{boundary}".encode())
    parts.append(
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{safe_filename}"'
        ).encode("utf-8")
    )
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    body = crlf.join(parts) + crlf + file_bytes + crlf
    body += f"--{boundary}--".encode() + crlf

    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # Required by Confluence for attachment uploads via the REST API.
        "X-Atlassian-Token": "no-check",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        try:
            msg = json.loads(err_body).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            msg = ""
        if msg:
            raise RuntimeError(
                f"Confluence API error ({e.code}): {msg}"
            ) from e
        raise RuntimeError(
            f"Confluence API error ({e.code}): attachment upload failed"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection failed: {e.reason}") from e


def _storage_to_text(storage_html: str, max_len: int = 20000) -> str:
    """Best-effort conversion of Confluence storage-format HTML to plain text.

    Strips tags and unescapes entities so the agent gets readable content
    without HTML noise. Not a full converter — intended for summarisation.
    Truncates very large bodies to keep the response bounded.
    """
    if not storage_html:
        return ""
    # Drop block boundaries as newlines for readability.
    text = re.sub(
        r"</(p|h[1-6]|li|tr|div|table)>", "\n", storage_html, flags=re.I
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    # Strip all remaining tags.
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse excessive blank lines.
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "\n… [truncated]"
    return text


def _summarize_page(data: dict, include_body: bool = True) -> dict:
    """Build a compact summary dict from a Confluence content object."""
    version = data.get("version") or {}
    space = data.get("space") or {}
    ancestors = data.get("ancestors") or []
    labels = ((data.get("metadata") or {}).get("labels") or {}).get(
        "results", []
    )
    result = {
        "id": data.get("id", ""),
        "type": data.get("type", ""),
        "title": data.get("title", ""),
        "space": space.get("key", ""),
        "url": f"{CONFLUENCE_VIEW}{data.get('id', '')}",
        "version": version.get("number", ""),
        "last_updated": (version.get("when") or "")[:10],
        "updated_by": ((version.get("by") or {}).get("displayName", "")),
        "ancestors": [a.get("title", "") for a in ancestors],
        "labels": [l.get("name", "") for l in labels],
    }
    if include_body:
        storage = ((data.get("body") or {}).get("storage") or {}).get(
            "value", ""
        )
        result["body"] = _storage_to_text(storage)
    return result


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def confluence_view(page_id: str) -> dict:
    """View a Confluence page with full details including body text, version, labels, and ancestors.

    Args:
        page_id: Numeric content/page ID, e.g. 123456. Use confluence_search or
            confluence_get_by_title to resolve a title to an ID.
    """
    pid = _validate_page_id(page_id)
    data = _conf_get(f"/content/{pid}?expand={EXPAND_FULL}")
    return _summarize_page(data, include_body=True)


@mcp.tool()
def confluence_get_by_title(title: str, space: str = "") -> dict:
    """Fetch a page by its exact title within a space (defaults to the configured space).

    Args:
        title: Exact page title.
        space: Space key (optional, defaults to configured CONFLUENCE_SPACE).
    """
    space_key = _validate_space_key(space) if space else SPACE
    _check_config()
    q = (
        f"/content?spaceKey={_quote_path(space_key)}"
        f"&title={_quote_path(title)}&expand={EXPAND_FULL}"
    )
    data = _conf_get(q)
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        raise RuntimeError(
            f"No page titled '{title}' found in space '{space_key}'."
        )
    return _summarize_page(results[0], include_body=True)


@mcp.tool()
def confluence_search(cql: str, limit: int = 25) -> list[dict]:
    """Search Confluence content using CQL (Confluence Query Language).

    Args:
        cql: CQL query string, e.g. 'space=DOCS AND type=page AND title ~ "release"'
        limit: Maximum number of results (default 25, hard cap 100)
    """
    _validate_cql(cql)
    limit = max(1, min(limit, 100))
    q = (
        f"/content/search?cql={_quote_path(cql)}"
        f"&limit={limit}&expand={EXPAND_LIST}"
    )
    data = _conf_get(q)
    results = data.get("results", []) if isinstance(data, dict) else []
    out = []
    for r in results:
        version = r.get("version") or {}
        space = r.get("space") or {}
        out.append({
            "id": r.get("id", ""),
            "type": r.get("type", ""),
            "title": r.get("title", ""),
            "space": space.get("key", ""),
            "url": f"{CONFLUENCE_VIEW}{r.get('id', '')}",
            "last_updated": (version.get("when") or "")[:10],
        })
    return out


@mcp.tool()
def confluence_text_search(text: str, limit: int = 25) -> list[dict]:
    """Full-text search across the configured space (convenience wrapper over CQL).

    Args:
        text: Free-text search terms.
        limit: Maximum number of results (default 25)
    """
    # Build an injection-safe, space-scoped CQL query. Escaping handles
    # backslash-then-quote breakout; the space key is validated separately.
    cql = _scope_cql_to_space(text, SPACE)
    return confluence_search(cql, limit=limit)


@mcp.tool()
def confluence_list_pages(space: str = "", limit: int = 50) -> list[dict]:
    """List pages in a space (defaults to the configured space), most-recent first.

    Args:
        space: Space key (optional, defaults to configured CONFLUENCE_SPACE).
        limit: Maximum number of results (default 50)
    """
    space_key = _validate_space_key(space) if space else SPACE
    cql = f"space={space_key} AND type=page ORDER BY lastmodified DESC"
    return confluence_search(cql, limit=limit)


@mcp.tool()
def confluence_children(page_id: str, limit: int = 50) -> list[dict]:
    """List the direct child pages of a page.

    Args:
        page_id: Parent page ID.
        limit: Maximum number of results (default 50)
    """
    pid = _validate_page_id(page_id)
    limit = max(1, min(limit, 100))
    data = _conf_get(
        f"/content/{pid}/child/page?limit={limit}&expand={EXPAND_LIST}"
    )
    results = data.get("results", []) if isinstance(data, dict) else []
    out = []
    for r in results:
        version = r.get("version") or {}
        out.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "url": f"{CONFLUENCE_VIEW}{r.get('id', '')}",
            "last_updated": (version.get("when") or "")[:10],
        })
    return out


@mcp.tool()
def confluence_attachments(page_id: str, limit: int = 50) -> list[dict]:
    """List attachments on a page.

    Args:
        page_id: Page ID.
        limit: Maximum number of results (default 50)
    """
    pid = _validate_page_id(page_id)
    limit = max(1, min(limit, 100))
    data = _conf_get(
        f"/content/{pid}/child/attachment?limit={limit}&expand=version"
    )
    results = data.get("results", []) if isinstance(data, dict) else []
    out = []
    for r in results:
        meta = r.get("metadata") or {}
        ext = r.get("extensions") or {}
        download = (r.get("_links") or {}).get("download", "")
        out.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "media_type": ext.get("mediaType", meta.get("mediaType", "")),
            "file_size": ext.get("fileSize", ""),
            "download_url": (
                f"{CONFLUENCE_BASE_URL}{download}" if download else ""
            ),
        })
    return out


@mcp.tool()
def confluence_labels(page_id: str) -> list[str]:
    """List the labels attached to a page.

    Args:
        page_id: Page ID.
    """
    pid = _validate_page_id(page_id)
    data = _conf_get(f"/content/{pid}/label")
    results = data.get("results", []) if isinstance(data, dict) else []
    return [r.get("name", "") for r in results]


@mcp.tool()
def confluence_spaces(limit: int = 50) -> list[dict]:
    """List spaces available to the configured account.

    Args:
        limit: Maximum number of results (default 50)
    """
    _check_config()
    limit = max(1, min(limit, 100))
    data = _conf_get(f"/space?limit={limit}")
    results = data.get("results", []) if isinstance(data, dict) else []
    return [
        {
            "key": s.get("key", ""),
            "name": s.get("name", ""),
            "type": s.get("type", ""),
        }
        for s in results
    ]


@mcp.tool()
def confluence_create(
    title: str,
    body: str,
    space: str = "",
    parent_id: str = "",
    body_format: str = "auto",
) -> dict:
    """Create a new Confluence page.

    Args:
        title: Page title.
        body: Page body. See body_format for how it is interpreted.
        space: Space key (optional, defaults to configured CONFLUENCE_SPACE).
        parent_id: Parent page ID to nest under (optional).
        body_format: How to interpret body — "text" (plain text, HTML-escaped),
            "storage" (Confluence storage-format XHTML, sent verbatim), or
            "auto" (legacy heuristic: storage iff it contains '<', else text).
            Prefer "text" or "storage" for unambiguous handling.
    """
    space_key = _validate_space_key(space) if space else SPACE
    _check_config()
    title = _validate_title(title)
    # Convert the body to storage format using an explicit format flag.
    storage_body = _wrap_body(body, body_format)
    payload: dict = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {"value": storage_body, "representation": "storage"}
        },
    }
    if parent_id:
        payload["ancestors"] = [{"id": _validate_page_id(parent_id)}]
    result = _conf_post("/content", payload)
    pid = result.get("id", "")
    return {
        "id": pid,
        "title": result.get("title", title),
        "url": f"{CONFLUENCE_VIEW}{pid}",
    }


def _check_expected_version(expected: int, current: int) -> None:
    if expected and expected != current:
        raise ValueError(
            f"Version conflict: page is at version {current}, expected "
            f"{expected}. Re-read the page and retry."
        )


@mcp.tool()
def confluence_update(
    page_id: str,
    body: str = "",
    title: str = "",
    body_format: str = "auto",
    expected_version: int = 0,
) -> dict:
    """Update an existing Confluence page's body and/or title.

    The version number is auto-incremented from the current version. For
    read-modify-write edits, pass the ``version`` returned by
    confluence_get_storage as expected_version (and body_format="storage"):
    the update is refused if the page changed in between.

    Args:
        page_id: Page ID to update.
        body: New body. See body_format. Optional if only changing the title.
        title: New title (optional).
        body_format: How to interpret body — "text", "storage", or "auto"
            (legacy heuristic). Prefer "text" or "storage".
        expected_version: If > 0, abort unless the page is still at this
            version (guards against overwriting concurrent edits).
    """
    pid = _validate_page_id(page_id)
    if not body and not title:
        raise ValueError(
            "Nothing to update. Provide a new body and/or title."
        )
    if title:
        title = _validate_title(title)
    # Fetch current version + title to build the update payload.
    current = _conf_get(f"/content/{pid}?expand=version,space")
    current_version = (current.get("version") or {}).get("number", 0)
    _check_expected_version(expected_version, current_version)
    new_title = title or current.get("title", "")
    payload: dict = {
        "id": pid,
        "type": current.get("type", "page"),
        "title": new_title,
        "version": {"number": current_version + 1},
    }
    if body:
        storage_body = _wrap_body(body, body_format)
        payload["body"] = {
            "storage": {"value": storage_body, "representation": "storage"}
        }
    result = _conf_put(f"/content/{pid}", payload)
    return {
        "id": pid,
        "title": result.get("title", new_title),
        "version": (result.get("version") or {}).get("number", ""),
        "url": f"{CONFLUENCE_VIEW}{pid}",
    }


@mcp.tool()
def confluence_add_comment(
    page_id: str, body: str, body_format: str = "auto"
) -> dict:
    """Add a comment to a Confluence page.

    Args:
        page_id: Page ID to comment on.
        body: Comment body. See body_format.
        body_format: How to interpret body — "text", "storage", or "auto"
            (legacy heuristic). Prefer "text" or "storage".
    """
    pid = _validate_page_id(page_id)
    storage_body = _wrap_body(body, body_format)
    payload = {
        "type": "comment",
        "container": {"id": pid, "type": "page"},
        "body": {
            "storage": {"value": storage_body, "representation": "storage"}
        },
    }
    result = _conf_post("/content", payload)
    return {
        "comment_id": result.get("id", ""),
        "page_id": pid,
    }


@mcp.tool()
def confluence_comments(page_id: str, limit: int = 50) -> list[dict]:
    """List the comments on a Confluence page.

    Returns each comment's ID, a plain-text rendering of its body (for quick
    reading), the raw storage-format body (lossless), and when it was last
    updated. Use the returned ID with confluence_delete_comment to remove a
    comment.

    Args:
        page_id: Page ID whose comments to list.
        limit: Maximum number of results (default 50, hard cap 100).
    """
    pid = _validate_page_id(page_id)
    limit = max(1, min(limit, 100))
    data = _conf_get(
        f"/content/{pid}/child/comment"
        f"?limit={limit}&expand=body.storage,version"
    )
    results = data.get("results", []) if isinstance(data, dict) else []
    out = []
    for r in results:
        version = r.get("version") or {}
        storage = ((r.get("body") or {}).get("storage") or {}).get("value", "")
        out.append({
            "id": r.get("id", ""),
            "text": _storage_to_text(storage),
            "body": storage,
            "last_updated": (version.get("when") or "")[:10],
        })
    return out


@mcp.tool()
def confluence_delete_comment(comment_id: str) -> dict:
    """Delete a comment from a Confluence page by its comment ID.

    Removes the comment permanently (subject to the space's trash policy). Use
    confluence_comments to find the comment ID first.

    Args:
        comment_id: The numeric comment content ID. Use confluence_comments to
            list comment IDs for a page.
    """
    cid = _validate_page_id(comment_id)
    _check_config()
    _conf_delete(f"/content/{cid}")
    return {"id": cid, "deleted": True}


@mcp.tool()
def confluence_add_label(page_id: str, label: str) -> dict:
    """Add a label to a Confluence page.

    Args:
        page_id: Page ID.
        label: Label name to add (no whitespace; max 255 chars).
    """
    pid = _validate_page_id(page_id)
    label = _validate_label(label)
    _conf_post(f"/content/{pid}/label", {"prefix": "global", "name": label})
    return {"page_id": pid, "added_label": label}


@mcp.tool()
def confluence_get_storage(page_id: str) -> dict:
    """Get a page's RAW storage-format body (lossless), plus version and title.

    Unlike confluence_view (which strips tags and truncates for readability),
    this returns the exact Confluence storage-format XHTML. Use it as the basis
    for a safe edit: read the raw body, modify it, and write it back with
    confluence_update(body_format="storage", expected_version=<version>) —
    nothing is lost or truncated, and the write is refused if someone else
    edited the page meanwhile. Without expected_version, a stale body would
    silently overwrite concurrent edits; for purely additive changes prefer
    confluence_append.

    Args:
        page_id: Page ID.
    """
    pid = _validate_page_id(page_id)
    data = _conf_get(f"/content/{pid}?expand=body.storage,version,space")
    version = (data.get("version") or {}).get("number", "")
    storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
    return {
        "id": data.get("id", pid),
        "title": data.get("title", ""),
        "space": (data.get("space") or {}).get("key", ""),
        "version": version,
        "url": f"{CONFLUENCE_VIEW}{pid}",
        "body": storage,
    }


@mcp.tool()
def confluence_append(
    page_id: str,
    body: str,
    body_format: str = "auto",
    heading: str = "",
    expected_version: int = 0,
) -> dict:
    """Safely APPEND content to a page without rewriting its existing body.

    This is the data-loss-safe way to add to an existing page. It fetches the
    current RAW storage body, concatenates the new content at the end, and
    writes it back with an auto-incremented version. The caller never has to
    reconstruct (and risk clobbering) the existing content.

    Args:
        page_id: Page ID to append to.
        body: New content to add at the end of the page.
        body_format: How to interpret body — "text" (plain, HTML-escaped),
            "storage" (storage-format XHTML, verbatim), or "auto".
        heading: Optional H2 heading inserted above the new content.
        expected_version: If > 0, abort unless the page is still at this
            version.
    """
    pid = _validate_page_id(page_id)
    if not body or not body.strip():
        raise ValueError("Nothing to append. Provide a non-empty body.")
    addition = _wrap_body(body, body_format)
    if heading:
        heading = _validate_title(heading)
        addition = f"<h2>{html.escape(heading)}</h2>{addition}"
    # Fetch current raw storage + version (lossless).
    current = _conf_get(f"/content/{pid}?expand=body.storage,version,space")
    current_version = (current.get("version") or {}).get("number", 0)
    _check_expected_version(expected_version, current_version)
    existing = ((current.get("body") or {}).get("storage") or {}).get(
        "value", ""
    )
    title = current.get("title", "")
    new_body = existing + addition
    _validate_body(new_body)
    payload = {
        "id": pid,
        "type": current.get("type", "page"),
        "title": title,
        "version": {"number": current_version + 1},
        "body": {
            "storage": {"value": new_body, "representation": "storage"}
        },
    }
    result = _conf_put(f"/content/{pid}", payload)
    return {
        "id": pid,
        "title": result.get("title", title),
        "version": (result.get("version") or {}).get("number", ""),
        "url": f"{CONFLUENCE_VIEW}{pid}",
    }


@mcp.tool()
def confluence_upload_attachment(
    page_id: str,
    file_path: str,
    comment: str = "",
) -> dict:
    """Upload a local file as an attachment on a Confluence page.

    Reads the file from disk and uploads it via multipart/form-data. Only
    files inside CONFLUENCE_UPLOAD_DIRS (default: the server's working
    directory) are allowed, and credential files are always refused. If an
    attachment with the same filename already exists, its content is replaced
    (new attachment version). Returns the attachment metadata.

    Args:
        page_id: Page ID to attach the file to.
        file_path: Path to a local file inside an allowed upload directory.
        comment: Optional version comment for the attachment.
    """
    pid = _validate_page_id(page_id)
    _check_config()
    path = _resolve_upload_path(file_path)
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"File too large: {size} bytes (max {MAX_ATTACHMENT_BYTES}). "
            "Host large media externally and link to it instead."
        )
    # Bound the read so a file that grew after stat() cannot exhaust memory.
    with path.open("rb") as fh:
        file_bytes = fh.read(MAX_ATTACHMENT_BYTES + 1)
    if len(file_bytes) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"File too large (max {MAX_ATTACHMENT_BYTES} bytes). "
            "Host large media externally and link to it instead."
        )
    size = len(file_bytes)
    filename = path.name
    content_type = (
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    fields = {"minorEdit": "true"}
    if comment:
        fields["comment"] = _validate_text(comment)
    # Confluence rejects a create (POST) for an existing filename, so look it
    # up and post replacement bytes to the attachment's /data endpoint.
    existing = _conf_get(
        f"/content/{pid}/child/attachment?filename="
        f"{urllib.parse.quote(filename)}"
    )
    found = existing.get("results", []) if isinstance(existing, dict) else []
    found = [r for r in found if r.get("title") == filename and r.get("id")]
    if found:
        att_id = _validate_attachment_id(str(found[0]["id"]))
        target = f"/content/{pid}/child/attachment/{att_id}/data"
    else:
        target = f"/content/{pid}/child/attachment"
    data = _api_multipart(target, fields, filename, file_bytes, content_type)
    results = data.get("results", []) if isinstance(data, dict) else []
    # Some Confluence versions return the attachment object directly.
    if not results and isinstance(data, dict) and data.get("id"):
        results = [data]
    out = []
    for r in results:
        ext = r.get("extensions") or {}
        out.append({
            "id": r.get("id", ""),
            "title": r.get("title", filename),
            "media_type": ext.get("mediaType", content_type),
            "file_size": ext.get("fileSize", size),
        })
    return {"page_id": pid, "attachments": out}


@mcp.tool()
def confluence_delete_attachment(attachment_id: str) -> dict:
    """Delete an attachment from a Confluence page by its attachment ID.

    Removes the attachment content permanently (subject to the space's trash
    policy). Use ``confluence_attachments`` to find the attachment ID first.

    Args:
        attachment_id: The attachment content ID, e.g. 'att12345' or a numeric
            content ID.
    """
    aid = _validate_attachment_id(attachment_id)
    _check_config()
    _conf_delete(f"/content/{aid}")
    return {"id": aid, "deleted": True}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
