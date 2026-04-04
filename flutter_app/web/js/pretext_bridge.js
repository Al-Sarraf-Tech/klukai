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

  console.log('[pretext_bridge] Ready');
})();
