from pathlib import Path


STATIC_JS = Path("static/js")
REQUESTS_FRAGMENT = Path("static/fragments/admin/requests.html")


def test_switching_to_requests_does_not_read_request_filters_before_fragment_loads():
    admin_js = (STATIC_JS / "admin.js").read_text(encoding="utf-8")

    assert "function updateRequestHashSafely()" in admin_js
    assert "if (typeof syncRequestHash === 'function' && document.getElementById('reqFilterModel'))" in admin_js
    assert "if (tab === 'requests') {\n            syncRequestHash();" not in admin_js


def test_switch_tab_updates_desktop_active_state():
    admin_js = (STATIC_JS / "admin.js").read_text(encoding="utf-8")

    assert "function updateTabActiveState(tab)" in admin_js
    assert "document.querySelectorAll('[id^=\"tab_\"]')" in admin_js
    assert "button.classList.toggle('tab-active', isActive)" in admin_js
    assert "button.classList.toggle('tab-inactive', !isActive)" in admin_js
    assert "updateTabActiveState(tab);" in admin_js


def test_request_time_conversion_helpers_are_defined_once():
    requests_js = (STATIC_JS / "requests.js").read_text(encoding="utf-8")

    assert requests_js.count("function localInputToUtcIso(") == 1
    assert requests_js.count("function utcIsoToLocalInput(") == 1


def test_requests_tab_has_api_key_name_column_and_filter():
    requests_js = (STATIC_JS / "requests.js").read_text(encoding="utf-8")
    requests_html = REQUESTS_FRAGMENT.read_text(encoding="utf-8")

    assert 'id="reqFilterApiKeyId"' in requests_html
    assert ">API Key<" in requests_html
    assert 'data-label="API Key"' in requests_js
    assert "req.api_key_name || req.api_key_id || '-'" in requests_js
    assert "loadRequestApiKeys" in requests_js


def test_requests_tab_caches_api_key_filter_options_until_invalidated():
    requests_js = (STATIC_JS / "requests.js").read_text(encoding="utf-8")
    apikeys_js = (STATIC_JS / "apikeys.js").read_text(encoding="utf-8")

    assert "let requestApiKeysLoaded = false;" in requests_js
    assert "if (!force && requestApiKeysLoaded) return;" in requests_js
    assert "requestApiKeysLoaded = true;" in requests_js
    assert "function invalidateRequestApiKeys()" in requests_js
    assert "invalidateRequestApiKeys" in apikeys_js
