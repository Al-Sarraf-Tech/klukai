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
    // Her stage iframe loads on his first flip to 3D (or quietly a few
    // seconds after the transcript, once he has used 3D on this device) and
    // then stays loaded, socket and all, across every CHAT ⇄ 3D flip, so 3D
    // shows her at once instead of reloading. It is never moved in the tree
    // or rebuilt (that would reload it): it is always the last child of the
    // tab's layout, under its own key, whichever face is up.
    //
    // 3D: a solid top bar and a solid composer strip, and her stage exactly
    // in the rect between them: nothing Flutter draws covers the iframe, so
    // the web engine needs no overlay canvas over it (overlays repainting
    // over an iframe are what made the page flicker on iPhone).
    //
    // CHAT: the opaque CHAT face fills the tab; the stage behind it is
    // parked: told to stop its render loop and sound (klukai:visibility),
    // hidden and deaf to taps, and once its page has loaded not even
    // painted (see paintStage). Another tab tears it (and its socket) down —
    // the tab stack stops painting the Chat tab, and a parked scene is too
    // much to hold on a phone meanwhile; Chat loads it again.
    final threeD = _view == ChatViewMode.threeD;
    _scheduleHudSync();
    return CustomMultiChildLayout(
      key: const Key('chat-tab'),
      delegate: _ChatTabLayout(_stageFrame),
      children: [
        if (threeD) ...[
          LayoutId(id: _ChatTabSlot.top, child: _buildStageTopStrip()),
          LayoutId(id: _ChatTabSlot.bottom, child: _buildStageBottomStrip()),
        ] else
          LayoutId(
            id: _ChatTabSlot.face,
            child: HudBackground(child: _buildChatLayout()),
          ),
        if (_stageMounted)
          LayoutId(
            id: _ChatTabSlot.stage,
            child: _buildStage(visible: threeD),
          ),
      ],
    );
  }

  /// Overflow-free at any height: the header compresses (and past half the
  /// height scrolls), tool status is capped, the transcript takes what is
  /// left and the composer stays put.
  Widget _buildChatLayout() {
    return LayoutBuilder(
      builder: (context, box) => Column(
        children: [
          ConstrainedBox(
            constraints: BoxConstraints(maxHeight: box.maxHeight * 0.5),
            child: SingleChildScrollView(
              // The unread-bond line scrolls with the header. A fixed row
              // under the transcript overflowed the composer when the
              // keyboard was up and the type was large.
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _buildHeader(
                    compact:
                        _keyboardUp ||
                        box.maxHeight < _ChatView._compactHeaderBelow,
                  ),
                  if (_affectionFailed) _buildAffectionNotice(),
                ],
              ),
            ),
          ),
          Expanded(child: _buildTranscriptArea()),
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
    );
  }

  void _setView(ChatViewMode next) {
    if (next == _view) return;
    _stageWarmTimer?.cancel();
    if (next == ChatViewMode.threeD) _rememberStageUsed();
    _update(() {
      _view = next;
      // Either way the stage has been, or is about to be, on screen: keep
      // it. Its readiness and notices still hold; her voice stops (a parked
      // stage stops it).
      _stageKept = true;
      if (_stageVoice == VoicePlayState.playing) {
        _stageVoice = VoicePlayState.idle;
      }
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
      case ChatMenuAction.logout:
        _logout();
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
      affectionKnown: !_affectionFailed,
      lastAffectionDelta: _lastAffectionDelta,
      herDay: _herDay,
      view: _view,
      onViewChanged: _setView,
      onMenu: _onMenu,
      onOpenProfile: _openProfile,
      onHerDayTap: _openHerDaySheet,
      compact: compact,
    );
  }

  Widget _buildAffectionNotice() => Semantics(
    key: const Key('chat-affection-unread'),
    liveRegion: true,
    child: Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 8, 0),
      child: Row(
        children: [
          const Icon(Icons.favorite_border, size: 16, color: HudColors.textDim),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              "Couldn't read the bond.",
              style: HudText.body(size: 13, color: HudColors.textDim),
            ),
          ),
          TextButton(
            key: const Key('chat-affection-retry'),
            onPressed: _loadAffection,
            style: TextButton.styleFrom(
              foregroundColor: HudColors.cyan,
              minimumSize: const Size(64, 48),
            ),
            child: const Text('Retry'),
          ),
        ],
      ),
    ),
  );

  Widget _buildComposer() {
    return ChatComposerBar(
      controller: _textController,
      availableHeight:
          MediaQuery.sizeOf(context).height -
          MediaQuery.viewInsetsOf(context).bottom,
      focusNode: _focusNode,
      language: _voiceLanguage,
      onLanguageChanged: _voiceLanguageSaving ? null : _setVoiceLanguage,
      onSend: _sendMessage,
      onPicture: _requestPicture,
      disabled: _inputDisabled,
      locked: _state.isInputLocked,
      lockReason: _state.inputLockReason,
      lockColor: _moodGlowColor,
      leading: (widget.voiceInputEnabled ?? _serverVoiceInputEnabled)
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
      if (_historyLoading || _historyFailed) return const SizedBox.expand();
      final linkDown = !_state.isConnected;
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
                      linkDown
                          ? 'SECURE LINK LOST'
                          : 'H.I.D.E. 404 — NEURAL LINK ESTABLISHED',
                      textAlign: TextAlign.center,
                      style: HudText.label(
                        size: 9.5,
                        color: linkDown
                            ? HudColors.danger
                            : HudColors.textMuted,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      linkDown
                          ? 'She cannot hear this until the link returns.'
                          : 'Awaiting orders, Commander.',
                      key: linkDown ? const Key('chat-link-down') : null,
                      textAlign: TextAlign.center,
                      style: HudText.body(size: 15, color: HudColors.textDim),
                    ),
                    if (!linkDown) ...[
                      const SizedBox(height: 16),
                      // Conversation starters — adapt to affection level
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        alignment: WrapAlignment.center,
                        children: _getStarters().map(_starterChip).toList(),
                      ),
                    ],
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
        child: CustomScrollView(
          controller: _scrollController,
          physics: const ClampingScrollPhysics(
            parent: AlwaysScrollableScrollPhysics(),
          ),
          // Older pages sit above the anchor and grow upward, so one arriving
          // never moves the line he is reading.
          center: _transcriptAnchor,
          slivers: [
            SliverList(
              delegate: SliverChildBuilderDelegate(
                // Built from the anchor upward: 0 is the newest older line.
                (context, i) {
                  if (i == _older.length) return _historyTop();
                  final at = _older.length - 1 - i;
                  return _transcriptLine(
                    _older[at],
                    at > 0 ? _older[at - 1] : null,
                  );
                },
                findChildIndexCallback: (key) {
                  final at = _older.indexWhere(
                    (message) => ValueKey(message.presentationId) == key,
                  );
                  return at < 0 ? null : _older.length - 1 - at;
                },
                childCount: _older.length + 1,
              ),
            ),
            SliverPadding(
              key: _transcriptAnchor,
              padding: const EdgeInsets.only(bottom: 8),
              sliver: SliverList(
                delegate: SliverChildBuilderDelegate(
                  (context, index) => _transcriptLine(
                    _messages[index],
                    index > 0
                        ? _messages[index - 1]
                        : (_older.isNotEmpty ? _older.last : null),
                  ),
                  findChildIndexCallback: (key) {
                    final at = _messages.indexWhere(
                      (message) => ValueKey(message.presentationId) == key,
                    );
                    return at < 0 ? null : at;
                  },
                  childCount: _messages.length,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// One line of the transcript, with a date divider when its day differs
  /// from the line before it (or it is the first line loaded).
  Widget _transcriptLine(ChatMessage msg, ChatMessage? prev) {
    final voiceId = _voiceNotes[msg.id];
    final bubble = MessageBubble(
      key: ValueKey(msg.presentationId),
      message: msg,
      browser: widget.browser,
      voiceOverride: voiceId == null
          ? null
          : () => _fetchAndPlayVoiceNote(voiceId),
    );
    final showDivider =
        prev == null ||
        DateUtils.dateOnly(prev.createdAt) != DateUtils.dateOnly(msg.createdAt);
    // Both the sliver row and its bubble retain identity when reconnect adds
    // earlier rows or the canonical timestamp changes this date boundary.
    return Column(
      key: ValueKey(msg.presentationId),
      mainAxisSize: MainAxisSize.min,
      children: [
        if (showDivider) DateDivider(date: msg.createdAt),
        bubble,
      ],
    );
  }

  /// The top of what is loaded: a spinner while an older page comes in,
  /// otherwise the list's top margin.
  Widget _historyTop() {
    if (_olderFailed) {
      return _historyNotice(
        label: 'Older messages could not be loaded.',
        retryKey: const Key('chat-older-retry'),
        onRetry: () => _loadOlder(retry: true),
      );
    }
    if (!_loadingOlder) return const SizedBox(height: 8);
    return const Padding(
      key: Key('chat-history-loading'),
      padding: EdgeInsets.symmetric(vertical: 12),
      child: Center(
        child: SizedBox(
          width: 18,
          height: 18,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      ),
    );
  }

  /// Tracks whether he is at the newest line, and keeps it there when the
  /// transcript's end moves without him scrolling (see [_pinnedToBottom]).
  ///
  /// Only while no scroll is in progress: scrolling up lays out older
  /// bubbles, which corrects the list's extent, and re-pinning then would
  /// jump back down and cancel his drag (the "can't scroll up" bug).
  bool _onTranscriptScroll(Notification notification) {
    if (notification is ScrollStartNotification) {
      _transcriptScrolling = true;
    } else if (notification is ScrollUpdateNotification) {
      final metrics = notification.metrics;
      _pinnedToBottom = metrics.maxScrollExtent - metrics.pixels < 48;
      // Near the top of what is loaded: fetch the page before it.
      if (metrics.axis == Axis.vertical &&
          metrics.pixels - metrics.minScrollExtent < 600 &&
          (notification.scrollDelta ?? 0) < 0) {
        _loadOlder();
      }
    } else if (notification is ScrollEndNotification) {
      _transcriptScrolling = false;
      final metrics = notification.metrics;
      _pinnedToBottom = metrics.maxScrollExtent - metrics.pixels < 48;
    } else if (notification is ScrollMetricsNotification &&
        _pinnedToBottom &&
        !_transcriptScrolling) {
      final metrics = notification.metrics;
      if (metrics.maxScrollExtent - metrics.pixels > 1) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (_pinnedToBottom &&
              !_transcriptScrolling &&
              _scrollController.hasClients &&
              !_scrollController.position.isScrollingNotifier.value) {
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
          // status or the client's fallback waiting notice. The
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
