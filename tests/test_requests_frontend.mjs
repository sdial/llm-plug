/** Request-detail presentation behavior contracts. Run: node --test tests/test_requests_frontend.mjs */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { commonGlobals, evalModule } from './_tools/frontend_harness.mjs';

function loadRequests() {
    return evalModule('static/js/requests.js', {
        globals: commonGlobals({
            window: { location: { pathname: '/admin', href: '', search: '' }, TabRuntime: { register() {} } },
            I18n: { t: (key, values = {}) => `${key}:${JSON.stringify(values)}` },
        }),
        returns: ['renderContextShapingReceipt', 'renderRequestError'],
    });
}

describe('request detail presentation', () => {
    it('keeps Context Shaping compact until the operator expands it', () => {
        const { renderContextShapingReceipt } = loadRequests();
        const html = renderContextShapingReceipt({
            upstream_api_format: 'openai-chat-completions',
            enabled_features: ['strip_ansi'],
            actions: [{ feature: 'strip_ansi', field_path: 'messages[*].content', hit_count: 2, before_chars: 14, after_chars: 4 }],
        });

        assert.match(html, /<details class="mt-3/);
        assert.match(html, /requests\.shapingSummaryChanged/);
        assert.match(html, /requests\.shapingActionStats/);
        assert.match(html, /requests\.shapingViewRaw/);
        assert.match(html, /"field_path": "messages\[\*\]\.content"/);
    });

    it('renders an explicit diagnostic state for every failed request', () => {
        const { renderRequestError } = loadRequests();

        assert.match(renderRequestError({ success: false, error_msg: 'upstream down' }), /upstream down/);
        assert.match(renderRequestError({ success: false, error_msg: null }), /requests\.detailErrorUnavailable/);
        assert.equal(renderRequestError({ success: true, error_msg: 'ignored' }), '');
    });
});
