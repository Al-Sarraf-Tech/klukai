import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const web = new URL('../../../flutter_app/web/js/', import.meta.url);

function loadBridge() {
  const drawn = [];
  const context = {
    scale() {}, fillRect() {},
    fillText(text) { drawn.push(text); },
    measureText(text) { return { width: text.length * 7 }; },
  };
  const canvas = { style: {}, getContext: () => context };
  const sandbox = vm.createContext({
    console: { log() {} },
    window: { devicePixelRatio: 2 },
    document: {
      getElementById: id => id === 'message' ? canvas : null,
      createElement: () => ({ getContext: () => context }),
    },
  });
  vm.runInContext(fs.readFileSync(new URL('marked.min.js', web), 'utf8'), sandbox);
  sandbox.window.marked = sandbox.marked;
  vm.runInContext(fs.readFileSync(new URL('pretext_bridge.js', web), 'utf8'), sandbox);
  return { bridge: sandbox.window.pretextBridge, canvas, drawn, sandbox };
}

test('the browser bridge loads and exports both measurement and markdown methods', () => {
  const { bridge } = loadBridge();
  assert.equal(bridge.isReady(), false);
  assert.equal(bridge.prepare('Hello', '15px sans-serif'), -1);
  assert.equal(typeof bridge.renderToCanvas, 'function');
  assert.equal(typeof bridge.renderMarkdownToCanvas, 'function');
  assert.equal(bridge.renderMarkdownToCanvas('Hello', 'missing', 200, {}), false);
});

test('real markdown tokens render heading, paragraph and code to the canvas', () => {
  const { bridge, canvas, drawn } = loadBridge();
  const markdown = '# Briefing\n\nReady, Commander.\n\n```\nstatus = ready\n```';
  assert.equal(bridge.renderMarkdownToCanvas(markdown, 'message', 240, {}), true);
  assert.deepEqual(drawn, ['Briefing', 'Ready, Commander.', 'status = ready']);
  assert.equal(canvas.width, 480);
  assert.ok(canvas.height > 100);
  assert.ok(bridge.getMarkdownHeight(markdown, 240) > 21);
});

// The new boundary must return tokens, never painted/truncated text.
test('structured markdown preserves quotes tables and ordered list sequence', () => {
  const { bridge } = loadBridge();
  const tokens = JSON.parse(bridge.parseMarkdown('> Hold position.\n\n3. Three\n4. Four\n\n| Name | Status |\n| --- | --- |\n| Klukai | Ready |'));
  assert.equal(tokens[0].type, 'blockquote');
  const list = tokens.find(t => t.type === 'list');
  assert.equal(list.start, 3);
  assert.deepEqual(list.items.map(t => t.text), ['Three', 'Four']);
  assert.equal(tokens.find(t => t.type === 'table').rows[0][0].text, 'Klukai');
});

test('structured parser preserves inert HTML and image source without DOM work', () => {
  const { bridge, drawn } = loadBridge();
  const tokens = JSON.parse(bridge.parseMarkdown('<script>alert(1)</script>\n\n![edges](https://example.invalid/image.png)'));
  assert.equal(tokens[0].type, 'html');
  assert.equal(tokens.find(t => t.type === 'paragraph').tokens[0].type, 'image');
  assert.deepEqual(drawn, []);
});

test('structured parser failure or absence requests source fallback', () => {
  const { bridge, sandbox } = loadBridge();
  sandbox.window.marked = undefined;
  assert.equal(bridge.parseMarkdown('Complete source'), null);
  sandbox.window.marked = {lexer() { throw new Error('broken parser'); }};
  assert.equal(bridge.parseMarkdown('Complete source'), null);
});
