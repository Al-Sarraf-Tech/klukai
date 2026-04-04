import 'dart:js_interop';

/// JS bridge bindings for pretext text measurement.
@JS('pretextBridge.isReady')
external bool _isReady();

@JS('pretextBridge.prepare')
external int _prepare(String text, String font);

@JS('pretextBridge.findTightWidth')
external double _findTightWidth(int handle, double maxWidth, double lineHeight);

@JS('pretextBridge.getHeight')
external double _getHeight(int handle, double maxWidth, double lineHeight);

@JS('pretextBridge.renderToCanvas')
external bool _renderToCanvas(
    int handle, String canvasId, double maxWidth, double lineHeight, String color);

@JS('pretextBridge.release')
external void _release(int handle);

/// High-level pretext service for Flutter.
class PretextService {
  // Must match the CSS font used in canvas rendering
  static const String font = '15px Inter, -apple-system, BlinkMacSystemFont, sans-serif';
  static const double lineHeight = 21.0;
  static const double bubblePaddingH = 16.0;
  static const double bubblePaddingV = 10.0;

  /// Whether the pretext JS bridge is loaded and ready.
  static bool get isReady {
    try {
      return _isReady();
    } catch (_) {
      return false;
    }
  }

  /// Prepare text for measurement. Returns an opaque handle (-1 if not ready).
  static int prepare(String text) {
    if (!isReady) return -1;
    return _prepare(text, font);
  }

  /// Compute the tightest bubble width for the given text.
  /// Returns total width including padding.
  static double tightBubbleWidth(int handle, double maxContentWidth) {
    if (handle < 0) return maxContentWidth + bubblePaddingH * 2;
    final tightContent = _findTightWidth(handle, maxContentWidth, lineHeight);
    return tightContent + bubblePaddingH * 2;
  }

  /// Get the rendered height at a given content width.
  /// Returns total height including padding.
  static double bubbleHeight(int handle, double contentWidth) {
    if (handle < 0) return 40;
    final h = _getHeight(handle, contentWidth, lineHeight);
    return h + bubblePaddingV * 2;
  }

  /// Render text to a canvas element.
  static bool renderToCanvas(int handle, String canvasId, double maxWidth, String color) {
    if (handle < 0) return false;
    return _renderToCanvas(handle, canvasId, maxWidth, lineHeight, color);
  }

  /// Release a prepared handle.
  static void release(int handle) {
    if (handle >= 0) _release(handle);
  }
}
