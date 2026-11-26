"""Tool-level tests for confluence_mcp.py using a mocked HTTP layer.

These tests monkeypatch `_api_request` so no network or real token is needed.
They cover all 19 MCP tools, the response summarizers, storage->text
conversion, and error/edge-case paths.

Run:  uvx --from fastmcp python server/test_tools_confluence_mcp.py
"""
import os
import sys
import json
import pathlib

os.environ["_CONFLUENCE_MCP_SKIP_ENV"] = "1"
# Provide dummy config so _check_config passes and module constants are set.
os.environ.setdefault("CONFLUENCE_PAT", "dummy-token")
os.environ.setdefault("CONFLUENCE_BASE_URL", "https://confluence.example.com")
os.environ.setdefault("CONFLUENCE_SPACE", "TEST")

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import confluence_mcp as c  # noqa: E402

# The module read env at import for its constants; ensure they reflect dummy cfg.
c.CONFLUENCE_BASE_URL = "https://confluence.example.com"
c.CONFLUENCE_BASE = "https://confluence.example.com/rest/api"
c.CONFLUENCE_VIEW = "https://confluence.example.com/pages/viewpage.action?pageId="
c.SPACE = "TEST"


# ── mock harness ─────────────────────────────────────────────────────────────
class MockAPI:
    """Records requests and returns queued responses."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, resp):
        self.responses.append(resp)
        return self

    def __call__(self, url, method="GET", payload=None):
        self.calls.append({"url": url, "method": method, "payload": payload})
        if self.responses:
            return self.responses.pop(0)
        return {}


def with_mock(fn):
    """Decorator: install a fresh MockAPI as _api_request for the test."""
    def wrapper():
        mock = MockAPI()
        orig = c._api_request
        c._api_request = mock
        try:
            fn(mock)
        finally:
            c._api_request = orig
    wrapper.__name__ = fn.__name__
    return wrapper


PAGE = {
    "id": "123",
    "type": "page",
    "title": "Sample Page",
    "space": {"key": "TEST"},
    "version": {"number": 5, "when": "2025-01-02T03:04:05.000Z",
                "by": {"displayName": "Jane Doe"}},
    "ancestors": [{"title": "Home"}, {"title": "Section"}],
    "metadata": {"labels": {"results": [{"name": "alpha"}, {"name": "beta"}]}},
    "body": {"storage": {"value": "<h1>Title</h1><p>Hello <b>world</b></p>"
                                  "<ul><li>one</li><li>two</li></ul>"}},
}


# ── helper-level tests ───────────────────────────────────────────────────────
def test_storage_to_text_basic():
    txt = c._storage_to_text("<p>Hello <strong>bold</strong></p><p>Second</p>")
    assert "Hello" in txt and "bold" in txt and "Second" in txt
    assert "<" not in txt  # tags stripped


def test_storage_to_text_entities_and_lists():
    txt = c._storage_to_text("<ul><li>a &amp; b</li></ul>")
    assert "a & b" in txt
    assert "- a & b" in txt or "a & b" in txt


def test_storage_to_text_truncation():
    big = "<p>" + ("x" * 30000) + "</p>"
    txt = c._storage_to_text(big, max_len=1000)
    assert txt.endswith("[truncated]")
    assert len(txt) < 1100


def test_storage_to_text_empty():
    assert c._storage_to_text("") == ""


def test_summarize_page_full():
    s = c._summarize_page(PAGE, include_body=True)
    assert s["id"] == "123"
    assert s["title"] == "Sample Page"
    assert s["space"] == "TEST"
    assert s["version"] == 5
    assert s["last_updated"] == "2025-01-02"
    assert s["updated_by"] == "Jane Doe"
    assert s["ancestors"] == ["Home", "Section"]
    assert s["labels"] == ["alpha", "beta"]
    assert "Hello" in s["body"]
    assert s["url"].endswith("pageId=123")


def test_summarize_page_no_body():
    s = c._summarize_page(PAGE, include_body=False)
    assert "body" not in s


def test_summarize_page_missing_fields():
    # Robust against sparse API objects.
    s = c._summarize_page({"id": "9"}, include_body=True)
    assert s["id"] == "9"
    assert s["title"] == ""
    assert s["labels"] == []
    assert s["ancestors"] == []


# ── tool tests (mocked) ──────────────────────────────────────────────────────
@with_mock
def test_view(mock):
    mock.queue(PAGE)
    out = c.confluence_view("123")
    assert out["id"] == "123"
    assert mock.calls[0]["url"].endswith("/content/123?expand=" + c.EXPAND_FULL)


@with_mock
def test_view_rejects_bad_id(mock):
    try:
        c.confluence_view("not-a-number")
        assert False
    except ValueError:
        pass
    assert mock.calls == []  # never hit the API


@with_mock
def test_get_by_title_found(mock):
    mock.queue({"results": [PAGE]})
    out = c.confluence_get_by_title("Sample Page")
    assert out["id"] == "123"
    assert "spaceKey=TEST" in mock.calls[0]["url"]
    assert "title=Sample%20Page" in mock.calls[0]["url"]


@with_mock
def test_get_by_title_not_found_raises(mock):
    mock.queue({"results": []})
    try:
        c.confluence_get_by_title("Nope")
        assert False
    except RuntimeError as e:
        assert "No page titled" in str(e)


@with_mock
def test_search_maps_results(mock):
    mock.queue({"results": [PAGE]})
    out = c.confluence_search("space=TEST AND type=page")
    assert out[0]["id"] == "123"
    assert out[0]["title"] == "Sample Page"


@with_mock
def test_search_limit_clamped(mock):
    mock.queue({"results": []})
    c.confluence_search("space=TEST", limit=9999)
    assert "limit=100" in mock.calls[0]["url"]  # hard cap 100


@with_mock
def test_search_rejects_control_chars(mock):
    try:
        c.confluence_search("space=TEST\x00")
        assert False
    except ValueError:
        pass
    assert mock.calls == []


@with_mock
def test_text_search_builds_scoped_cql(mock):
    mock.queue({"results": []})
    c.confluence_text_search('hello "world"')
    url = mock.calls[0]["url"]
    # Escaped quotes are URL-encoded; ensure space scoping present.
    assert "space%3DTEST" in url or "space=TEST" in url


@with_mock
def test_list_pages(mock):
    mock.queue({"results": [PAGE]})
    out = c.confluence_list_pages()
    assert out[0]["id"] == "123"


@with_mock
def test_children(mock):
    mock.queue({"results": [PAGE]})
    out = c.confluence_children("123")
    assert out[0]["id"] == "123"
    assert "/content/123/child/page" in mock.calls[0]["url"]


@with_mock
def test_attachments(mock):
    mock.queue({"results": [{
        "id": "att1", "title": "file.png",
        "extensions": {"mediaType": "image/png", "fileSize": 42},
        "_links": {"download": "/download/attachments/123/file.png"},
    }]})
    out = c.confluence_attachments("123")
    assert out[0]["id"] == "att1"
    assert out[0]["media_type"] == "image/png"
    assert out[0]["file_size"] == 42
    assert out[0]["download_url"].startswith("https://confluence.example.com")


@with_mock
def test_labels(mock):
    mock.queue({"results": [{"name": "x"}, {"name": "y"}]})
    out = c.confluence_labels("123")
    assert out == ["x", "y"]


@with_mock
def test_spaces(mock):
    mock.queue({"results": [{"key": "TEST", "name": "Test Space", "type": "global"}]})
    out = c.confluence_spaces()
    assert out[0]["key"] == "TEST"


@with_mock
def test_create_text_format_escapes(mock):
    mock.queue({"id": "500", "title": "New"})
    c.confluence_create("New", "a < b", body_format="text")
    sent = mock.calls[0]["payload"]["body"]["storage"]["value"]
    assert sent == "<p>a &lt; b</p>"
    assert mock.calls[0]["method"] == "POST"


@with_mock
def test_create_storage_format_verbatim(mock):
    mock.queue({"id": "501", "title": "New"})
    c.confluence_create("New", "<p>hi</p>", body_format="storage")
    sent = mock.calls[0]["payload"]["body"]["storage"]["value"]
    assert sent == "<p>hi</p>"


@with_mock
def test_create_with_parent(mock):
    mock.queue({"id": "502", "title": "Child"})
    c.confluence_create("Child", "body", parent_id="123")
    assert mock.calls[0]["payload"]["ancestors"] == [{"id": "123"}]


@with_mock
def test_create_rejects_bad_parent(mock):
    try:
        c.confluence_create("Child", "body", parent_id="bad")
        assert False
    except ValueError:
        pass


@with_mock
def test_update_bumps_version(mock):
    mock.queue({"version": {"number": 7}, "title": "Old", "type": "page"})  # GET current
    mock.queue({"id": "123", "title": "Old", "version": {"number": 8}})     # PUT
    out = c.confluence_update("123", body="new body")
    put = mock.calls[1]
    assert put["method"] == "PUT"
    assert put["payload"]["version"]["number"] == 8
    assert out["version"] == 8


@with_mock
def test_update_nothing_raises(mock):
    try:
        c.confluence_update("123")
        assert False
    except ValueError as e:
        assert "Nothing to update" in str(e)
    assert mock.calls == []  # short-circuits before any API call


@with_mock
def test_add_comment(mock):
    mock.queue({"id": "c1"})
    out = c.confluence_add_comment("123", "nice page")
    assert out["comment_id"] == "c1"
    assert mock.calls[0]["payload"]["type"] == "comment"


@with_mock
def test_add_label_valid(mock):
    mock.queue({})
    out = c.confluence_add_label("123", "release")
    assert out["added_label"] == "release"


@with_mock
def test_add_label_rejects_space(mock):
    try:
        c.confluence_add_label("123", "has space")
        assert False
    except ValueError:
        pass
    assert mock.calls == []


# ── error-path tests for _api_request wrapper behavior ───────────────────────
def test_check_config_missing_raises(monkeypatch=None):
    saved = os.environ.get("CONFLUENCE_PAT")
    saved_base = c.CONFLUENCE_BASE_URL
    try:
        os.environ.pop("CONFLUENCE_PAT", None)
        try:
            c._check_config()
            assert False, "expected RuntimeError for missing PAT"
        except RuntimeError as e:
            assert "CONFLUENCE_PAT" in str(e)
    finally:
        if saved is not None:
            os.environ["CONFLUENCE_PAT"] = saved
        c.CONFLUENCE_BASE_URL = saved_base


def test_check_config_rejects_http():
    saved = c.CONFLUENCE_BASE_URL
    try:
        c.CONFLUENCE_BASE_URL = "http://insecure.example.com"
        try:
            c._check_config()
            assert False, "expected RuntimeError for non-HTTPS"
        except RuntimeError as e:
            assert "HTTPS" in str(e)
    finally:
        c.CONFLUENCE_BASE_URL = saved


# ── get_storage (raw, lossless body) ─────────────────────────────────────────
@with_mock
def test_get_storage_returns_raw_body(mock):
    mock.queue(PAGE)
    out = c.confluence_get_storage("123")
    assert out["id"] == "123"
    assert out["version"] == 5
    assert out["title"] == "Sample Page"
    # Raw storage returned verbatim, NOT tag-stripped.
    assert out["body"] == PAGE["body"]["storage"]["value"]
    assert "<h1>" in out["body"]
    # Must request body.storage expansion.
    assert "body.storage" in mock.calls[0]["url"]


def test_get_storage_rejects_bad_id():
    try:
        c.confluence_get_storage("not-a-number")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── append (safe additive edit, no body reconstruction) ──────────────────────
@with_mock
def test_append_concatenates_and_bumps_version(mock):
    mock.queue(PAGE)              # GET current (with storage)
    mock.queue({"id": "123", "title": "Sample Page",
                "version": {"number": 6}})  # PUT result
    out = c.confluence_append("123", "<p>Added line.</p>", body_format="storage")
    put = mock.calls[1]
    assert put["method"] == "PUT"
    new_body = put["payload"]["body"]["storage"]["value"]
    # Original content preserved AND new content appended.
    assert PAGE["body"]["storage"]["value"] in new_body
    assert "<p>Added line.</p>" in new_body
    assert new_body.index(PAGE["body"]["storage"]["value"]) < new_body.index("<p>Added line.</p>")
    # Version incremented from current (5 -> 6).
    assert put["payload"]["version"]["number"] == 6
    assert out["version"] == 6


@with_mock
def test_append_text_format_is_escaped(mock):
    mock.queue(PAGE)
    mock.queue({"id": "123", "version": {"number": 6}})
    c.confluence_append("123", "a < b & c", body_format="text")
    new_body = mock.calls[1]["payload"]["body"]["storage"]["value"]
    assert "a &lt; b &amp; c" in new_body
    # Original preserved.
    assert PAGE["body"]["storage"]["value"] in new_body


@with_mock
def test_append_optional_heading(mock):
    mock.queue(PAGE)
    mock.queue({"id": "123", "version": {"number": 6}})
    c.confluence_append("123", "<p>x</p>", body_format="storage",
                        heading="Related links")
    new_body = mock.calls[1]["payload"]["body"]["storage"]["value"]
    assert "Related links" in new_body
    assert "<h2>" in new_body


def test_append_rejects_bad_id():
    try:
        c.confluence_append("x", "<p>y</p>")
        assert False, "expected ValueError"
    except ValueError:
        pass


@with_mock
def test_append_empty_body_raises(mock):
    try:
        c.confluence_append("123", "   ")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── upload_attachment (binary multipart) ─────────────────────────────────────
def test_upload_attachment_missing_file_raises():
    try:
        c.confluence_upload_attachment("123", "/nonexistent/file.bin")
        assert False, "expected error for missing file"
    except (FileNotFoundError, ValueError):
        pass


def test_upload_attachment_rejects_bad_id():
    try:
        c.confluence_upload_attachment("nope", __file__)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_api_multipart_sanitizes_filename_and_content_type():
    # A crafted filename / bogus content-type must not break the multipart
    # body or inject headers. We can't open a socket, so monkeypatch the
    # opener to capture the request bytes.
    import urllib.request as _ur
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    class _FakeOpener:
        def open(self, req, timeout=0):
            captured["body"] = req.data
            captured["headers"] = req.headers
            return _FakeResp()

    orig_opener = c._OPENER
    c._OPENER = _FakeOpener()
    try:
        c._api_multipart(
            "/content/1/child/attachment",
            {"comment": "ok"},
            'ev"il\r\nX-Inject: 1.txt',
            b"data",
            "not a mime type",
        )
    finally:
        c._OPENER = orig_opener
    raw = captured["body"]
    # Injected quote/CRLF stripped from filename; no header injection.
    assert b'X-Inject' not in raw.split(b"data")[0] or b'filename="evil' in raw
    assert b'\r\nX-Inject: 1' not in raw
    # Bogus content-type replaced with the safe default.
    assert b"application/octet-stream" in raw


def _upload_env(tmp):
    os.environ["CONFLUENCE_UPLOAD_DIRS"] = str(tmp)


def _run_upload(tmp, name, content, existing_results):
    """Upload `name` from tmp with stubbed lookup + multipart layers."""
    orig_mp, orig_api = c._api_multipart, c._api_request
    rec = {}

    def fake_multipart(path, fields, filename, file_bytes, content_type):
        rec.update(path=path, filename=filename, bytes=file_bytes)
        return {"results": [{"id": "att123", "title": filename}]}

    mock = MockAPI().queue({"results": existing_results})
    c._api_multipart, c._api_request = fake_multipart, mock
    _upload_env(tmp)
    try:
        f = tmp / name
        f.write_bytes(content)
        out = c.confluence_upload_attachment("123", str(f), comment="note")
    finally:
        c._api_multipart, c._api_request = orig_mp, orig_api
    return out, rec, mock


def test_upload_attachment_posts_multipart():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out, rec, _ = _run_upload(
            pathlib.Path(d).resolve(), "a.txt", b"hello attachment", []
        )
    assert rec["bytes"] == b"hello attachment"
    assert rec["path"] == "/content/123/child/attachment"
    assert out["attachments"][0]["id"] == "att123"


def test_upload_attachment_replaces_existing_filename():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out, rec, mock = _run_upload(
            pathlib.Path(d).resolve(), "a.txt", b"v2",
            [{"id": "att99", "title": "a.txt"}],
        )
    assert rec["path"] == "/content/123/child/attachment/att99/data"
    assert "filename=a.txt" in mock.calls[0]["url"]


def test_upload_attachment_rejects_outside_allowed_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as allowed, \
            tempfile.TemporaryDirectory() as other:
        _upload_env(pathlib.Path(allowed).resolve())
        f = pathlib.Path(other) / "x.txt"
        f.write_text("x")
        try:
            c.confluence_upload_attachment("123", str(f))
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_upload_attachment_rejects_symlink_escape_and_env_file():
    import tempfile
    with tempfile.TemporaryDirectory() as allowed, \
            tempfile.TemporaryDirectory() as other:
        root = pathlib.Path(allowed).resolve()
        _upload_env(root)
        secret = pathlib.Path(other) / "secret.txt"
        secret.write_text("s")
        link = root / "link.txt"
        link.symlink_to(secret)
        env = root / ".env"
        env.write_text("CONFLUENCE_PAT=x")
        for bad in (link, env):
            try:
                c.confluence_upload_attachment("123", str(bad))
                assert False, f"expected ValueError for {bad}"
            except ValueError:
                pass


def test_upload_attachment_bounded_read_rejects_oversize():
    import tempfile
    orig = c.MAX_ATTACHMENT_BYTES
    c.MAX_ATTACHMENT_BYTES = 4
    try:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d).resolve()
            _upload_env(root)
            f = root / "big.bin"
            f.write_bytes(b"12345")
            try:
                c.confluence_upload_attachment("123", str(f))
                assert False, "expected ValueError"
            except ValueError:
                pass
    finally:
        c.MAX_ATTACHMENT_BYTES = orig


@with_mock
def test_update_expected_version_conflict(mock):
    mock.queue({"id": "123", "title": "T", "version": {"number": 6}})
    try:
        c.confluence_update("123", "<p>x</p>", body_format="storage",
                            expected_version=5)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Version conflict" in str(e)
    assert all(call["method"] == "GET" for call in mock.calls)


@with_mock
def test_append_expected_version_conflict(mock):
    mock.queue({"id": "123", "title": "T", "version": {"number": 6},
                "body": {"storage": {"value": "<p>a</p>"}}})
    try:
        c.confluence_append("123", "more", expected_version=5)
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert all(call["method"] == "GET" for call in mock.calls)


# ── delete_attachment ────────────────────────────────────────────────────────
def test_delete_attachment_rejects_bad_id():
    for bad in ["", "   ", "att", "123/../x", "att 12", "<script>"]:
        try:
            c.confluence_delete_attachment(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


@with_mock
def test_delete_attachment_issues_delete(mock):
    mock.queue({})  # Confluence returns empty body (204) on delete.
    out = c.confluence_delete_attachment("att12345")
    assert len(mock.calls) == 1
    call = mock.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/content/att12345")
    assert out["id"] == "att12345"
    assert out["deleted"] is True


@with_mock
def test_delete_attachment_accepts_numeric_id(mock):
    mock.queue({})
    out = c.confluence_delete_attachment("987654")
    assert mock.calls[0]["url"].endswith("/content/987654")
    assert out["id"] == "987654"
    assert out["deleted"] is True


# ── comments: list ───────────────────────────────────────────────────────────
@with_mock
def test_comments_lists_and_renders_body(mock):
    mock.queue({"results": [
        {"id": "c1", "body": {"storage": {"value": "<p>See <b>here</b>.</p>"}},
         "version": {"when": "2024-08-30T10:00:00.000Z"}},
        {"id": "c2", "body": {"storage": {"value": "<p>Second</p>"}},
         "version": {"when": "2024-09-01T12:00:00.000Z"}},
    ]})
    out = c.confluence_comments("1114572201")
    assert mock.calls[0]["method"] == "GET"
    assert "/content/1114572201/child/comment" in mock.calls[0]["url"]
    assert "expand=body.storage,version" in mock.calls[0]["url"]
    assert isinstance(out, list) and len(out) == 2
    assert out[0]["id"] == "c1"
    assert out[0]["text"] == "See here."
    assert out[0]["body"] == "<p>See <b>here</b>.</p>"
    assert out[0]["last_updated"] == "2024-08-30"


@with_mock
def test_comments_empty(mock):
    mock.queue({"results": []})
    out = c.confluence_comments("123")
    assert out == []


@with_mock
def test_comments_limit_clamped(mock):
    mock.queue({"results": []})
    c.confluence_comments("123", limit=9999)
    assert "limit=100" in mock.calls[0]["url"]


def test_comments_rejects_bad_id():
    for bad in ["", "   ", "abc", "12/../x", "<script>"]:
        try:
            c.confluence_comments(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


# ── comments: delete ──────────────────────────────────────────────────────────
def test_delete_comment_rejects_bad_id():
    for bad in ["", "   ", "abc", "12 34", "123/../x", "<script>"]:
        try:
            c.confluence_delete_comment(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


@with_mock
def test_delete_comment_issues_delete(mock):
    mock.queue({})  # Confluence returns empty body (204) on delete.
    out = c.confluence_delete_comment("987654")
    assert len(mock.calls) == 1
    call = mock.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/content/987654")
    assert out["id"] == "987654"
    assert out["deleted"] is True


# ── self-runner ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
