part of 'chat_screen.dart';

/// The Chat tab's transcript face: HUD header, messages, tool status and the
/// composer. Shares the screen's single State owner; no transport or state is
/// duplicated.
extension _ChatView on _ChatScreenState {
  /// The on-screen keyboard is up: the header folds to one line.
  bool get _keyboardUp => MediaQuery.viewInsetsOf(context).bottom > 0;

  /// Below this body height the full header (about 240px) would leave the
  /// transcript too little room: it folds to one line, as with the keyboard.
  static const _compactHeaderBelow = 500.0;

  Widget _buildMobileLayout() {
    // The iframe only lives while the 3D face of the Chat tab is on screen;
    // another tab or the CHAT face tears it (and its socket) down.
    if (_view == ChatViewMode.threeD) {
      return _tab == HudTab.chat
          ? _buildStageLayout()
          : const ColoredBox(color: HudColors.ground);
    }
    return HudBackground(child: _buildChatLayout());
  }

  /// Overflow-free at any height: the header compresses (and past half the
  /// height scrolls), tool status is capped, the transcript takes what is
  /// left and the composer stays put.
  Widget _buildChatLayout() {
    return LayoutBuilder(
      builder: (context, box) => Stack(
        children: [
          Column(
            children: [
              ConstrainedBox(
                constraints: BoxConstraints(maxHeight: box.maxHeight * 0.5),
                child: SingleChildScrollView(
                  child: _buildHeader(
                    compact:
                        _keyboardUp ||
                        box.maxHeight < _ChatView._compactHeaderBelow,
                  ),
                ),
              ),
              Expanded(child: _buildMessageList()),
              if (_activeTools.isNotEmpty)
                ConstrainedBox(
                  constraints: BoxConstraints(maxHeight: box.maxHeight * 0.2),
                  child: SingleChildScrollView(child: _buildToolStatus()),
                ),
              if (_state.isTyping && _streamingId == null)
                _buildProcessingIndicator(),
              _buildComposer(),
            ],
          ),
          if (_showScrollFAB)
            Positioned(
              bottom: 70,
              right: 16,
              child: HudSquareButton(
                key: const Key('chat-latest'),
                tooltip: 'Latest',
                onTap: () => _scrollToBottom(),
                child: const Icon(Icons.keyboard_arrow_down),
              ),
            ),
        ],
      ),
    );
  }

  void _setView(ChatViewMode next) {
    if (next == _view) return;
    _stageSpeakTimer?.cancel();
    _update(() {
      _view = next;
      _stageReady = false;
      _stageNotice = null;
      _stageVoice = VoicePlayState.idle;
      _pendingSpeakId = null;
      _pendingSpeakLine = null;
    });
    if (next == ChatViewMode.chat) _scrollToBottom(instant: true, retries: 2);
  }

  void _onMenu(ChatMenuAction action) {
    switch (action) {
      case ChatMenuAction.companion:
        _openCompanion();
      case ChatMenuAction.threads:
        _openThreads();
      case ChatMenuAction.herPov:
        _openHerPov();
      case ChatMenuAction.subscription:
        _openSubscription();
      case ChatMenuAction.ambient:
        _toggleAmbient();
      case ChatMenuAction.logout:
        _logout();
    }
  }

  void _toggleAmbient() {
    try {
      final isOn = _jsToggleAmbientMute().toDart;
      _update(() => _ambientMuted = !isOn);
    } catch (_) {
      _update(() => _ambientMuted = !_ambientMuted);
    }
  }

  Widget _buildHeader({required bool compact}) {
    return ChatHeader(
      connected: _state.isConnected,
      mood: _state.mood,
      glow: _moodGlowColor,
      bpm: _moodBPM,
      spiking: _heartbeatSpikeOverride != null,
      affectionScore: _state.affectionScore,
      affectionLevel: _state.affectionLevel,
      affectionLevelName: _state.affectionLevelName,
      lastAffectionDelta: _lastAffectionDelta,
      herDay: _herDay,
      view: _view,
      onViewChanged: _setView,
      onMenu: _onMenu,
      ambientMuted: _ambientMuted,
      onOpenProfile: _openProfile,
      onHerDayTap: _openHerDaySheet,
      compact: compact,
    );
  }

  Widget _buildComposer() {
    return ChatComposerBar(
      controller: _textController,
      focusNode: _focusNode,
      language: _voiceLanguage,
      onLanguageChanged: _voiceLanguageSaving ? null : _setVoiceLanguage,
      onSend: _sendMessage,
      onPicture: _requestPicture,
      disabled: _inputDisabled,
      locked: _state.isInputLocked,
      lockReason: _state.inputLockReason,
      lockColor: _moodGlowColor,
      leading: widget.voiceInputEnabled
          ? VoiceButton(
              isRecording:
                  _speech.phase == SpeechPhase.recording ||
                  _speech.phase == SpeechPhase.requestingPermission,
              enabled: !_inputDisabled,
              onTapDown: () {
                _onVoiceDown();
              },
              onTapUp: () {
                _onVoiceUp();
              },
              onCancel: () {
                _cancelVoice();
              },
            )
          : null,
    );
  }

  Widget _buildMessageList() {
    if (_messages.isEmpty) {
      return LayoutBuilder(
        builder: (context, box) => SingleChildScrollView(
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: box.maxHeight),
            child: Center(
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 12,
                ),
                child: Column(
                  key: const Key('chat-empty'),
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      'KLUKAI // SST-05',
                      style: HudText.label(size: 12, color: HudColors.cyan),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      'H.I.D.E. 404 — NEURAL LINK ESTABLISHED',
                      textAlign: TextAlign.center,
                      style: HudText.label(
                        size: 9.5,
                        color: HudColors.textMuted,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      'Awaiting orders, Commander.',
                      style: HudText.body(size: 15, color: HudColors.textDim),
                    ),
                    const SizedBox(height: 16),
                    // Conversation starters — adapt to affection level
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      alignment: WrapAlignment.center,
                      children: _getStarters().map(_starterChip).toList(),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
    }

    return Focus(
      // Keyboard scroll-nav (PageUp/PageDown/Home/End) for the message list.
      // Non-nav keys are returned as ignored so typing/shortcuts still work.
      onKeyEvent: _handleKeyScroll,
      child: NotificationListener<Notification>(
        onNotification: _onTranscriptScroll,
        child: ListView.builder(
          controller: _scrollController,
          physics: const ClampingScrollPhysics(
            parent: AlwaysScrollableScrollPhysics(),
          ),
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: _messages.length,
          itemBuilder: (context, index) {
            final msg = _messages[index];
            final msgDate = DateTime(
              msg.createdAt.year,
              msg.createdAt.month,
              msg.createdAt.day,
            );
            bool showDivider = false;
            if (index == 0) {
              showDivider = true;
            } else {
              final prev = _messages[index - 1];
              final prevDate = DateTime(
                prev.createdAt.year,
                prev.createdAt.month,
                prev.createdAt.day,
              );
              if (msgDate != prevDate) showDivider = true;
            }
            final voiceId = _voiceNotes[msg.id];
            final bubble = MessageBubble(
              message: msg,
              browser: widget.browser,
              voiceOverride: voiceId == null
                  ? null
                  : () => _fetchAndPlayVoiceNote(voiceId),
            );
            if (showDivider) {
              return Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  DateDivider(date: msg.createdAt),
                  bubble,
                ],
              );
            }
            return bubble;
          },
        ),
      ),
    );
  }

  /// Tracks whether he is at the newest line, and keeps it there when the
  /// transcript's end moves without him scrolling (see [_pinnedToBottom]).
  bool _onTranscriptScroll(Notification notification) {
    if (notification is ScrollUpdateNotification ||
        notification is ScrollEndNotification) {
      final metrics = (notification as ScrollNotification).metrics;
      _pinnedToBottom = metrics.maxScrollExtent - metrics.pixels < 48;
    } else if (notification is ScrollMetricsNotification && _pinnedToBottom) {
      final metrics = notification.metrics;
      if (metrics.maxScrollExtent - metrics.pixels > 1) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (_pinnedToBottom && _scrollController.hasClients) {
            _scrollController.jumpTo(
              _scrollController.position.maxScrollExtent,
            );
          }
        });
      }
    }
    return false;
  }

  Widget _buildToolStatus() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final tool in _activeTools)
          ToolStatusIndicator(toolName: tool['tool']!, status: tool['status']!),
      ],
    );
  }

  Widget _buildProcessingIndicator() {
    final displayText = _thinkingText ?? 'PROCESSING';
    return Padding(
      padding: const EdgeInsets.only(left: 16, right: 16, bottom: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(
                  strokeWidth: 1.5,
                  color: HudColors.cyanA(0.6),
                ),
              ),
              const SizedBox(width: 8),
              Flexible(
                child: Text(
                  displayText.toUpperCase(),
                  style: HudText.label(
                    size: 10,
                    color: HudColors.cyanA(0.7),
                    weight: FontWeight.w600,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
          ),
          // Loading bar for the whole pre-response wait — backend "thinking"
          // status, cold-start model load, prefill, or normal processing. The
          // enclosing indicator only renders while waiting (isTyping &&
          // _streamingId == null), so this is always the right time to show it.
          // Indeterminate on purpose: the wait varies from seconds to minutes,
          // so a guessed percentage would mislead.
          const SizedBox(height: 6),
          LinearProgressIndicator(
            minHeight: 2,
            backgroundColor: HudColors.cyanA(0.12),
            valueColor: AlwaysStoppedAnimation<Color>(HudColors.cyanA(0.7)),
          ),
        ],
      ),
    );
  }
}
