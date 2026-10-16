part of 'chat_screen.dart';

/// Outgoing text, waiting hints, input errors, and keyboard transcript navigation.
/// Uses the existing text/scroll controllers and the one transport owner.
extension _ChatComposer on _ChatScreenState {
  void _sendMessage() {
    if (_inputDisabled) return;
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    if (!_ws.sendMessage(text)) {
      // Channel is gone (race with a disconnect) — don't echo a message that
      // never left, and keep the draft in the composer for retry.
      _showSendError();
      return;
    }
    _update(() {
      _messages.add(
        ChatMessage(
          id: 'user-${DateTime.now().millisecondsSinceEpoch}',
          role: 'user',
          content: text,
        ),
      );
    });
    _textController.clear();
    _focusNode.requestFocus();
    _scrollToBottom();
    _startWarmupHint();
  }

  // When she's been idle the local model evicts (load-on-demand), so the first
  // message waits on a JIT model load and then a prefill ("processing prompt")
  // pass before the first token. Narrate that sequence so a slow first reply
  // reads as progress, not a hang. Two phases, keyed to elapsed silence — they
  // only ever surface on a genuinely cold turn, because a warm reply streams its
  // first token within a second or two and clears them. Phase 1 (~7s): the model
  // is JIT-loading from cold. Phase 2 (~22s): it's loaded and warming/prefilling
  // your message. Cleared the instant she responds (token/thinking/done/error).
  void _startWarmupHint() {
    _clearWarmupHint();
    _warmupTimer = Timer(const Duration(seconds: 7), () {
      if (!mounted || _streamingId != null) return;
      _update(() {
        _state = _state.copyWith(isTyping: true);
        _thinkingText = 'WAKING HER UP // loading the model from cold';
      });
      _warmupTimer = Timer(const Duration(seconds: 15), () {
        if (!mounted || _streamingId != null) return;
        _update(() {
          _thinkingText = 'WARMING UP // processing your message';
        });
      });
    });
  }

  void _clearWarmupHint() {
    _warmupTimer?.cancel();
    _warmupTimer = null;
  }

  void _showSendError() {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text(
          'TRANSMISSION FAILED // LINK DOWN',
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

  /// The push-to-talk clip went nowhere: STT is down (the GPU host is off),
  /// timed out, or heard nothing. Short, in her register; the keyboard works.
  void _showVoiceLinkError() {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text(
          '// VOICE LINK GARBLED — try again or type.',
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
