from pathlib import Path


STATIC_JS = Path("static/js")


def test_switching_to_requests_does_not_read_request_filters_before_fragment_loads():
    admin_js = (STATIC_JS / "admin.js").read_text(encoding="utf-8")

    assert "function updateRequestHashSafely()" in admin_js
    assert "if (typeof syncRequestHash === 'function' && document.getElementById('reqFilterModel'))" in admin_js
    assert "syncRequestHash();" not in admin_js


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
