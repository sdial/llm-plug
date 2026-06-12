"""P1-7: config._validate_iana_timezone 直接测试"""

import pytest

from config import _validate_iana_timezone


class TestValidateIanaTimezone:

    def test_empty_string_is_valid(self):
        """空字符串视为合法（使用系统默认）"""
        _validate_iana_timezone("")  # 不应抛出

    def test_valid_asia_shanghai(self):
        _validate_iana_timezone("Asia/Shanghai")

    def test_valid_america_new_york(self):
        _validate_iana_timezone("America/New_York")

    def test_valid_europe_london(self):
        _validate_iana_timezone("Europe/London")

    def test_valid_utc(self):
        _validate_iana_timezone("UTC")

    def test_valid_pacific_auckland(self):
        _validate_iana_timezone("Pacific/Auckland")

    def test_invalid_timezone_raises(self):
        with pytest.raises(ValueError, match="不是有效的 IANA 时区名"):
            _validate_iana_timezone("Invalid/Zone")

    def test_garbage_string_raises(self):
        with pytest.raises(ValueError):
            _validate_iana_timezone("not-a-timezone")

    def test_numeric_string_raises(self):
        with pytest.raises(ValueError):
            _validate_iana_timezone("12345")

    def test_partial_name_raises(self):
        with pytest.raises(ValueError):
            _validate_iana_timezone("Asia")

    def test_whitespace_raises(self):
        with pytest.raises(ValueError):
            _validate_iana_timezone("  Asia/Shanghai  ")

    def test_case_sensitive(self):
        """IANA 时区名大小写敏感"""
        with pytest.raises(ValueError):
            _validate_iana_timezone("asia/shanghai")
