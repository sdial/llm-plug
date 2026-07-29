from pathlib import Path

STATIC_DIR = Path("static")
INDEX_HTML = STATIC_DIR / "index.html"
LOGIN_HTML = STATIC_DIR / "admin-login.html"


def test_admin_assets_are_split_into_cohesive_modules():
    html = INDEX_HTML.read_text(encoding="utf-8")

    expected_assets = [
        "css/admin.css",
        "js/tailwind-config.js",
        "js/tag_input.js",
        "js/channels.js",
        "js/apikeys.js",
        "js/stats.js",
        "js/requests.js",
        "js/settings.js",
        "js/model_groups.js",
        "js/whitelist.js",
        "js/admin.js",
    ]

    for asset in expected_assets:
        assert (STATIC_DIR / asset).exists()
        assert f"/admin/static/{asset}" in html

    assert 'src="/static/' not in html
    assert 'href="/static/' not in html

    assert not (STATIC_DIR / "js" / "common.js").exists()
    assert "class TagInput" not in html
    assert "async function loadChannels" not in html
    assert "<style>" not in html


def test_admin_login_uses_shared_admin_styles_for_primary_button():
    html = LOGIN_HTML.read_text(encoding="utf-8")

    assert 'href="/admin/static/css/admin.css?v=' in html
    assert 'id="submitBtn" type="submit" class="btn-primary' in html


def test_admin_index_form_controls_have_accessible_names():
    html = INDEX_HTML.read_text(encoding="utf-8")

    labelled_controls = [
        "f_name",
        "f_api_type",
        "f_weight",
        "f_priority",
        "f_anthropic_version_policy",
        "f_anthropic_beta_policy",
        "fk_name",
        "testModelSelect",
    ]

    for control_id in labelled_controls:
        assert f'for="{control_id}"' in html

    assert 'id="tabMobileSelect" aria-label="选择管理页面"' in html


def test_admin_index_uses_css_classes_instead_of_inline_styles():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = (STATIC_DIR / "css" / "admin.css").read_text(encoding="utf-8")

    assert " style=" not in html
    assert ".modal-panel-shadow" in css


def test_admin_tool_pages_keep_offline_and_default_port_contracts():
    analyzer_html = (STATIC_DIR / "request-analyzer.html").read_text(encoding="utf-8")
    stream_test_html = (STATIC_DIR / "stream-test.html").read_text(encoding="utf-8")

    assert "cdnjs.cloudflare.com" not in analyzer_html
    assert "/admin/static/vendor/vs2015.min.css?v=" in analyzer_html
    assert "http://localhost:55555/v1/chat/completions" in stream_test_html
    assert "http://localhost:8000/v1/chat/completions" not in stream_test_html


def test_session_viewer_passes_event_explicitly_for_chunk_details():
    html = (STATIC_DIR / "session-viewer.html").read_text(encoding="utf-8")

    assert 'onclick="showChunkDetail(${index}, event)"' in html
    assert "function showChunkDetail(chunkIndex, evt)" in html
    assert "evt.target.closest('.turn')" in html
    assert "event.target.closest('.turn')" not in html


def test_channels_list_click_handler_is_bound_once_per_container():
    js = (STATIC_DIR / "js" / "channels.js").read_text(encoding="utf-8")

    assert "let lastChannelListContainer = null;" in js
    assert "if (container !== lastChannelListContainer)" in js
    assert js.count("container.addEventListener('click'") == 1
