"""routers/auth.py 独立测试

覆盖 check_proxy_authorization 函数的所有路径：
1. request_state 为 None
2. request_state 缺少 proxy_auth_checked 属性
3. proxy_auth_checked 为 False
4. proxy_auth_checked 为 True
5. proxy_auth_checked 为非 bool truthy/falsy 值
"""

from routers.auth import check_proxy_authorization


class TestCheckProxyAuthorization:
    def test_none_state_returns_false(self):
        """request_state 为 None 时返回 False"""
        assert check_proxy_authorization("Bearer token", None) is False

    def test_missing_attribute_returns_false(self):
        """request_state 缺少 proxy_auth_checked 属性时返回 False"""
        state = object()
        assert check_proxy_authorization("Bearer token", state) is False

    def test_false_flag_returns_false(self):
        """proxy_auth_checked 为 False 时返回 False"""
        state = type("State", (), {"proxy_auth_checked": False})()
        assert check_proxy_authorization("Bearer token", state) is False

    def test_true_flag_returns_true(self):
        """proxy_auth_checked 为 True 时返回 True"""
        state = type("State", (), {"proxy_auth_checked": True})()
        assert check_proxy_authorization("Bearer token", state) is True

    def test_none_authorization_with_true_flag(self):
        """authorization 为 None 但 flag 为 True 时仍应返回 True（flag 由中间件设置，与 header 无关）"""
        state = type("State", (), {"proxy_auth_checked": True})()
        assert check_proxy_authorization(None, state) is True

    def test_truthy_value_returns_true(self):
        """proxy_auth_checked 为 truthy 值（如 1）时返回 True"""
        state = type("State", (), {"proxy_auth_checked": 1})()
        assert check_proxy_authorization("Bearer token", state) is True

    def test_falsy_value_returns_false(self):
        """proxy_auth_checked 为 falsy 值（如 0、空字符串）时返回 False"""
        state = type("State", (), {"proxy_auth_checked": 0})()
        assert check_proxy_authorization("Bearer token", state) is False

        state2 = type("State", (), {"proxy_auth_checked": ""})()
        assert check_proxy_authorization("Bearer token", state2) is False

    def test_empty_authorization_string(self):
        """authorization 为空字符串时不影响结果"""
        state = type("State", (), {"proxy_auth_checked": True})()
        assert check_proxy_authorization("", state) is True

    def test_function_signature_accepts_positional(self):
        """函数应支持位置参数调用"""
        state = type("State", (), {"proxy_auth_checked": True})()
        result = check_proxy_authorization("Bearer x", state)
        assert result is True

    def test_function_signature_accepts_keyword(self):
        """函数应支持关键字参数调用"""
        state = type("State", (), {"proxy_auth_checked": True})()
        result = check_proxy_authorization(
            authorization="Bearer x", request_state=state
        )
        assert result is True
