from proxy.errors import SensitiveBlockError


def test_sensitive_block_error_carries_triggered():
    exc = SensitiveBlockError("blocked", triggered=["CN_PHONE_NUMBER"])
    assert str(exc) == "blocked"
    assert exc.triggered == ["CN_PHONE_NUMBER"]
