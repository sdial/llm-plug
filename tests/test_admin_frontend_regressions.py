from pathlib import Path


STATIC_JS = Path("static/js")
REQUESTS_FRAGMENT = Path("static/fragments/admin/requests.html")


def test_settings_page_contains_lb_strategy_controls():
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")

    assert 'id="set_lb_strategy"' in html
    assert 'id="set_sticky_ttl"' in html
    assert 'id="set_sticky_cache_max_entries"' in html
    assert 'id="sticky_lb_options"' in html
    assert 'value="round_robin"' in html
    assert 'value="backup"' in html
    assert 'value="sticky"' in html


def test_settings_js_loads_saves_and_toggles_lb_strategy_controls():
    js = Path("static/js/settings.js").read_text(encoding="utf-8")

    assert "syncLbStrategyMode" in js
    assert "set_lb_strategy" in js
    assert "set_sticky_ttl" in js
    assert "set_sticky_cache_max_entries" in js
    assert "data.lb_strategy" in js
    assert "data.sticky_ttl" in js
    assert "data.sticky_cache_max_entries" in js


def test_switching_to_requests_does_not_read_request_filters_before_fragment_loads():
    admin_js = (STATIC_JS / "admin.js").read_text(encoding="utf-8")

    assert "function updateRequestHashSafely()" in admin_js
    assert (
        "if (typeof syncRequestHash === 'function' && document.getElementById('reqFilterModel'))"
        in admin_js
    )
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


def test_requests_table_shows_zero_cache_read_tokens_when_field_exists():
    requests_js = (STATIC_JS / "requests.js").read_text(encoding="utf-8")
    admin_css = Path("static/css/admin.css").read_text(encoding="utf-8")

    assert (
        "if (cachedTokens === null) return renderMissingCacheReadToken('null');"
        in requests_js
    )
    assert (
        "if (cachedTokens === undefined) return renderMissingCacheReadToken('undefined');"
        in requests_js
    )
    assert "renderTokenUsage(inputTokens, req.cache_read_input_tokens)" in requests_js
    assert "request-cache-missing" in admin_css
    assert 'content: "cache";' in admin_css


def test_request_analyzer_link_passes_api_type_from_channel_metadata():
    requests_js = (STATIC_JS / "requests.js").read_text(encoding="utf-8")

    assert "function getRequestAnalyzerApiType(req)" in requests_js
    assert "if (req.api_type) return req.api_type;" in requests_js
    assert (
        "api_type=${encodeURIComponent(getRequestAnalyzerApiType(req))}" in requests_js
    )


def test_request_analyzer_normalizes_chat_and_anthropic_contexts():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")
    analyzer_html = Path("static/request-analyzer.html").read_text(encoding="utf-8")

    assert 'data-view="overview"' in analyzer_html
    assert "function normalizeRequest(raw, apiType)" in analyzer_js
    assert "function normalizeChatRequest(raw)" in analyzer_js
    assert "function normalizeAnthropicRequest(raw)" in analyzer_js
    assert "tool_use_id" in analyzer_js
    assert "tool_call_id" in analyzer_js
    assert "renderDiagnosticsView" in analyzer_js


def test_request_analyzer_renders_chat_assistant_tool_calls_as_message_blocks():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "blocks: normalizeChatMessageBlocks(msg)" in analyzer_js
    assert "function normalizeChatToolCallBlock(call)" in analyzer_js
    assert "type: 'tool_use'" in analyzer_js
    assert "id: call.id || ''" in analyzer_js
    assert "input: prettyJsonString(call.function?.arguments || '{}')" in analyzer_js
    assert "!turn.blocks.some(b => b.type === 'tool_use')" in analyzer_js


def test_request_analyzer_preserves_chat_annotations_legacy_function_call_and_audio():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "function normalizeChatAnnotationBlock(annotation)" in analyzer_js
    assert "message.annotations.map(normalizeChatAnnotationBlock)" in analyzer_js
    assert "message.function_call" in analyzer_js
    assert "normalizeChatLegacyFunctionCallBlock" in analyzer_js
    assert "message.audio" in analyzer_js
    assert "normalizeChatAudioBlock(message.audio" in analyzer_js
    assert "function collectChatOutputToolCalls(message, choiceIndex)" in analyzer_js


def test_request_analyzer_preserves_request_params_and_media_details():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "requestParams: normalizeRequestParams(raw)" in analyzer_js
    assert "renderRequestParamsSection(normalizedContext.requestParams)" in analyzer_js
    assert "'temperature'" in analyzer_js
    assert "'reasoning_effort'" in analyzer_js
    assert "'parallel_tool_calls'" in analyzer_js
    assert "block.image_url?.detail" in analyzer_js
    assert "formatAudioInputSummary(block.input_audio)" in analyzer_js
    assert "tool_call_id: message.tool_call_id" in analyzer_js
    assert "renderBlockMeta('tool_call_id', block.tool_call_id)" in analyzer_js


def test_request_analyzer_structures_output_metadata_and_usage_details():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "metadata: normalizeOutputMetadata(raw)" in analyzer_js
    assert "renderOutputMetadata(output.metadata)" in analyzer_js
    assert "'system_fingerprint'" in analyzer_js
    assert "'service_tier'" in analyzer_js
    assert "renderUsageDetails(output.usage)" in analyzer_js
    assert "prompt_tokens_details?.cached_tokens" in analyzer_js
    assert "completion_tokens_details?.reasoning_tokens" in analyzer_js


def test_request_analyzer_renders_anthropic_tool_use_inputs_without_raw_block_dump():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "input: safeJson(block.input || {})" in analyzer_js
    assert "if (block.type === 'tool_use') {" in analyzer_js
    assert "escapeHtml(block.input || '')" in analyzer_js


def test_request_analyzer_treats_anthropic_redacted_thinking_as_thinking_block():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "block.type === 'redacted_thinking'" in analyzer_js
    assert "type: 'thinking'" in analyzer_js
    assert "text: '[redacted thinking]'" in analyzer_js


def test_request_analyzer_sanitizes_markdown_html():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "function sanitizeHtml(html)" in analyzer_js
    assert "template.content.querySelectorAll" in analyzer_js
    assert "sanitizeHtml(marked.parse(text))" in analyzer_js


def test_request_analyzer_loads_and_normalizes_model_output():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")
    analyzer_html = Path("static/request-analyzer.html").read_text(encoding="utf-8")

    assert 'data-view="output"' in analyzer_html
    assert "模型输出" in analyzer_html
    assert "const data = result.data" in analyzer_js
    assert "fetch(`/admin/requests/${requestId}/response-body`)" in analyzer_js
    assert "function normalizeOutput(raw, reqApiType" in analyzer_js
    assert "function normalizeChatOutput(raw)" in analyzer_js
    assert "function normalizeAnthropicOutput(raw)" in analyzer_js
    assert "function normalizeResponsesOutput(raw)" in analyzer_js
    assert "renderOutputView" in analyzer_js


def test_request_analyzer_renders_responses_terminal_execute_arguments():
    analyzer_js = (STATIC_JS / "request-analyzer.js").read_text(encoding="utf-8")

    assert "function normalizeResponsesToolUseBlock(item)" in analyzer_js
    assert "'terminal_execute'" in analyzer_js
    assert (
        "input: prettyJsonString(item.arguments || item.input || item.function?.arguments || {})"
        in analyzer_js
    )
    assert "blocks: [normalizeResponsesToolUseBlock(item)]" in analyzer_js
    assert "return [normalizeResponsesToolUseBlock(item)];" in analyzer_js


def test_stats_today_merge_uses_configured_aggregation_timezone():
    stats_js = (STATIC_JS / "stats.js").read_text(encoding="utf-8")

    assert "8 * 3600000" not in stats_js
    assert "function getStatsAggregationTimezone()" in stats_js
    assert "function formatStatsDateInTimezone(" in stats_js
    assert (
        "const todayStr = formatStatsDateInTimezone(new Date(), getStatsAggregationTimezone());"
        in stats_js
    )
