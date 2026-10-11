import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;
import '../models/message.dart';
import '../main.dart';
import 'message_bubble.dart';

/// Check if pretext bridge has markdown rendering
@JS('pretextBridge.renderMarkdownToCanvas')
external bool _renderMarkdown(String text, String canvasId, double maxWidth, JSObject? options);

@JS('pretextBridge.getMarkdownHeight')
external double _getMarkdownHeight(String text, double maxWidth);

@JS('pretextBridge.isReady')
external bool _isReady();

/// Canvas-rendered message bubble for Klukai's responses with markdown support.
///
/// The bridge is best-effort: if it is missing, throws, or cannot find its
/// canvas, the bubble degrades to a plain [MessageBubble] rather than leaving
/// a blank panel where her reply should be. Images never reach this widget
/// (ChatScreen routes them to MessageBubble, which has the download control).
class CanvasMessageBubble extends StatefulWidget {
  final ChatMessage message;

  const CanvasMessageBubble({super.key, required this.message});

  @override
  State<CanvasMessageBubble> createState() => _CanvasMessageBubbleState();
}

class _CanvasMessageBubbleState extends State<CanvasMessageBubble> {
  /// The platform-view slot is attached to the DOM a frame after the first
  /// build, so the first render attempt can legitimately miss the canvas.
  /// Retry a few times before giving the bubble up to MessageBubble.
  static const int _maxRenderAttempts = 3;
  static const Duration _retryDelay = Duration(milliseconds: 150);

  String? _canvasId;
  double _height = 60;
  bool _rendered = false;
  bool _fallback = false;
  int _renderAttempts = 0;

  @override
  void initState() {
    super.initState();
    _canvasId = 'klukai-canvas-${widget.message.id}';
  }

  @override
  Widget build(BuildContext context) {
    if (_fallback) return MessageBubble(message: widget.message);

    final isMobile = MediaQuery.of(context).size.width < 600;
    final maxBubbleWidth = MediaQuery.of(context).size.width * (isMobile ? 0.85 : 0.75);
    final contentWidth = maxBubbleWidth - 28; // padding + border

    // Try to get markdown height
    if (!_rendered) {
      try {
        if (!_isReady()) {
          // The bridge went away between ChatScreen's check and this build —
          // never leave an empty canvas standing in for her words.
          _fallback = true;
          return MessageBubble(message: widget.message);
        }
        _height = _getMarkdownHeight(widget.message.content, contentWidth);
        // Schedule canvas rendering after build
        WidgetsBinding.instance.addPostFrameCallback((_) => _renderCanvas(contentWidth));
      } catch (e) {
        debugPrint('Canvas measure failed: $e');
        _fallback = true;
        return MessageBubble(message: widget.message);
      }
    }

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Flexible(
            child: Container(
              constraints: BoxConstraints(maxWidth: maxBubbleWidth),
              decoration: BoxDecoration(
                color: GFL2Colors.surface,
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(4),
                  topRight: Radius.circular(4),
                  bottomRight: Radius.circular(4),
                ),
                border: const Border(
                  left: BorderSide(color: GFL2Colors.primary, width: 2),
                ),
              ),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Comm tag
                    Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(
                        'KLUKAI // SST-05',
                        style: TextStyle(
                          color: GFL2Colors.primary.withValues(alpha: 0.45),
                          fontSize: 9,
                          fontWeight: FontWeight.w700,
                          letterSpacing: 1.5,
                          fontFamily: 'monospace',
                        ),
                      ),
                    ),
                    // Canvas for markdown rendering
                    SizedBox(
                      width: contentWidth,
                      height: _height,
                      child: HtmlElementView.fromTagName(
                        tagName: 'canvas',
                        onElementCreated: (element) {
                          final canvas = element as web.HTMLCanvasElement;
                          canvas.id = _canvasId!;
                          canvas.style.width = '${contentWidth}px';
                          canvas.style.height = '${_height}px';
                          // Render after element is in DOM
                          Future.delayed(const Duration(milliseconds: 100), () {
                            _renderCanvas(contentWidth);
                          });
                        },
                      ),
                    ),
                    // Timestamp
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Align(
                        alignment: Alignment.centerRight,
                        child: Text(
                          _formatTime(widget.message.createdAt),
                          style: TextStyle(
                            color: GFL2Colors.textDim.withValues(alpha: 0.4),
                            fontSize: 9,
                            fontFamily: 'monospace',
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _renderCanvas(double maxWidth) {
    if (_canvasId == null || _rendered || _fallback || !mounted) return;
    bool ok;
    try {
      ok = _renderMarkdown(widget.message.content, _canvasId!, maxWidth, null);
    } catch (e) {
      debugPrint('Canvas render failed: $e');
      _bailToPlainBubble();
      return;
    }
    if (ok) {
      _rendered = true;
      _syncHeightToCanvas();
      return;
    }
    // `false` means the bridge could not find the canvas (or has no text
    // engine). The slot may simply not be in the DOM yet — retry briefly.
    if (++_renderAttempts >= _maxRenderAttempts) {
      debugPrint('Canvas render gave up after $_renderAttempts attempts');
      _bailToPlainBubble();
      return;
    }
    Future.delayed(_retryDelay, () => _renderCanvas(maxWidth));
  }

  /// Swap the whole bubble for MessageBubble. Her reply is still readable —
  /// just without the markdown pass.
  void _bailToPlainBubble() {
    if (!mounted || _fallback) return;
    setState(() => _fallback = true);
  }

  /// The bridge sizes the canvas to the real laid-out height, which can differ
  /// from the quick pre-render estimate. Grow/shrink the Flutter box to match
  /// so the last lines are never clipped (or left floating over blank space).
  void _syncHeightToCanvas() {
    try {
      final el = web.document.getElementById(_canvasId!);
      if (el == null || !el.isA<web.HTMLCanvasElement>()) return;
      final canvas = el as web.HTMLCanvasElement;
      final dpr = web.window.devicePixelRatio;
      if (dpr <= 0 || canvas.height <= 0) return;
      final realHeight = canvas.height / dpr;
      if ((realHeight - _height).abs() < 0.5 || !mounted) return;
      setState(() => _height = realHeight);
    } catch (e) {
      debugPrint('Canvas height sync skipped: $e');
    }
  }

  String _formatTime(DateTime dt) {
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}
