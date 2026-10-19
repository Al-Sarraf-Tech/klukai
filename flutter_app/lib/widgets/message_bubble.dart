import 'dart:async';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../models/message.dart';
import '../platform/browser.dart';
import '../services/session_auth.dart';
import '../theme/hud.dart';
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

  /// Plays this line some other way than reading it through /api/tts (a
  /// voice letter's recorded note). Resolves true once it was heard. Still
  /// only ever runs from a tap on `▶ VOICE`.
  final Future<bool> Function()? voiceOverride;

  const MessageBubble({
    super.key,
    required this.message,
    this.browser,
    this.client,
    this.voiceOverride,
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

    final override = widget.voiceOverride;
    if (override != null) {
      var heard = false;
      try {
        heard = await override();
      } catch (e) {
        debugPrint('Voice playback failed: $e');
      }
      if (mounted) setState(() => _isLoading = false);
      if (!heard) _showVoiceUnavailable();
      return;
    }

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
        content: Text(
          '// VOICE SYNTH OFFLINE — her words stand.',
          style: HudText.label(size: 11, color: HudColors.text),
        ),
        backgroundColor: const Color(0xFF0B1118),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 4),
        shape: CutCornerBorder(
          cut: 8,
          side: BorderSide(color: HudColors.danger.withValues(alpha: 0.5)),
        ),
      ),
    );
  }

  bool get _speakable =>
      !widget.message.isStreaming &&
      widget.message.content.trim().isNotEmpty &&
      widget.message.content != '[IMAGE]';

  VoicePlayState get _voiceState => _isLoading
      ? VoicePlayState.loading
      : _isPlaying
      ? VoicePlayState.playing
      : VoicePlayState.idle;

  List<Widget> _body() => [
    if (widget.message.imageData != null)
      Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: ArtworkView(base64Data: widget.message.imageData!),
      ),
    if (widget.message.content.isNotEmpty &&
        !(widget.message.imageData != null &&
            widget.message.content == '[IMAGE]'))
      DefaultTextStyle.merge(
        style: HudText.body(size: 15),
        child: MessageContent(
          source: widget.message.content,
          browser: widget.browser,
        ),
      ),
    if (widget.message.isStreaming)
      const Align(alignment: Alignment.centerLeft, child: _BlinkingCursor()),
  ];

  /// Hers: a cyan rail on the left, `▸ KLUKAI` and `▶ VOICE` on top.
  Widget _hers() => ClipPath(
    clipper: const ShapeBorderClipper(
      shape: CutCornerBorder(
        cut: 10,
        cutTopLeft: false,
        cutTopRight: true,
        cutBottomRight: false,
      ),
    ),
    child: DecoratedBox(
      decoration: BoxDecoration(
        color: HudColors.cyanA(0.07),
        border: const Border(left: BorderSide(color: HudColors.cyan, width: 2)),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 4, 10),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SizedBox(
              height: 44,
              child: Row(
                children: [
                  Expanded(
                    child: Row(
                      children: [
                        Flexible(
                          child: Text(
                            '\u25B8 KLUKAI',
                            maxLines: 1,
                            softWrap: false,
                            overflow: TextOverflow.ellipsis,
                            style: HudText.label(
                              size: 9.5,
                              color: HudColors.cyan,
                            ),
                          ),
                        ),
                        if (!widget.message.isStreaming) ...[
                          const SizedBox(width: 8),
                          Flexible(
                            child: Text(
                              MessageFooter.clock(widget.message.createdAt),
                              maxLines: 1,
                              softWrap: false,
                              overflow: TextOverflow.clip,
                              style: HudText.label(
                                size: 9.5,
                                color: HudColors.textMuted,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                  if (_speakable)
                    HudVoiceButton(state: _voiceState, onTap: _playVoice),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: _body(),
              ),
            ),
          ],
        ),
      ),
    ),
  );

  /// A line with no markdown structure (and no picture) can shrink-wrap: its
  /// layout is plain selectable text, which supports intrinsic sizing (tables
  /// and code blocks scroll, which doesn't).
  static final _structure = RegExp(
    r'[`|#*_~\[\]>]|^\s*([-+]|\d+\.)\s',
    multiLine: true,
  );
  bool get _plain =>
      widget.message.imageData == null &&
      !_structure.hasMatch(widget.message.content);

  /// His: a violet outline, right-aligned and as wide as his words, time and
  /// tick underneath.
  Widget _his() => _plain ? IntrinsicWidth(child: _hisBox()) : _hisBox();

  Widget _hisBox() => DecoratedBox(
    decoration: ShapeDecoration(
      color: HudColors.violetA(0.08),
      shape: CutCornerBorder(
        cut: 10,
        cutTopLeft: true,
        cutBottomRight: false,
        side: BorderSide(color: HudColors.violetA(0.6)),
      ),
    ),
    child: Padding(
      padding: const EdgeInsets.fromLTRB(14, 9, 12, 6),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          ..._body(),
          if (!widget.message.isStreaming)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: MessageFooter(
                createdAt: widget.message.createdAt,
                isUser: true,
                status: widget.message.status,
              ),
            ),
        ],
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final isUser = widget.message.role == 'user';
    final width = MediaQuery.sizeOf(context).width;
    final maxWidth = width * (width < 600 ? .86 : .7);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 5),
      child: Row(
        mainAxisAlignment: isUser
            ? MainAxisAlignment.end
            : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Flexible(
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: maxWidth),
              child: isUser ? _his() : _hers(),
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
            color: HudColors.cyanA(0.6),
          ),
        );
      },
    );
  }
}
