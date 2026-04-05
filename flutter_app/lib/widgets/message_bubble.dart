import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;
import '../models/message.dart';
import '../main.dart';

class MessageBubble extends StatefulWidget {
  final ChatMessage message;

  const MessageBubble({super.key, required this.message});

  @override
  State<MessageBubble> createState() => _MessageBubbleState();
}

class _MessageBubbleState extends State<MessageBubble> {
  bool _isPlaying = false;
  bool _isLoading = false;

  Future<void> _playVoice() async {
    if (_isLoading || _isPlaying) return;
    if (widget.message.content.isEmpty) return;

    setState(() => _isLoading = true);

    try {
      // Get server URL from the page origin
      final serverUrl = Uri.base.origin.contains('localhost')
          ? 'http://localhost:8300'
          : Uri.base.origin;

      final response = await http.post(
        Uri.parse('$serverUrl/api/tts'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'text': widget.message.content.length > 500
              ? widget.message.content.substring(0, 500)
              : widget.message.content,
          'language': 'en',
        }),
      );

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final audioB64 = data['audio'] as String?;
        if (audioB64 != null) {
          setState(() {
            _isLoading = false;
            _isPlaying = true;
          });
          final audio = web.HTMLAudioElement()
            ..src = 'data:audio/wav;base64,$audioB64';
          audio.onEnded.listen((_) {
            if (mounted) setState(() => _isPlaying = false);
          });
          audio.play();
          return;
        }
      }
    } catch (e) {
      debugPrint('Voice playback failed: $e');
    }

    if (mounted) setState(() { _isLoading = false; _isPlaying = false; });
  }

  @override
  Widget build(BuildContext context) {
    final isUser = widget.message.role == 'user';
    final isMobile = MediaQuery.of(context).size.width < 600;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      child: Row(
        mainAxisAlignment:
            isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Flexible(
            child: Container(
              constraints: BoxConstraints(
                maxWidth: widget.message.tightBubbleWidth ??
                    MediaQuery.of(context).size.width * (isMobile ? 0.85 : 0.75),
              ),
              decoration: BoxDecoration(
                color: isUser ? GFL2Colors.panel : GFL2Colors.surface,
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(4),
                  topRight: const Radius.circular(4),
                  bottomLeft: Radius.circular(isUser ? 4 : 0),
                  bottomRight: Radius.circular(isUser ? 0 : 4),
                ),
                border: Border(
                  left: isUser
                      ? BorderSide.none
                      : const BorderSide(color: GFL2Colors.primary, width: 2),
                  right: isUser
                      ? const BorderSide(color: GFL2Colors.accent, width: 2)
                      : BorderSide.none,
                ),
              ),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Comm tag for Klukai
                    if (!isUser)
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
                    // Image content
                    if (widget.message.imageData != null)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: ClipRRect(
                          borderRadius: BorderRadius.circular(4),
                          child: Image.memory(
                            base64Decode(widget.message.imageData!),
                            fit: BoxFit.contain,
                            width: double.infinity,
                          ),
                        ),
                      )
                    else
                      SelectableText(
                        widget.message.content,
                        style: TextStyle(
                          color: GFL2Colors.textPrimary.withValues(alpha: 0.92),
                          fontSize: 14,
                          height: 1.5,
                        ),
                      ),
                    if (widget.message.isStreaming)
                      const _BlinkingCursor(),
                    // Bottom row: speaker icon + timestamp
                    if (!widget.message.isStreaming)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            // Speaker icon for Klukai messages
                            if (!isUser)
                              GestureDetector(
                                onTap: _playVoice,
                                child: Padding(
                                  padding: const EdgeInsets.only(right: 8),
                                  child: _isLoading
                                      ? SizedBox(
                                          width: 14,
                                          height: 14,
                                          child: CircularProgressIndicator(
                                            strokeWidth: 1.5,
                                            color: GFL2Colors.primary.withValues(alpha: 0.5),
                                          ),
                                        )
                                      : Icon(
                                          _isPlaying
                                              ? Icons.volume_up
                                              : Icons.volume_up_outlined,
                                          size: 16,
                                          color: _isPlaying
                                              ? GFL2Colors.primary
                                              : GFL2Colors.textDim.withValues(alpha: 0.4),
                                        ),
                                ),
                              ),
                            const Spacer(),
                            Text(
                              _formatTime(widget.message.createdAt),
                              style: TextStyle(
                                color: GFL2Colors.textDim.withValues(alpha: 0.4),
                                fontSize: 9,
                                fontFamily: 'monospace',
                              ),
                            ),
                          ],
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

  String _formatTime(DateTime dt) {
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}

/// Blinking terminal cursor for streaming messages.
class _BlinkingCursor extends StatefulWidget {
  const _BlinkingCursor();

  @override
  State<_BlinkingCursor> createState() => _BlinkingCursorState();
}

class _BlinkingCursorState extends State<_BlinkingCursor>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 600),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        return Opacity(
          opacity: _controller.value,
          child: Container(
            width: 8, height: 16,
            margin: const EdgeInsets.only(top: 2),
            color: GFL2Colors.primary.withValues(alpha: 0.6),
          ),
        );
      },
    );
  }
}
