"""Unit tests for the adversarial-review fixes in confluence_mcp.py.

These tests import the module as a library (no .env bootstrap, no network) by
setting _CONFLUENCE_MCP_SKIP_ENV=1 before import. They exercise the pure
helper functions that implement the security fixes.

Run:  uvx --from fastmcp python -m pytest server/test_confluence_mcp.py -q
  or: uvx --from fastmcp python server/test_confluence_mcp.py   (self-runner)
"""
import os
import sys
import pathlib

os.environ["_CONFLUENCE_MCP_SKIP_ENV"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import confluence_mcp as c  # noqa: E402


# ── HIGH #1: CQL escaping / injection ─────────────────────────────────────────
def test_escape_cql_backslash_first():
    # A trailing backslash must not combine with the quote-escape to break out.
    assert c._escape_cql_value("a\\") == "a\\\\"
    assert c._escape_cql_value('a"b') == 'a\\"b'
    # The classic breakout attempt: input ending in backslash then a quote.
    got = c._escape_cql_value('x\\" OR type=page')
    # backslash doubled, quote escaped -> no unescaped quote remains
    assert got == 'x\\\\\\" OR type=page'


def test_scope_cql_is_injection_safe():
    cql = c._scope_cql_to_space('foo" OR space=SECRET', "QAS")
    # The injected quote is escaped, so it cannot terminate the literal early.
    assert cql.startswith('space=QAS AND text ~ "')
    assert '\\"' in cql
    # No bare (unescaped) double quote before the closing one that would let
    # ' OR space=SECRET' escape into query structure.
    inner = cql.split('text ~ "', 1)[1].rsplit('"', 1)[0]
    # Every double-quote inside the literal must be backslash-escaped.
    for i, ch in enumerate(inner):
        if ch == '"':
            assert inner[i - 1] == "\\", "unescaped quote in CQL literal"


def test_scope_cql_rejects_bad_space():
    try:
        c._scope_cql_to_space("hello", "QAS; DROP")
        assert False, "expected ValueError for bad space key"
    except ValueError:
        pass


# ── HIGH #2 / MEDIUM #5: length caps ─────────────────────────────────────────
def test_cql_length_cap():
    try:
        c._validate_cql("a" * (c.MAX_CQL_LEN + 1))
        assert False, "expected ValueError"
    except ValueError:
        pass
    # Within cap is fine.
    assert c._validate_cql("space=QAS")


def test_text_length_cap():
    try:
        c._validate_text("a" * (c.MAX_TEXT_LEN + 1))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_title_length_and_control():
    try:
        c._validate_title("a" * (c.MAX_TITLE_LEN + 1))
        assert False
    except ValueError:
        pass
    try:
        c._validate_title("bad\x01title")
        assert False
    except ValueError:
        pass
    assert c._validate_title("Normal Title") == "Normal Title"


def test_label_validation():
    for bad in ["", "has space", "a" * (c.MAX_LABEL_LEN + 1), "null\x00"]:
        try:
            c._validate_label(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
    assert c._validate_label("  release  ") == "release"


def test_body_length_cap():
    try:
        c._validate_body("x" * (c.MAX_BODY_LEN + 1))
        assert False
    except ValueError:
        pass


# ── LOW #6: explicit body_format ─────────────────────────────────────────────
def test_wrap_body_text_always_escapes():
    # 'text' must escape even when it contains angle brackets.
    assert c._wrap_body("a < b & c", "text") == "<p>a &lt; b &amp; c</p>"


def test_wrap_body_storage_verbatim():
    xhtml = "<p>hello <strong>world</strong></p>"
    assert c._wrap_body(xhtml, "storage") == xhtml


def test_wrap_body_auto_heuristic():
    assert c._wrap_body("plain", "auto") == "<p>plain</p>"
    assert c._wrap_body("<p>x</p>", "auto") == "<p>x</p>"


def test_wrap_body_invalid_format():
    try:
        c._wrap_body("x", "bogus")
        assert False
    except ValueError:
        pass


# ── MEDIUM #3: base URL normalization ────────────────────────────────────────
def test_base_url_rstrip_logic():
    # Simulate the normalization the module applies at import.
    assert "https://x/y".rstrip("/") == "https://x/y"
    assert "https://x/y/".rstrip("/") == "https://x/y"
    assert "https://x/y///".rstrip("/") == "https://x/y"


# ── MEDIUM #4: redirect handler policy ───────────────────────────────────────
def test_redirect_handler_blocks_scheme_downgrade():
    import urllib.request, urllib.error, io
    h = c._NoRedirectSameHostHandler()
    req = urllib.request.Request("https://host.example/a")
    try:
        h.redirect_request(
            req, io.BytesIO(b""), 302, "Found", {}, "http://host.example/b"
        )
        assert False, "expected HTTPError for scheme downgrade"
    except urllib.error.HTTPError:
        pass


def test_redirect_handler_blocks_cross_host():
    import urllib.request, urllib.error, io
    h = c._NoRedirectSameHostHandler()
    req = urllib.request.Request("https://host.example/a")
    try:
        h.redirect_request(
            req, io.BytesIO(b""), 302, "Found", {}, "https://evil.example/b"
        )
        assert False, "expected HTTPError for cross-host redirect"
    except urllib.error.HTTPError:
        pass


def test_redirect_handler_allows_same_host_https():
    import urllib.request, io
    h = c._NoRedirectSameHostHandler()
    req = urllib.request.Request("https://host.example/a")
    # Same host + https should NOT raise; returns a new Request (or None).
    result = h.redirect_request(
        req, io.BytesIO(b""), 302, "Found", {}, "https://host.example/b"
    )
    assert result is None or isinstance(result, urllib.request.Request)


# ── LOW #7: .env malformed-quote warning ─────────────────────────────────────
def test_env_unterminated_quote_warns(capsys=None):
    # Capture stderr manually (no pytest dependency required).
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        val = c._parse_env_value('"unterminated', key="CONFLUENCE_PAT")
    assert val == "unterminated"
    assert "WARNING" in buf.getvalue()
    assert "CONFLUENCE_PAT" in buf.getvalue()


def test_env_well_formed_no_warning():
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        val = c._parse_env_value('"clean value"', key="K")
    assert val == "clean value"
    assert "WARNING" not in buf.getvalue()


# ── LOW #8: retry policy constants present ───────────────────────────────────
def test_retry_policy_defined():
    assert 429 in c._RETRY_STATUSES
    assert 503 in c._RETRY_STATUSES
    assert c._MAX_RETRIES >= 1


# ── self-runner (no pytest needed) ───────────────────────────────────────────
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
