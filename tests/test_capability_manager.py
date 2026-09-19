"""档案动作使用的纯结构辅助测试（ADR-0031）。"""

from capability_manager import merge_system_messages


def test_merge_system_messages_preserves_non_system_order():
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "system", "content": "s1"},
        {"role": "assistant", "content": "a1"},
        {"role": "system", "content": [{"type": "text", "text": "s2"}]},
    ]

    assert merge_system_messages(messages) == [
        {"role": "system", "content": "s1\n\ns2"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_merge_system_messages_does_not_mutate_input():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    original = [dict(message) for message in messages]

    merge_system_messages(messages)

    assert messages == original
