from pathlib import Path


STATIC_DIR = Path("static")
INDEX_HTML = STATIC_DIR / "index.html"


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
        assert f"/static/{asset}" in html

    assert not (STATIC_DIR / "js" / "common.js").exists()
    assert "class TagInput" not in html
    assert "async function loadChannels" not in html
    assert "<style>" not in html
