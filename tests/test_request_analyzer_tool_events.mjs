/**
 * Tests for extractChatToolEvents / extractAnthropicToolEvents
 *
 * These functions are pure (no DOM dependency), so we can extract them from
 * the IIFE in request-analyzer.js and run them directly in Node.js.
 *
 * Run:  node --test tests/test_request_analyzer_tool_events.mjs
 */
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC_PATH = resolve(__dirname, '..', 'static', 'js', 'request-analyzer.js');

/**
 * Load the target functions from the IIFE-wrapped source file.
 * Strategy: wrap the entire IIFE body inside a function that returns the
 * functions we want to test, then eval it in a sandboxed context.
 */
function loadFunctions() {
    const source = readFileSync(SRC_PATH, 'utf-8');

    // The file is an IIFE: (() => { ... })();
    // Strip the outer wrapper to get the inner body
    const bodyMatch = source.match(/^\s*\(\(\)\s*=>\s*\{([\s\S]*)\}\)\(\);?\s*$/);
    if (!bodyMatch) throw new Error('Cannot parse IIFE wrapper');
    // Strip the trailing init() call to avoid DOM side-effects in Node.js
    const body = bodyMatch[1].replace(/\n\s*init\(\);\s*$/, '\n');

    // Build a module that exposes the functions we need
    const wrapped = `
        // Stubs for browser-only globals used by other functions in the IIFE
        const marked = { setOptions() {}, use() {} };
        const hljs = { getLanguage() { return null; }, highlight() { return { value: '' }; }, highlightAuto() { return { value: '' }; } };
        const document = { getElementById() { return null; }, createElement() { return { textContent: '', innerHTML: '' }; }, querySelector() { return null; }, querySelectorAll() { return []; } };
        const window = { location: { search: '' }, addEventListener() {} };
        const history = { pushState() {} };
        const fetch = async () => ({});
        const setTimeout = (fn) => fn();
        const clearTimeout = () => {};

        ${body}

        // Return the functions under test
        return { extractChatToolEvents, extractAnthropicToolEvents, extractResponsesToolEvents };
    `;

    const fn = new Function(wrapped);
    return fn();
}

const { extractChatToolEvents, extractAnthropicToolEvents, extractResponsesToolEvents } = loadFunctions();

// ─── OpenAI Chat Completions ────────────────────────────────────────

describe('extractChatToolEvents', () => {

    it('should count matched tool results (kind=result) in events array', () => {
        const messages = [
            { role: 'user', content: 'What is the weather in Tokyo?' },
            {
                role: 'assistant',
                content: null,
                tool_calls: [{
                    id: 'call_abc123',
                    type: 'function',
                    function: { name: 'get_weather', arguments: '{"city":"Tokyo"}' }
                }]
            },
            { role: 'tool', tool_call_id: 'call_abc123', content: 'Sunny, 25°C' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');
        const calls = events.filter(e => e.kind === 'call');

        assert.equal(calls.length, 1, 'should have 1 tool call');
        assert.equal(results.length, 1, 'should have 1 tool result (BUG: matched results are missing)');
    });

    it('should count multiple matched tool results', () => {
        const messages = [
            { role: 'user', content: 'Check weather and time' },
            {
                role: 'assistant',
                content: null,
                tool_calls: [
                    { id: 'call_1', type: 'function', function: { name: 'get_weather', arguments: '{}' } },
                    { id: 'call_2', type: 'function', function: { name: 'get_time', arguments: '{}' } }
                ]
            },
            { role: 'tool', tool_call_id: 'call_1', content: 'Sunny' },
            { role: 'tool', tool_call_id: 'call_2', content: '14:00' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 2, 'should have 2 tool results for 2 matched calls');
    });

    it('should count multi-turn tool results', () => {
        const messages = [
            { role: 'user', content: 'Step 1' },
            {
                role: 'assistant', content: null,
                tool_calls: [{ id: 'call_a', type: 'function', function: { name: 'step1', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call_a', content: 'result_a' },
            {
                role: 'assistant', content: null,
                tool_calls: [{ id: 'call_b', type: 'function', function: { name: 'step2', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call_b', content: 'result_b' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');
        const calls = events.filter(e => e.kind === 'call');

        assert.equal(calls.length, 2, 'should have 2 tool calls');
        assert.equal(results.length, 2, 'should have 2 tool results');
    });

    it('should still include unmatched results', () => {
        const messages = [
            { role: 'tool', tool_call_id: 'orphan_result', content: 'no matching call' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 1, 'unmatched result should still appear');
        assert.equal(results[0].matched, false, 'unmatched result should have matched=false');
    });

    it('should mark matched results with matched=true', () => {
        const messages = [
            {
                role: 'assistant', content: null,
                tool_calls: [{ id: 'call_x', type: 'function', function: { name: 'test', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call_x', content: 'result' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 1);
        assert.equal(results[0].matched, true, 'matched result should have matched=true');
    });

    it('should return 0 results when there are no tool messages', () => {
        const messages = [
            { role: 'user', content: 'Hello' },
            { role: 'assistant', content: 'Hi!' }
        ];

        const events = extractChatToolEvents(messages);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 0);
    });

    it('call events should still embed matched result data', () => {
        const messages = [
            {
                role: 'assistant', content: null,
                tool_calls: [{ id: 'call_z', type: 'function', function: { name: 'test', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call_z', content: 'the answer is 42' }
        ];

        const events = extractChatToolEvents(messages);
        const call = events.find(e => e.kind === 'call');

        assert.equal(call.matched, true);
        assert.equal(call.result, 'the answer is 42', 'call event should embed the result text');
    });
});

// ─── Anthropic Messages ─────────────────────────────────────────────

describe('extractAnthropicToolEvents', () => {

    it('should count matched tool_result blocks', () => {
        const turns = [
            {
                index: 0, role: 'user',
                blocks: [{ type: 'text', text: 'What is the weather?' }]
            },
            {
                index: 1, role: 'assistant',
                blocks: [{
                    type: 'tool_use', id: 'toolu_abc', name: 'get_weather',
                    input: '{"city":"Tokyo"}', raw: {}
                }]
            },
            {
                index: 2, role: 'user',
                blocks: [{
                    type: 'tool_result', tool_use_id: 'toolu_abc',
                    text: 'Sunny, 25°C', raw: {}
                }]
            }
        ];

        const events = extractAnthropicToolEvents(turns);
        const results = events.filter(e => e.kind === 'result');
        const calls = events.filter(e => e.kind === 'call');

        assert.equal(calls.length, 1, 'should have 1 tool call');
        assert.equal(results.length, 1, 'should have 1 tool result (BUG: matched results are missing)');
    });

    it('should mark matched results with matched=true', () => {
        const turns = [
            {
                index: 0, role: 'assistant',
                blocks: [{ type: 'tool_use', id: 'toolu_1', name: 'test', input: '{}', raw: {} }]
            },
            {
                index: 1, role: 'user',
                blocks: [{ type: 'tool_result', tool_use_id: 'toolu_1', text: 'ok', raw: {} }]
            }
        ];

        const events = extractAnthropicToolEvents(turns);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 1);
        assert.equal(results[0].matched, true, 'matched result should have matched=true');
    });

    it('should still include unmatched results with matched=false', () => {
        const turns = [
            {
                index: 0, role: 'user',
                blocks: [{ type: 'tool_result', tool_use_id: 'orphan', text: 'no call', raw: {} }]
            }
        ];

        const events = extractAnthropicToolEvents(turns);
        const results = events.filter(e => e.kind === 'result');

        assert.equal(results.length, 1);
        assert.equal(results[0].matched, false);
    });
});
