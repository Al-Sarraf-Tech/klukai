/**
 * Pretext bridge for Flutter Web — handle-based API for Dart interop.
 * Loaded after pretext ES modules are available on window.__pretext.
 */
(function () {
  'use strict';

  let nextHandle = 1;
  const handles = new Map();

  function getPretext() {
    return window.__pretext;
  }

  window.pretextBridge = {
    /**
     * Check if pretext is loaded and ready.
     */
    isReady() {
      return !!window.__pretext;
    },

    /**
     * Prepare text for measurement. Returns an opaque handle.
     */
    prepare(text, font) {
      const pt = getPretext();
      if (!pt) return -1;
      const prepared = pt.prepareWithSegments(text, font);
      const h = nextHandle++;
      handles.set(h, { prepared, font });
      return h;
    },

    /**
     * Get layout info at a given width.
     * Returns: { lineCount, height }
     */
    layout(handle, maxWidth, lineHeight) {
      const entry = handles.get(handle);
      if (!entry) return null;
      return getPretext().layout(entry.prepared, maxWidth, lineHeight);
    },

    /**
     * Find the tightest bubble width using binary search.
     * Returns the pixel width of the widest line at the tightest wrapping.
     */
    findTightWidth(handle, maxWidth, lineHeight) {
      const entry = handles.get(handle);
      if (!entry) return maxWidth;
      const pt = getPretext();
      const initial = pt.layout(entry.prepared, maxWidth, lineHeight);

      if (initial.lineCount <= 1) {
        // Single line — find the actual width
        let w = 0;
        pt.walkLineRanges(entry.prepared, maxWidth, (line) => {
          if (line.width > w) w = line.width;
        });
        return Math.ceil(w);
      }

      // Binary search for narrowest width that doesn't increase line count
      let lo = 1;
      let hi = Math.ceil(maxWidth);
      while (lo < hi) {
        const mid = Math.floor((lo + hi) / 2);
        const test = pt.layout(entry.prepared, mid, lineHeight);
        if (test.lineCount <= initial.lineCount) {
          hi = mid;
        } else {
          lo = mid + 1;
        }
      }

      // Get actual max line width at the tight width
      let maxLineWidth = 0;
      pt.walkLineRanges(entry.prepared, lo, (line) => {
        if (line.width > maxLineWidth) maxLineWidth = line.width;
      });
      return Math.ceil(maxLineWidth);
    },

    /**
     * Render text to a canvas element by ID.
     */
    renderToCanvas(handle, canvasId, maxWidth, lineHeight, color) {
      const entry = handles.get(handle);
      if (!entry) return false;
      const pt = getPretext();
      const result = pt.layoutWithLines(entry.prepared, maxWidth, lineHeight);
      const canvas = document.getElementById(canvasId);
      if (!canvas) return false;

      const dpr = window.devicePixelRatio || 1;
      const cssW = Math.ceil(maxWidth);
      const cssH = Math.ceil(result.height);
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      canvas.style.width = cssW + 'px';
      canvas.style.height = cssH + 'px';

      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.font = entry.font;
      ctx.fillStyle = color || 'rgba(255,255,255,0.92)';
      ctx.textBaseline = 'top';

      const ascent = lineHeight * 0.8; // approximate baseline offset
      for (let i = 0; i < result.lines.length; i++) {
        const line = result.lines[i];
        ctx.fillText(line.text, 0, i * lineHeight + (lineHeight - ascent) / 2);
      }
      return true;
    },

    /**
     * Get layout height for a given width (convenience for sizing).
     */
    getHeight(handle, maxWidth, lineHeight) {
      const entry = handles.get(handle);
      if (!entry) return 0;
      return getPretext().layout(entry.prepared, maxWidth, lineHeight).height;
    },

    /**
     * Release a prepared handle.
     */
    release(handle) {
      handles.delete(handle);
    },

    /**
     * Release all handles.
     */
    releaseAll() {
      handles.clear();
      nextHandle = 1;
    },
  };

    // ── Markdown rendering ──────────────────────────────────────────

    /**
     * Render markdown text to a canvas element.
     * Parses with marked, renders with styled canvas 2D.
     */
    renderMarkdownToCanvas(text, canvasId, maxWidth, options) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return false;

      const marked = window.marked;
      if (!marked) {
        // Fallback: render plain text
        return this.renderToCanvas(
          this.prepare(text, options?.font || '15px Inter, sans-serif'),
          canvasId, maxWidth, 21, options?.color || 'rgba(255,255,255,0.92)'
        );
      }

      const dpr = window.devicePixelRatio || 1;
      const font = options?.font || '15px Inter, sans-serif';
      const boldFont = options?.boldFont || 'bold 15px Inter, sans-serif';
      const codeFont = options?.codeFont || '13px monospace';
      const color = options?.color || 'rgba(255,255,255,0.92)';
      const dimColor = options?.dimColor || 'rgba(255,255,255,0.5)';
      const codeColor = options?.codeColor || 'rgba(79,195,247,0.9)';
      const codeBg = options?.codeBg || 'rgba(37,43,59,0.8)';
      const lineHeight = 21;
      const codePadding = 6;

      // Parse markdown tokens
      const tokens = marked.lexer(text);

      // Measure total height
      let totalHeight = 0;
      const blocks = [];

      for (const token of tokens) {
        if (token.type === 'paragraph' || token.type === 'text') {
          const rawText = token.raw?.replace(/\n/g, ' ').trim() || '';
          const lines = this._wrapText(rawText, font, maxWidth);
          blocks.push({ type: 'text', lines, font, color });
          totalHeight += lines.length * lineHeight + 4;
        } else if (token.type === 'heading') {
          const hFont = `bold ${18 - token.depth}px Inter, sans-serif`;
          const lines = this._wrapText(token.text, hFont, maxWidth);
          blocks.push({ type: 'heading', lines, font: hFont, color });
          totalHeight += lines.length * (lineHeight + 2) + 8;
        } else if (token.type === 'code') {
          const lines = token.text.split('\n');
          blocks.push({ type: 'code', lines, font: codeFont, color: codeColor });
          totalHeight += lines.length * lineHeight + codePadding * 2 + 8;
        } else if (token.type === 'list') {
          for (const item of (token.items || [])) {
            const prefix = token.ordered ? `${item.index || '1'}. ` : '- ';
            const lines = this._wrapText(prefix + (item.text || ''), font, maxWidth - 16);
            blocks.push({ type: 'list', lines, font, color });
            totalHeight += lines.length * lineHeight + 2;
          }
        } else if (token.type === 'space') {
          totalHeight += 8;
          blocks.push({ type: 'space' });
        }
      }

      totalHeight = Math.max(totalHeight, lineHeight);

      // Size canvas
      const cssW = Math.ceil(maxWidth);
      const cssH = Math.ceil(totalHeight);
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      canvas.style.width = cssW + 'px';
      canvas.style.height = cssH + 'px';

      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.textBaseline = 'top';

      let y = 0;
      for (const block of blocks) {
        if (block.type === 'space') {
          y += 8;
          continue;
        }
        if (block.type === 'code') {
          // Draw code background
          const blockH = block.lines.length * lineHeight + codePadding * 2;
          ctx.fillStyle = codeBg;
          ctx.fillRect(0, y, cssW, blockH);
          ctx.fillStyle = block.color;
          ctx.font = block.font;
          for (let i = 0; i < block.lines.length; i++) {
            ctx.fillText(block.lines[i], codePadding, y + codePadding + i * lineHeight);
          }
          y += blockH + 8;
          continue;
        }
        ctx.font = block.font;
        ctx.fillStyle = block.color;
        for (let i = 0; i < block.lines.length; i++) {
          const x = block.type === 'list' ? 12 : 0;
          ctx.fillText(block.lines[i], x, y + i * lineHeight);
        }
        y += block.lines.length * lineHeight + (block.type === 'heading' ? 8 : 4);
      }

      return true;
    },

    /**
     * Get the rendered height for markdown content.
     */
    getMarkdownHeight(text, maxWidth) {
      // Quick estimate without full rendering
      const marked = window.marked;
      if (!marked) return text.split('\n').length * 21;

      const tokens = marked.lexer(text);
      let h = 0;
      for (const token of tokens) {
        if (token.type === 'code') {
          h += token.text.split('\n').length * 21 + 20;
        } else if (token.type === 'space') {
          h += 8;
        } else if (token.type === 'heading') {
          h += 30;
        } else {
          const lines = Math.ceil((token.raw?.length || 20) * 8 / maxWidth);
          h += Math.max(1, lines) * 21 + 4;
        }
      }
      return Math.max(h, 21);
    },

    /** Helper: wrap text to fit within maxWidth. */
    _wrapText(text, font, maxWidth) {
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      ctx.font = font;

      const words = text.split(' ');
      const lines = [];
      let current = '';

      for (const word of words) {
        const test = current ? current + ' ' + word : word;
        if (ctx.measureText(test).width > maxWidth && current) {
          lines.push(current);
          current = word;
        } else {
          current = test;
        }
      }
      if (current) lines.push(current);
      return lines.length ? lines : [''];
    },

  };

  console.log('[pretext_bridge] Ready');
})();
