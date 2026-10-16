part of 'chat_screen.dart';

/// Chat header, transcript, tool status, and input presentation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatView on _ChatScreenState {
  Widget _buildMobileLayout() {
    return Stack(
      children: [
        Column(
          children: [
            _buildHeader(),
            Expanded(child: _buildMessageList()),
            if (_activeTools.isNotEmpty) _buildToolStatus(),
            if (_state.isTyping && _streamingId == null)
              _buildProcessingIndicator(),
            _buildInputBar(),
          ],
        ),
        if (_showScrollFAB)
          Positioned(
            bottom: 80,
            right: 16,
            child: FloatingActionButton.small(
              onPressed: () => _scrollToBottom(),
              backgroundColor: GFL2Colors.surface,
              child: const Icon(
                Icons.keyboard_arrow_down,
                color: GFL2Colors.primary,
              ),
            ),
          ),
      ],
    );
  }

  Widget _buildHeader() {
    return ChatHeader(
      connected: _state.isConnected,
      mood: _state.mood,
      glow: _moodGlowColor,
      bpm: _moodBPM,
      heartbeatColor: _heartbeatSpikeOverride != null
          ? const Color(0xFFFF1744) // red flash during a spike
          : _moodGlowColor,
      ambientMuted: _ambientMuted,
      affectionScore: _state.affectionScore,
      affectionLevel: _state.affectionLevel,
      affectionLevelName: _state.affectionLevelName,
      lastAffectionDelta: _lastAffectionDelta,
      herDay: _herDay,
      onOpenProfile: _openProfile,
      onHerDayTap: _openHerDaySheet,
      onToggleAmbient: () {
        try {
          final isOn = _jsToggleAmbientMute().toDart;
          _update(() => _ambientMuted = !isOn);
        } catch (_) {
          _update(() => _ambientMuted = !_ambientMuted);
        }
      },
      onCompanion: _openCompanion,
      onHerPov: _openHerPov,
      onArchive: _openArchive,
      onSubscription: _openSubscription,
      onLogout: _logout,
    );
  }

  Widget _buildMessageList() {
    if (_messages.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Large portrait
            Container(
              width: 96,
              height: 96,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(6),
                border: Border.all(
                  color: GFL2Colors.primary.withValues(alpha: 0.3),
                  width: 1.5,
                ),
                boxShadow: [
                  BoxShadow(
                    color: GFL2Colors.primary.withValues(alpha: 0.08),
                    blurRadius: 16,
                  ),
                ],
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(5),
                child: Image.asset(
                  'assets/klukai_portrait.png',
                  fit: BoxFit.cover,
                  errorBuilder: (_, e, s) => Container(
                    color: GFL2Colors.panel,
                    child: const Center(
                      child: Text(
                        'K',
                        style: TextStyle(
                          color: GFL2Colors.primary,
                          fontSize: 36,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 16),
            Text(
              'KLUKAI // SST-05',
              style: TextStyle(
                color: GFL2Colors.primary.withValues(alpha: 0.6),
                fontSize: 13,
                fontWeight: FontWeight.w700,
                letterSpacing: 2.0,
                fontFamily: 'monospace',
              ),
            ),
            const SizedBox(height: 6),
            Text(
              'H.I.D.E. 404 — NEURAL LINK ESTABLISHED',
              style: TextStyle(
                color: GFL2Colors.textDim.withValues(alpha: 0.35),
                fontSize: 10,
                letterSpacing: 1.0,
                fontFamily: 'monospace',
              ),
            ),
            const SizedBox(height: 12),
            Text(
              'Awaiting orders, Commander.',
              style: TextStyle(
                color: GFL2Colors.textDim.withValues(alpha: 0.3),
                fontSize: 13,
                fontStyle: FontStyle.italic,
              ),
            ),
            const SizedBox(height: 20),
            // Conversation starters — adapt to affection level
            Wrap(
              spacing: 8,
              runSpacing: 8,
              alignment: WrapAlignment.center,
              children: _getStarters().map(_starterChip).toList(),
            ),
          ],
        ),
      );
    }

    return Focus(
      // Keyboard scroll-nav (PageUp/PageDown/Home/End) for the message list.
      // Non-nav keys are returned as ignored so typing/shortcuts still work.
      onKeyEvent: _handleKeyScroll,
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
          final bubble = MessageBubble(message: msg, browser: widget.browser);
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
    );
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
      padding: const EdgeInsets.only(left: 14, right: 14, bottom: 4),
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
                  color: GFL2Colors.primary.withValues(alpha: 0.5),
                ),
              ),
              const SizedBox(width: 8),
              Flexible(
                child: Text(
                  displayText.toUpperCase(),
                  style: TextStyle(
                    color: GFL2Colors.primary.withValues(alpha: 0.5),
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    fontFamily: 'monospace',
                    letterSpacing: 0.8,
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
          ClipRRect(
            borderRadius: BorderRadius.circular(2),
            child: LinearProgressIndicator(
              minHeight: 2,
              backgroundColor: GFL2Colors.primary.withValues(alpha: 0.12),
              valueColor: AlwaysStoppedAnimation<Color>(
                GFL2Colors.primary.withValues(alpha: 0.6),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildInputBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(8, 8, 8, 12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        border: Border(
          top: BorderSide(color: GFL2Colors.border.withValues(alpha: 0.4)),
        ),
      ),
      child: Row(
        children: [
          VoiceButton(
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
          ),
          const SizedBox(width: 8),
          Expanded(
            child: TextField(
              controller: _textController,
              focusNode: _focusNode,
              readOnly: _state.isInputLocked,
              style: TextStyle(
                color: _state.isInputLocked
                    ? GFL2Colors.textDim.withValues(alpha: 0.3)
                    : GFL2Colors.textPrimary,
                fontSize: 14,
              ),
              maxLines: 4,
              minLines: 1,
              textInputAction: TextInputAction.send,
              onSubmitted: _inputDisabled ? null : (_) => _sendMessage(),
              decoration: InputDecoration(
                hintText: _state.isInputLocked
                    ? '// ${_state.inputLockReason ?? "STANDBY"}...'
                    : '// ENTER COMMAND...',
                hintStyle: TextStyle(
                  color: _state.isInputLocked
                      ? _moodGlowColor.withValues(alpha: 0.4)
                      : GFL2Colors.textDim.withValues(alpha: 0.35),
                  fontFamily: 'monospace',
                  fontSize: 13,
                ),
                filled: true,
                fillColor: GFL2Colors.background,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 10,
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(4),
                  borderSide: BorderSide(
                    color: GFL2Colors.border.withValues(alpha: 0.3),
                  ),
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(4),
                  borderSide: BorderSide(
                    color: GFL2Colors.border.withValues(alpha: 0.3),
                  ),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(4),
                  borderSide: const BorderSide(color: GFL2Colors.primary),
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          IconButton(
            tooltip: 'Send',
            onPressed: _inputDisabled ? null : _sendMessage,
            icon: const Icon(Icons.send, color: Colors.white, size: 20),
            style: IconButton.styleFrom(
              backgroundColor: _inputDisabled
                  ? GFL2Colors.border
                  : GFL2Colors.accent,
              fixedSize: const Size(44, 44),
              shape: const CircleBorder(),
            ),
          ),
        ],
      ),
    );
  }
}
