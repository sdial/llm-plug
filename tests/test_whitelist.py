import ipaddress
import os
import time

import whitelist as wl


# ─── load_rules ───

def test_load_rules_file_not_found():
    assert wl.load_rules("/nonexistent/whitelist.csv") == []


def test_load_rules_empty_file(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("")
    assert wl.load_rules(str(f)) == []


def test_load_rules_skips_comments(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("# this is a comment\n# another comment\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_skips_header(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("path_pattern,methods,ip_cidr,description\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_parses_wildcard_method(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("path_pattern,methods,ip_cidr,description\n/admin/*,*,10.0.0.0/8,内网\n", encoding="utf-8")
    rules = wl.load_rules(str(f))
    assert len(rules) == 1
    r = rules[0]
    assert r.path_pattern == "/admin/*"
    assert r.methods == frozenset()          # * → empty set means all
    assert str(r.network) == "10.0.0.0/8"
    assert r.description == "内网"


def test_load_rules_parses_method_filter(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,GET|POST,127.0.0.1,本机\n", encoding="utf-8")
    rules = wl.load_rules(str(f))
    assert rules[0].methods == frozenset({"GET", "POST"})


def test_load_rules_skips_invalid_cidr(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,not-an-ip,test\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_multiple_with_comments(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text(
        "# comment\n"
        "path_pattern,methods,ip_cidr,description\n"
        "/admin/*,*,10.1.1.0/24,内网\n"
        "\n"
        "# another comment\n"
        "/admin/stats,GET,203.0.113.5,公司\n",
        encoding="utf-8",
    )
    rules = wl.load_rules(str(f))
    assert len(rules) == 2


# ─── check_request ───

def _make_rules():
    return [
        wl.WhitelistRule(
            path_pattern="/admin/*",
            methods=frozenset(),
            network=ipaddress.ip_network("10.1.1.0/24"),
            description="内网",
        ),
        wl.WhitelistRule(
            path_pattern="/admin/stats",
            methods=frozenset({"GET"}),
            network=ipaddress.ip_network("203.0.113.5/32"),
            description="公司只读",
        ),
    ]


def test_check_no_rules_allows_all():
    ok, _ = wl.check_request([], "/admin/channels", "GET", "1.2.3.4")
    assert ok is True


def test_check_path_not_matched_allows():
    ok, _ = wl.check_request(_make_rules(), "/v1/chat/completions", "POST", "1.2.3.4")
    assert ok is True


def test_check_ip_not_in_cidr_returns_403():
    ok, reason = wl.check_request(_make_rules(), "/admin/channels", "GET", "192.168.1.1")
    assert ok is False
    assert "IP 白名单" in reason


def test_check_ip_in_cidr_allowed():
    ok, _ = wl.check_request(_make_rules(), "/admin/channels", "DELETE", "10.1.1.50")
    assert ok is True


def test_check_method_not_allowed_returns_403():
    ok, reason = wl.check_request(_make_rules(), "/admin/stats", "DELETE", "203.0.113.5")
    assert ok is False
    assert "DELETE" in reason


def test_check_method_allowed():
    ok, _ = wl.check_request(_make_rules(), "/admin/stats", "GET", "203.0.113.5")
    assert ok is True


def test_check_exact_ip_host_bits():
    """192.168.1.5 进入 192.168.1.0/24 应被允许"""
    rules = [
        wl.WhitelistRule(
            path_pattern="/admin/*",
            methods=frozenset(),
            network=ipaddress.ip_network("192.168.1.0/24"),
            description="test",
        )
    ]
    ok, _ = wl.check_request(rules, "/admin/x", "GET", "192.168.1.5")
    assert ok is True


def test_ipv4_mapped_ipv6_does_not_match_ipv4_network():
    # Python's ipaddress module does not coerce IPv4-mapped IPv6 addresses
    # (e.g. ::ffff:10.1.1.50) when checking membership in an IPv4Network.
    # The check raises TypeError, which check_request treats as a non-match
    # (fails closed — denies access). This test pins that safe-fail behavior.
    rule = wl.WhitelistRule(
        path_pattern="/admin/*",
        methods=frozenset(),
        network=ipaddress.IPv4Network("10.0.0.0/8"),
        description="内网",
    )
    ok, _ = wl.check_request([rule], "/admin/foo", "GET", "::ffff:10.1.1.50")
    assert ok is False


# ─── validate_rules_text ───

def test_validate_empty_text():
    ok, _, rules = wl.validate_rules_text("")
    assert ok is True
    assert rules == []


def test_validate_valid_text():
    text = "path_pattern,methods,ip_cidr,description\n/admin/*,*,10.0.0.0/8,内网\n"
    ok, err, rules = wl.validate_rules_text(text)
    assert ok is True
    assert err == ""
    assert len(rules) == 1


def test_validate_bad_column_count():
    ok, err, _ = wl.validate_rules_text("/admin/*,*,10.0.0.0/8\n")
    assert ok is False
    assert "4 列" in err


def test_validate_bad_cidr():
    ok, err, _ = wl.validate_rules_text("/admin/*,*,bad-ip,test\n")
    assert ok is False
    assert "bad-ip" in err


def test_validate_line_number_with_preceding_comments():
    text = "# comment 1\n# comment 2\n\n/admin/*,*,10.0.0.0/8\n"
    ok, err, _ = wl.validate_rules_text(text)
    assert ok is False
    assert "第 4 行" in err   # actual line 4 in the file


# ─── WhitelistCache ───

def test_cache_missing_file():
    cache = wl.WhitelistCache("/nonexistent/whitelist.csv")
    assert cache.get_rules() == []


def test_cache_hot_reload(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,10.0.0.0/8,内网\n", encoding="utf-8")
    cache = wl.WhitelistCache(str(f))

    rules1 = cache.get_rules()
    assert len(rules1) == 1

    # 写入新内容并强制 mtime 变化（文件系统精度可能 1s）
    time.sleep(0.05)
    f.write_text("")
    os.utime(str(f), (time.time() + 1, time.time() + 1))

    rules2 = cache.get_rules()
    assert len(rules2) == 0


def test_cache_no_reload_if_mtime_unchanged(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,10.0.0.0/8,内网\n", encoding="utf-8")
    cache = wl.WhitelistCache(str(f))
    rules1 = cache.get_rules()
    rules2 = cache.get_rules()
    assert rules1 is rules2   # same list object = no reload
