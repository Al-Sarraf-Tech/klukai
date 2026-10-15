import 'dart:async';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../models/message.dart';
import '../platform/browser.dart';
import '../services/session_auth.dart';
import '../theme/gfl2_colors.dart';
import '../services/tts_request.dart';
import 'artwork_view.dart';
import 'message_content.dart';
import 'message_footer.dart';

class MessageBubble extends StatefulWidget {
  final ChatMessage message;

  /// Voice playback seam; defaults to the real browser.
  final BrowserPlatform? browser;

  /// HTTP seam for the TTS request; defaults to a real client. Tests inject
  /// a MockClient.
  final http.Client? client;

  const MessageBubble({
    super.key,
    required this.message,
    this.browser,
    this.client,
  });

  @override
  State<MessageBubble> createState() => _MessageBubbleState();
}

class _MessageBubbleState extends State<MessageBubble> {
  bool _isPlaying = false;
  bool _isLoading = false;

  TtsAudioRequest? _activeTts;

  @override
  void dispose() {
    unawaited(_activeTts?.cancel());
    super.dispose();
  }

  Future<void> _playVoice() async {
    if (_isLoading || _isPlaying) return;
    if (widget.message.content.isEmpty) return;

    setState(() => _isLoading = true);

    var spoke = false;
    // An injected client is the caller's to close; one we open here is ours.
    final client = widget.client ?? http.Client();
    try {
      // Get server URL from the page origin
      final serverUrl = Uri.base.origin.contains('localhost')
          ? 'http://localhost:8300'
          : Uri.base.origin;

      for (final chunk in chunkForTts(widget.message.content)) {
        if (!mounted) break;
        setState(() {
          _isLoading = true;
          _isPlaying = false;
        });
        final request = TtsAudioRequest(
          client: client,
          baseUrl: serverUrl,
          token: readAuthToken(),
        );
        _activeTts = request;
        final audioB64 = await request.run(chunk);
        _activeTts = null;
        if (!mounted) break;
        spoke = true;
        setState(() {
          _isLoading = false;
          _isPlaying = true;
        });
        // Reuse the gesture-unlocked channel and await each clip so every
        // sentence plays in order, including replies longer than 500 chars.
        final played = await (widget.browser ?? defaultBrowserPlatform())
            .playVoice('data:audio/wav;base64,$audioB64');
        if (!played) throw StateError('Browser voice playback unavailable');
      }
    } catch (e) {
      spoke = false;
      debugPrint('Voice playback failed: $e');
    } finally {
      _activeTts = null;
      if (widget.client == null) client.close();
    }

    // Any failure (502 from a powered-off GPU host, timeout, transport error)
    // must drop the spinner and say so — the text still stands on its own.
    if (mounted) {
      setState(() {
        _isLoading = false;
        _isPlaying = false;
      });
    }
    if (!spoke) _showVoiceUnavailable();
  }

  void _showVoiceUnavailable() {
    if (!mounted) return;
    final messenger = ScaffoldMessenger.maybeOf(context);
    if (messenger == null) return;
    messenger.showSnackBar(
      SnackBar(
        content: const Text(
          '// VOICE SYNTH OFFLINE — her words stand.',
          style: TextStyle(
            fontFamily: 'monospace',
            letterSpacing: 1.0,
            fontSize: 12,
          ),
        ),
        backgroundColor: GFL2Colors.surface,
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 4),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(4),
          side: BorderSide(color: GFL2Colors.danger.withValues(alpha: 0.4)),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isUser = widget.message.role == 'user';
    final isMobile = MediaQuery.sizeOf(context).width < 600;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      child: Row(
        mainAxisAlignment: isUser
            ? MainAxisAlignment.end
            : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Flexible(
            child: Container(
              constraints: BoxConstraints(
                maxWidth:
                    MediaQuery.sizeOf(context).width * (isMobile ? .85 : .75),
              ),
              decoration: BoxDecoration(
                color: isUser ? GFL2Colors.panel : GFL2Colors.surface,
                borderRadius: BorderRadius.circular(4),
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
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (!isUser)
                      const Padding(
                        padding: EdgeInsets.only(bottom: 6),
                        child: Text(
                          'KLUKAI // SST-05',
                          style: TextStyle(
                            color: GFL2Colors.primary,
                            fontSize: 11,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 1.5,
                            fontFamily: 'monospace',
                          ),
                        ),
                      ),
                    if (widget.message.imageData != null)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: ArtworkView(
                          base64Data: widget.message.imageData!,
                        ),
                      ),
                    if (widget.message.content.isNotEmpty &&
                        !(widget.message.imageData != null &&
                            widget.message.content == '[IMAGE]'))
                      MessageContent(
                        source: widget.message.content,
                        browser: widget.browser,
                      ),
                    if (widget.message.isStreaming)
                      const Align(
                        alignment: Alignment.centerLeft,
                        child: _BlinkingCursor(),
                      ),
                    if (!widget.message.isStreaming)
                      MessageFooter(
                        createdAt: widget.message.createdAt,
                        isUser: isUser,
                        status: widget.message.status,
                        loading: _isLoading,
                        playing: _isPlaying,
                        onReplay: _playVoice,
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
            width: 8,
            height: 16,
            margin: const EdgeInsets.only(top: 2),
            color: GFL2Colors.primary.withValues(alpha: 0.6),
          ),
        );
      },
    );
  }
}
