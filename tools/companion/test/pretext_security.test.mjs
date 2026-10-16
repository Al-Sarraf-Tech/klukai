import test from 'node:test';
import assert from 'node:assert/strict';
import { Worker } from 'node:worker_threads';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { analyzeText } from '../../../flutter_app/web/js/pretext/analysis.js';

const profile = {
  lineFitEpsilon: 0.005, carryCJKAfterClosingQuote: false,
  preferPrefixWidthsForBreakableRuns: false, preferEarlySoftHyphenBreak: false,
};

test('the upstream security backport and mixed component inventory remain pinned', () => {
  const root = new URL('../../../flutter_app/web/js/pretext/', import.meta.url);
  const provenance = JSON.parse(fs.readFileSync(new URL('PROVENANCE.json', root), 'utf8'));
  const digest = (name) => createHash('sha256').update(fs.readFileSync(new URL(name, root))).digest('hex');
  // Independently verified official 0.0.5 dist/analysis.js, from the archive's
  // registry-published SHA-512 integrity recorded in PROVENANCE.json.
  assert.equal(digest('analysis.js'), '12e6894f6463188fd5c548dcd44e6174f50eb677065504f1cfdde6652269c31f');
  assert.equal(provenance.backport.release, '0.0.5');
  assert.equal(provenance.vulnerability.id, 'GHSA-5478-66c3-rhxr');
  assert.equal(provenance.vulnerability.status, 'fixed_by_source_backport');
  for (const [name, entry] of Object.entries(provenance.files)) assert.equal(digest(name), entry.sha256, name);
  assert.equal(digest('LICENSE'), 'e9355cb16457e81acd97dac2e50f2f8bbf2a9a464025f9c46db3680cf9598846');
  assert.equal(provenance.files['line-break.js'].upstream_version, '0.0.4', 'do not relabel inherited files');
});

// Recorded from the previous shipped analysis module under this same Node/ICU
// profile. Literal expected text/cursors are independent of the replacement.
const cases = [
  ['', '', [], [], []],
  ['A   quiet\twatch', 'A quiet watch', ['A', ' ', 'quiet', ' ', 'watch'], ['text', 'space', 'text', 'space', 'text'], [0, 1, 2, 7, 8]],
  ['Ready, Commander.', 'Ready, Commander.', ['Ready,', ' ', 'Commander.'], ['text', 'space', 'text'], [0, 6, 7]],
  ['待機。次へ', '待機。次へ', ['待機。', '次', 'へ'], ['text', 'text', 'text'], [0, 3, 4]],
  ['مرحبا بالعالم', 'مرحبا بالعالم', ['مرحبا', ' ', 'بالعالم'], ['text', 'space', 'text'], [0, 5, 6]],
  ['soft\u00adhyphen', 'soft\u00adhyphen', ['soft', '\u00ad', 'hyphen'], ['text', 'soft-hyphen', 'text'], [0, 4, 5]],
  ['go\nnow', 'go now', ['go', ' ', 'now'], ['text', 'space', 'text'], [0, 2, 3]],
];

test('the patched analysis retains normalized text, segmentation and cursors', () => {
  for (const [input, normalized, texts, kinds, starts] of cases) {
    const actual = analyzeText(input, profile);
    assert.deepEqual({ normalized: actual.normalized, texts: actual.texts, kinds: actual.kinds, starts: actual.starts },
      { normalized, texts, kinds, starts }, JSON.stringify(input));
  }
});

test('hostile repeated punctuation has bounded character visits without truncation', { timeout: 6000 }, async () => {
  const source = new URL('../../../flutter_app/web/js/pretext/analysis.js', import.meta.url).href;
  const worker = new Worker(`
    const { parentPort, workerData } = require('node:worker_threads');
    (async () => {
      const { analyzeText } = await import(workerData.source);
      const original = String.prototype[Symbol.iterator];
      const results = [];
      try {
        for (const character of ['(', '!', '#', '[']) {
          const input = character.repeat(12000);
          let visits = 0;
          String.prototype[Symbol.iterator] = function* () {
            const iterator = original.call(this);
            for (let step = iterator.next(); !step.done; step = iterator.next()) {
              if (++visits > 32 * input.length) throw new Error('text analysis exceeded its linear character-visit budget');
              yield step.value;
            }
          };
          const result = analyzeText(input, workerData.profile);
          String.prototype[Symbol.iterator] = original;
          if (result.normalized !== input || result.texts.join('') !== input) throw new Error('analysis truncated or changed the input');
          results.push({ character, visits });
        }
        parentPort.postMessage({ ok: true, results });
      } finally { String.prototype[Symbol.iterator] = original; }
    })().catch((error) => parentPort.postMessage({ ok: false, error: error.message }));
  `, { eval: true, workerData: { source, profile } });
  let timer;
  try {
    const result = await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('malicious-input worker exceeded 3 seconds')), 3000);
      worker.once('message', resolve); worker.once('error', reject);
    });
    assert.equal(result.ok, true, result.error);
    assert.equal(result.results.length, 4);
  } finally { clearTimeout(timer); await worker.terminate(); }
});
