# Readable conversation and complete artwork

## Intent

The conversation must stay beautiful and usable when a reply finishes, when the window changes size, and when artwork is landscape. Preserve the existing cyan/navy/orange identity and approved character assets. Fix content and interaction loss before adding decoration.

## Current evidence

Interface audit UI-01 through UI-05: the finalized canvas path drops blockquotes/tables, misnumbers ordered lists, clips wrapped headings, and loses selection, accessible text and replay. Square cover crops a third of current 3:2 artwork. See /home/jalsarraf/.local/state/codex-tasks/klukai-interface-refactor-audit-20261014.txt and markdown probe JSON beside it.

## Architecture

Use one MessageBubble for streaming and finalized replies, with small answer-content, artwork and footer components. Use native Flutter layout and selectable accessible text; remove the canvas-only answer route. Reuse the checked-in marked parser through a narrow structured-token interop adapter if needed for rich Markdown; do not acquire a new package. Preserve a complete selectable source-text fallback if parsing is absent, throws or encounters unsupported content. Never silently discard a block.

Supported rich content: paragraphs, headings, emphasis, links, ordered/unordered nested lists, quotes, code and tables. Native layout owns sizing and reflow. Long code/tables may scroll horizontally inside the bubble; the page must not overflow. Raw HTML stays inert text. External images in Markdown must not auto-fetch. Only http/https links can open; provide keyboard-accessible link actions. Existing actual source URLs must remain inspectable.

Streaming and final messages share the same controls/semantics contract. A completed assistant reply has voice replay, with current unavailable feedback. Message timestamps and labels use readable semantic colors rather than low-opacity small text. Existing interop text-width estimates must not constrain rich content to obsolete width measurements.

Artwork shows its full composition with contained sizing, supports square/landscape/portrait images, and offers labeled keyboard/touch full-view and download actions. Include loading/error states. Full view fits available bounds and permits explicit zoom; preserve source bytes and choose truthful download MIME/extension. Reuse across chat and completed Her POV where practical; gallery thumbnail cropping remains explicit and separate.

## Acceptance

- Every meaningful text token from a mixed Markdown fixture remains readable/selectable in streaming and final states, including parser-unavailable fallback.
- Finalization preserves replay and accessible text, including when the old pretext bridge says ready.
- Ordered lists retain sequence; nested lists, code, quotes, tables, malformed syntax, long URLs and multilingual text remain visible.
- Malicious href schemes and HTML do not execute or navigate; Markdown image URLs cause no fetch.
- 320/390/1280 widths and 200% text scale reflow without clipped headings, footer overlap or page overflow.
- All four edge markers in square, 3:2, 4:3 and portrait test artwork remain visible in full presentation.
- Voice, download, full-view and link controls have meaningful labels, keyboard activation and visible focus.
- Tests use controlled fixtures and no real model, image, voice or private-memory request.
- Preserve avatar/camera/motion source and canonical Commander reference bytes.

