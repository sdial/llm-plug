"""ResponseStreamState 直测（ADR-0022 D1）：脱离 converter 构造 state，钉死不变量方法。

覆盖两拍协议幂等（emit_created）、usage 等待状态机（queue_final → release_pending）、
空流伪造 ID 补偿（finalize）、文本→工具切换防护（emit_text_done）、
sequence_number 单调（next_seq）。端到端事件序列回归见 test_stream_sequences /
test_stream_protocol。
"""

from converters.response_stream_state import ResponseStreamState


def _state(**overrides) -> ResponseStreamState:
    state = ResponseStreamState()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class TestEmitCreated:
    def test_two_beat_created_then_in_progress_same_batch(self):
        state = _state(response_id="resp_abc", model="m1")
        events = state.emit_created()
        assert [e["type"] for e in events] == ["response.created", "response.in_progress"]
        assert events[0]["response"]["id"] == "resp_abc"
        assert events[0]["response"]["model"] == "m1"
        assert events[0]["response"]["status"] == "in_progress"
        assert events[1]["response"]["id"] == "resp_abc"

    def test_marks_created_sent_and_consumes_need_in_progress(self):
        state = _state(response_id="resp_abc")
        state.emit_created()
        assert state.response_created_sent is True
        # in_progress 同拍注入后标志即被消费，不会泄漏到下一拍
        assert state.need_in_progress is False

    def test_idempotent_created_sent_only_once(self):
        state = _state(response_id="resp_abc")
        assert state.emit_created()  # 首次发出
        assert state.emit_created() == []
        assert state.emit_created() == []

    def test_backfills_message_id_from_response_id(self):
        state = _state(response_id="resp_abc")
        state.emit_created()
        assert state.message_id == "resp_abc"

    def test_keeps_existing_message_id(self):
        state = _state(response_id="resp_abc", message_id="msg_xyz")
        state.emit_created()
        assert state.message_id == "msg_xyz"


class TestQueueFinalReleasePending:
    def _state_with_text_item(self) -> ResponseStreamState:
        state = _state(response_id="resp_abc", message_id="msg_abc", accumulated_text="hello")
        state.output_items.append({"type": "message", "output_index": 0, "item_id": "msg_abc"})
        state.item_texts[0] = "hello"
        state.active_text_item_id = "msg_abc"
        state.active_text_output_index = 0
        return state

    def test_queue_final_holds_completed_and_emits_done_sequence(self):
        state = self._state_with_text_item()
        events = state.queue_final("stop")
        # completed 被扣留：只发 done 序列（output_text.done + content_part.done + output_item.done）
        assert [e["type"] for e in events] == [
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
        ]
        assert state.pending_finish_reason == "stop"
        assert state.waiting_for_usage_after_finish is True
        assert state.completed_sent is False
        assert state.pending_final_events[-1]["type"] == "response.completed"

    def test_release_pending_backfills_latest_usage(self):
        state = self._state_with_text_item()
        state.queue_final("stop")
        state.input_tokens = 11
        state.output_tokens = 7
        state.total_tokens = 18
        released = state.release_pending()
        assert len(released) == 1
        completed = released[0]
        assert completed["type"] == "response.completed"
        assert completed["response"]["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
        assert completed["response"]["output_text"] == "hello"

    def test_release_pending_resets_waiting_state(self):
        state = self._state_with_text_item()
        state.queue_final("stop")
        state.release_pending()
        assert state.completed_sent is True
        assert state.waiting_for_usage_after_finish is False
        assert state.pending_final_events == []
        # 幂等：排空后再 release 无产出
        assert state.release_pending() == []

    def test_release_pending_backfills_usage_details(self):
        state = self._state_with_text_item()
        state.queue_final("stop")
        state.input_tokens = 3
        state.output_tokens = 4
        state.total_tokens = 7
        state.input_tokens_details = {"cached_tokens": 2}
        state.output_tokens_details = {"reasoning_tokens": 1}
        completed = state.release_pending()[0]
        assert completed["response"]["usage"]["input_tokens_details"] == {"cached_tokens": 2}
        assert completed["response"]["usage"]["output_tokens_details"] == {"reasoning_tokens": 1}


class TestFinalize:
    def test_empty_stream_forges_response_and_message_ids(self):
        state = ResponseStreamState()
        events = state.finalize()
        assert len(events) == 1
        completed = events[0]
        assert completed["type"] == "response.completed"
        response = completed["response"]
        assert state.response_id and state.response_id.startswith("resp_")
        assert response["id"] == state.response_id
        assert state.message_id.startswith("msg_")
        # 空流补偿：输出里补一条空文本 message item
        assert response["output"][0]["type"] == "message"
        assert response["output"][0]["content"] == [{"type": "output_text", "text": ""}]
        assert response["status"] == "completed"
        assert state.completed_sent is True

    def test_finalize_releases_pending_first(self):
        state = _state(response_id="resp_a", message_id="msg_a", waiting_for_usage_after_finish=True, pending_finish_reason="stop")
        state.pending_final_events = [{"type": "response.completed", "response": {"id": "resp_a", "usage": {"input_tokens": 0}}}]
        state.input_tokens = 5
        events = state.finalize()
        assert len(events) == 1
        assert events[0]["type"] == "response.completed"
        assert events[0]["response"]["usage"]["input_tokens"] == 5
        assert state.completed_sent is True

    def test_finalize_skips_when_already_completed(self):
        state = _state(response_id="resp_a", completed_sent=True)
        assert state.finalize() == []

    def test_finalize_length_finish_marks_incomplete(self):
        state = _state(response_id="resp_a", message_id="msg_a")
        state.output_items.append({"type": "message", "output_index": 0, "item_id": "msg_a"})
        state.item_texts[0] = "partial"
        state.completed_sent = False
        events = state.build_final_events("length")
        completed = events[-1]
        assert completed["response"]["status"] == "incomplete"
        assert completed["response"]["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_finalize_only_closes_the_active_text_output_index(self):
        state = _state(response_id="resp_a", message_id="msg_shared", active_text_item_id="msg_shared", active_text_output_index=1)
        state.output_items.extend(
            [
                {"type": "message", "output_index": 0, "item_id": "msg_shared"},
                {"type": "message", "output_index": 1, "item_id": "msg_shared"},
            ]
        )
        state.item_texts.update({0: "first", 1: "second"})

        events = state.build_final_events("stop")
        done_indexes = [event["output_index"] for event in events if event["type"] == "response.output_item.done"]

        assert done_indexes == [1]


class TestEmitTextDone:
    def test_closes_active_text_output_with_done_sequence(self):
        state = _state(
            response_id="resp_a",
            message_id="msg_a",
            active_text_item_id="msg_a",
            active_text_output_index=2,
            content_part_added_sent=True,
        )
        state.item_texts[2] = "hi"
        events = state.emit_text_done()
        assert [e["type"] for e in events] == [
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
        ]
        assert events[0]["item_id"] == "msg_a"
        assert events[0]["output_index"] == 2
        assert events[0]["text"] == "hi"

    def test_resets_active_text_state_to_prevent_duplicate_done(self):
        state = _state(active_text_item_id="msg_a", active_text_output_index=0, content_part_added_sent=True)
        state.item_texts[0] = "hi"
        state.emit_text_done()
        assert state.active_text_item_id is None
        assert state.active_text_output_index is None
        assert state.content_part_added_sent is False
        # 幂等：复位后再调用无产出（finalize 不会重复发相同 done）
        assert state.emit_text_done() == []

    def test_no_active_text_item_is_noop(self):
        state = ResponseStreamState()
        assert state.emit_text_done() == []

    def test_missing_output_index_falls_back_to_zero(self):
        state = _state(active_text_item_id="msg_a")
        events = state.emit_text_done()
        assert events[0]["output_index"] == 0


class TestNextSeq:
    def test_sequence_number_monotonic(self):
        state = ResponseStreamState()
        assert [state.next_seq() for _ in range(3)] == [1, 2, 3]
