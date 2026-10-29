part of 'chat_screen.dart';

/// Outgoing text, waiting hints, input errors, and keyboard transcript navigation.
/// Uses the existing text/scroll controllers and the one transport owner.
extension _ChatComposer on _ChatScreenState {
  void _sendMessage() {
    if (_inputDisabled) return;
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    if (!_transmit(text)) return; // draft stays in the composer for retry
    _textController.clear();
    _focusNode.requestFocus();
  }

  /// The camera button: asks her for a picture through the ordinary message
  /// path (her image tool answers it). His draft is left alone.
  void _requestPicture() {
    if (_inputDisabled) return;
    _transmit(kPictureRequest);
  }

  /// Sends [text] and echoes it in the transcript. False (and a notice) when
  /// the link refused it, so nothing that never left is shown as sent.
  bool _transmit(String text) {
    if (!_ws.sendMessage(text)) {
      // Channel is gone (race with a disconnect) — don't echo a message that
      // never left.
      _showSendError();
      return false;
    }
    _update(() {
      _messages.add(
        ChatMessage(
          id: _newLocalMessageId('user'),
          role: 'user',
          content: text,
        ),
      );
    });
    _scrollToBottom();
    _startWarmupHint();
    return true;
  }

  // Elapsed silence only tells us that a reply has not arrived. Keep the
  // fallback honest; real server status always takes precedence.
  void _startWarmupHint() {
    _clearWarmupHint();
    _warmupTimer = Timer(const Duration(seconds: 7), () {
      if (!mounted || _streamingId != null) return;
      _update(() {
        _state = _state.copyWith(isTyping: true);
        _thinkingText = 'AWAITING HER REPLY…';
      });
      _warmupTimer = Timer(const Duration(seconds: 15), () {
        if (!mounted || _streamingId != null) return;
        _update(() {
          _thinkingText = 'STILL WAITING // thank you for your patience';
        });
      });
    });
  }

  void _clearWarmupHint() {
    _warmupTimer?.cancel();
    _warmupTimer = null;
  }

  /// A short HUD notice above the composer.
  void _showHudNotice(String text, {Key? key}) {
    if (!mounted) return;
    final messenger = ScaffoldMessenger.maybeOf(context);
    if (messenger == null) return;
    messenger.showSnackBar(
      SnackBar(
        key: key,
        content: Text(
          text,
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

  void _showSendError() => _showHudNotice('TRANSMISSION FAILED // LINK DOWN');

  /// A failed recording leaves the keyboard and its draft available.
  void _showVoiceLinkError([
    String message = '// NO SPEECH HEARD — try again or type.',
  ]) => _showHudNotice(message);

  /// Keyboard scroll-navigation for the message list: PageUp/PageDown jump a
  /// near-viewport, Home/End jump to the extremes. Returns
  /// [KeyEventResult.handled] when it consumes a nav key (so the keystroke is
  /// swallowed) and [KeyEventResult.ignored] otherwise — letting every other
  /// key (including typing) fall through untouched. Wired to the [Focus] that
  /// wraps the message list in [_buildMessageList].
  KeyEventResult _handleKeyScroll(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (!_scrollController.hasClients) return KeyEventResult.ignored;

    final pos = _scrollController.position;
    final pageSize = pos.viewportDimension * 0.8;
    final key = event.logicalKey;

    double? target;
    if (key == LogicalKeyboardKey.pageDown) {
      target = (pos.pixels + pageSize).clamp(
        pos.minScrollExtent,
        pos.maxScrollExtent,
      );
    } else if (key == LogicalKeyboardKey.pageUp) {
      target = (pos.pixels - pageSize).clamp(
        pos.minScrollExtent,
        pos.maxScrollExtent,
      );
    } else if (key == LogicalKeyboardKey.home) {
      target = pos.minScrollExtent;
    } else if (key == LogicalKeyboardKey.end) {
      target = pos.maxScrollExtent;
    } else {
      // Not a navigation key — let it propagate (e.g. so typing still works).
      return KeyEventResult.ignored;
    }

    _scrollController.animateTo(
      target,
      duration: const Duration(milliseconds: 400),
      curve: Curves.easeOutCubic,
    );
    return KeyEventResult.handled;
  }
}
