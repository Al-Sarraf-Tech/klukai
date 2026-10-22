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

class _MessageBubbleState extends State<MessageBubble>
    with AutomaticKeepAliveClientMixin {
  bool _isPlaying = false;
  bool _isLoading = false;
  int _voiceEpoch = 0;

  bool _ownsVoice(int epoch) => mounted && epoch == _voiceEpoch;

  /// From his tap on `▶ VOICE` until her line has ended. Meanwhile the
  /// bubble keeps its place in the lazy transcript: scrolled out of view and
  /// disposed, it cancelled the clip made ahead and cut her off mid-reply.
  bool _speaking = false;

  @override
  bool get wantKeepAlive => _speaking;

  void _setSpeaking(bool on) {
    if (_speaking == on || !mounted) return;
    _speaking = on;
    updateKeepAlive();
  }

  /// Synthesis in flight: the clip about to play and the one made ahead of
  /// it. Leaving the screen cancels them, and their jobs on the server.
  final Set<TtsAudioRequest> _activeTts = {};

  @override
  void didUpdateWidget(MessageBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.message.presentationId == widget.message.presentationId) {
      return;
    }
    _voiceEpoch++;
    _cancelTts();
    _isLoading = false;
    _isPlaying = false;
    _setSpeaking(false);
  }

  void _cancelTts() {
    for (final request in _activeTts) {
      unawaited(request.cancel());
    }
    _activeTts.clear();
  }

  @override
  void dispose() {
    _voiceEpoch++;
    _cancelTts();
    super.dispose();
  }

  /// Starts synthesizing [text] in the background.
  _Clip _synthesize(http.Client client, String serverUrl, String text) {
    final request = TtsAudioRequest(
      client: client,
      baseUrl: serverUrl,
      token: readAuthToken(),
    );
    _activeTts.add(request);
    final clip = _Clip(request, request.run(text));
    clip.settled.whenComplete(() => _activeTts.remove(request));
    return clip;
  }

  Future<void> _playVoice() async {
    if (_isLoading || _isPlaying) return;
    if (widget.message.content.isEmpty) return;
    final epoch = ++_voiceEpoch;
    _setSpeaking(true);
    try {
      await _speak(epoch);
    } finally {
      if (_ownsVoice(epoch)) _setSpeaking(false);
    }
  }

  Future<void> _speak(int epoch) async {
    setState(() => _isLoading = true);

    final override = widget.voiceOverride;
    if (override != null) {
      var heard = false;
      try {
        heard = await override();
      } catch (e) {
        debugPrint('Voice playback failed: $e');
      }
      if (_ownsVoice(epoch)) {
        setState(() => _isLoading = false);
        if (!heard) _showVoiceUnavailable();
      }
      return;
    }

    var spoke = false;
    // An injected client is the caller's to close; one we open here is ours.
    final ownsClient = widget.client == null;
    final client = widget.client ?? http.Client();
    final clips = <_Clip>[];
    try {
      // Get server URL from the page origin
      final serverUrl = Uri.base.origin.contains('localhost')
          ? 'http://localhost:8300'
          : Uri.base.origin;

      // As few clips as fit (usually one), each made while the one before
      // it plays, so a long reply runs on without a pause between clips.
      final chunks = chunkForTts(widget.message.content);
      for (var i = 0; i < chunks.length; i++) {
        if (!_ownsVoice(epoch)) break;
        if (i == 0) clips.add(_synthesize(client, serverUrl, chunks[0]));
        final clip = clips[i];
        if (!clip.done) {
          setState(() {
            _isLoading = true;
            _isPlaying = false;
          });
        }
        final audioB64 = await clip.audio;
        if (!_ownsVoice(epoch)) break;
        if (i + 1 < chunks.length) {
          clips.add(_synthesize(client, serverUrl, chunks[i + 1]));
        }
        spoke = true;
        setState(() {
          _isLoading = false;
          _isPlaying = true;
        });
        // Reuse the gesture-unlocked channel and await each clip so they
        // play in order, including replies longer than 500 chars.
        final played = await (widget.browser ?? defaultBrowserPlatform())
            .playVoice('data:audio/wav;base64,$audioB64');
        if (!played) throw StateError('Browser voice playback unavailable');
      }
    } catch (e) {
      spoke = false;
      if (_ownsVoice(epoch)) debugPrint('Voice playback failed: $e');
    } finally {
      // A clip made ahead that will not play now: cancel it (and its job),
      // and let every request settle before its client closes.
      for (final clip in clips) {
        if (!clip.done) unawaited(clip.request.cancel());
      }
      await Future.wait(clips.map((clip) => clip.settled));
      if (ownsClient) client.close();
    }

    // Any failure (502 from a powered-off GPU host, timeout, transport error)
    // must drop the spinner and say so — the text still stands on its own.
    if (_ownsVoice(epoch)) {
      setState(() {
        _isLoading = false;
        _isPlaying = false;
      });
    }
    if (!spoke && _ownsVoice(epoch)) _showVoiceUnavailable();
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
    super.build(context);
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

/// One voice clip being synthesized.
class _Clip {
  _Clip(this.request, this.audio) {
    settled = audio.then<void>((_) {}, onError: (Object _) {});
    settled.whenComplete(() => done = true);
  }

  final TtsAudioRequest request;

  /// Her clip as base64 WAV; fails if synthesis does.
  final Future<String> audio;

  /// Completes, without an error, once [audio] has.
  late final Future<void> settled;

  /// [audio] has completed.
  bool done = false;
}
