import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;
import '../models/message.dart';
import '../main.dart';

/// Check if pretext bridge has markdown rendering
@JS('pretextBridge.renderMarkdownToCanvas')
external bool _renderMarkdown(String text, String canvasId, double maxWidth, JSObject? options);

@JS('pretextBridge.getMarkdownHeight')
external double _getMarkdownHeight(String text, double maxWidth);

@JS('pretextBridge.isReady')
external bool _isReady();

/// Canvas-rendered message bubble for Klukai's responses with markdown support.
class CanvasMessageBubble extends StatefulWidget {
  final ChatMessage message;

  const CanvasMessageBubble({super.key, required this.message});

  @override
  State<CanvasMessageBubble> createState() => _CanvasMessageBubbleState();
}

class _CanvasMessageBubbleState extends State<CanvasMessageBubble> {
  String? _canvasId;
  double _height = 60;
  bool _rendered = false;

  @override
  void initState() {
    super.initState();
    _canvasId = 'klukai-canvas-${widget.message.id}';
  }

  @override
  Widget build(BuildContext context) {
    final isMobile = MediaQuery.of(context).size.width < 600;
    final maxBubbleWidth = MediaQuery.of(context).size.width * (isMobile ? 0.85 : 0.75);
    final contentWidth = maxBubbleWidth - 28; // padding + border

    // Try to get markdown height
    if (!_rendered) {
      try {
        if (_isReady()) {
          _height = _getMarkdownHeight(widget.message.content, contentWidth);
          // Schedule canvas rendering after build
          WidgetsBinding.instance.addPostFrameCallback((_) => _renderCanvas(contentWidth));
        }
      } catch (_) {
        // Bridge not ready, use fallback height
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
    if (_canvasId == null) return;
    try {
      _renderMarkdown(widget.message.content, _canvasId!, maxWidth, null);
      _rendered = true;
    } catch (e) {
      debugPrint('Canvas render failed: $e');
    }
  }

  String _formatTime(DateTime dt) {
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}
