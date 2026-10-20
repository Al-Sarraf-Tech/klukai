part of 'chat_screen.dart';

/// The Chat tab's 3D face: her companion stage in an iframe under the HUD
/// (status tags, tracking readout, her latest line with `▶ VOICE`, and the
/// same composer). Shares the screen's single State owner.
extension _ChatStage on _ChatScreenState {
  /// How long to wait for the stage to answer a speak request.
  static const _speakAnswerTimeout = Duration(seconds: 8);

  /// How long after her transcript arrives the stage starts loading behind
  /// the CHAT face: the transcript, its pictures and the socket go first.
  static const _stageWarmUpDelay = Duration(seconds: 3);

  /// The stage is loaded under the Chat tab: while the 3D face shows, and
  /// from his first flip (or the warm-up) until he leaves the tab.
  bool get _stageMounted =>
      _tab == HudTab.chat && (_view == ChatViewMode.threeD || _stageKept);

  /// The stage itself, kept at one place in the tree so it is never rebuilt
  /// (that would reload the page). Under the CHAT face it is parked: the
  /// page stops drawing and playing, and it takes no taps.
  Widget _buildStage({required bool visible}) {
    final builder = widget.stageBuilder ?? defaultCompanionStage;
    return IgnorePointer(
      key: const Key('chat-stage-layer'),
      ignoring: !visible,
      child: builder(
        context,
        src: _stageSrc ??= companionStageSrc(hidden: !visible),
        visible: visible,
        onLink: _onStageLink,
        onEvent: _onStageEvent,
      ),
    );
  }

  /// No setState: the iframe reports it during build and dispose.
  void _onStageLink(CompanionStageLink? link) {
    _stageLink = link;
    // A new stage (or none) has not heard her HUD yet.
    _sentHud = null;
    if (link == null) {
      // Gone: the next stage loads from scratch and reports for itself.
      _stageReady = false;
      _stageNotice = null;
    }
  }

  /// Loads the stage behind the CHAT face a little after the transcript, so
  /// his first flip finds her there. Only on a device where he has opened
  /// 3D before: otherwise a three.js scene would sit in a phone's memory for
  /// a view he never uses.
  void _scheduleStageWarmUp() {
    if (_stageMounted || (_stageWarmTimer?.isActive ?? false)) return;
    if (!_stageUsedHere()) return;
    _stageWarmTimer = Timer(_ChatStage._stageWarmUpDelay, () {
      if (!mounted || _tab != HudTab.chat || _stageMounted) return;
      _update(() => _stageKept = true);
    });
  }

  /// Leaving the Chat tab tears the stage down (see _buildMobileLayout).
  void _dropStage() {
    _stageWarmTimer?.cancel();
    _stageSpeakTimer?.cancel();
    _stageKept = false;
    _stageSrc = null;
    _stageVoice = VoicePlayState.idle;
    _pendingSpeakId = null;
    _pendingSpeakLine = null;
  }

  bool _stageUsedHere() {
    try {
      return web.window.localStorage.getItem(kStageUsedKey) == '1';
    } catch (_) {
      return false;
    }
  }

  void _rememberStageUsed() {
    if (_stageUsedHere()) return;
    try {
      web.window.localStorage.setItem(kStageUsedKey, '1');
    } catch (_) {}
  }

  /// Her latest line worth showing (not his, not an image-only frame).
  ChatMessage? get _latestLine {
    for (var i = _messages.length - 1; i >= 0; i--) {
      final m = _messages[i];
      if (m.role == 'user') continue;
      final text = m.content.trim();
      if (text.isEmpty || text == '[IMAGE]') continue;
      return m;
    }
    return null;
  }

  /// Everything the stage's HUD shows, as one `klukai:hud` snapshot: the
  /// readouts of the CHAT header, her latest line, and her status while she
  /// is working on a reply that has not started yet.
  Map<String, Object?> get _hudSnapshot {
    final line = _latestLine;
    final waiting = _state.isTyping && _streamingId == null;
    return {
      'level': _state.affectionLevel.clamp(0, kMaxAffinityLevel),
      'mood': moodLabel(_state.mood),
      'bpm': _moodBPM,
      'spiking': _heartbeatSpikeOverride != null,
      'wearing': _herDay?.outfit?.name ?? '',
      'line': line?.content ?? '',
      'lineId': line?.id,
      'streaming': line?.isStreaming ?? false,
      'status': waiting ? (_thinkingText ?? 'PROCESSING') : null,
    };
  }

  /// Tells the stage its HUD after this frame. Every change to what it shows
  /// goes through setState and so rebuilds the Chat tab, which calls this.
  void _scheduleHudSync() {
    if (_hudSyncScheduled) return;
    _hudSyncScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _hudSyncScheduled = false;
      if (mounted) _syncHud();
    });
  }

  /// Sends the snapshot, unless the stage already has exactly this one.
  void _syncHud() {
    final link = _stageLink;
    if (link == null) return;
    final hud = _hudSnapshot;
    final sent = _sentHud;
    if (sent != null && mapEquals(sent, hud)) return;
    if (link.sendHud(hud)) _sentHud = hud;
  }

  List<String> get _trackingLines {
    final notice = _stageNotice?.trim() ?? '';
    if (notice.isNotEmpty) return [notice.toUpperCase()];
    if (!_stageReady) return const ['LINKING 3D…'];
    final doing = _stageVoice == VoicePlayState.playing
        ? 'VOICE · LIP SYNC'
        : _state.isTyping || _streamingId != null
        ? 'THINKING'
        : 'IDLE · BREATH';
    return ['TRACKING', doing, 'GAZE: COMMANDER'];
  }

  Widget _buildStageLayout() =>
      LayoutBuilder(builder: (context, box) => _stageLayout(box.maxHeight));

  /// The HUD thins out as the height shrinks (keyboard up, landscape, small
  /// phone): the readout goes first, then the tags; her line gets shorter.
  Widget _stageLayout(double height) {
    final keyboard = _keyboardUp;
    final showTags = !keyboard && height >= 460;
    final showReadout = !keyboard && height >= 620;
    final line = _latestLine;
    final waiting = _state.isTyping && _streamingId == null;
    // The stage itself is under this layer (see _buildMobileLayout).
    return SizedBox.expand(
      child: Stack(
        key: const Key('chat-stage'),
        children: [
          Positioned(
            top: 6,
            left: 16,
            right: 16,
            child: HudPointerShield(
              child: HudTopBar(
                title: '3D LINK // LIVE',
                view: _view,
                onViewChanged: _setView,
                onMenu: _onMenu,
                ambientMuted: _ambientMuted,
              ),
            ),
          ),
          // Only real controls are shielded: everywhere else a drag or pinch
          // reaches her. Decorative parts ignore pointers; of the status
          // tags only WEARING (her day) is a control, shielded on its box.
          if (showTags) ...[
            const Positioned(
              top: 58,
              left: 16,
              child: IgnorePointer(
                key: Key('stage-bracket'),
                child: _Bracket(),
              ),
            ),
            Positioned(
              top: 56,
              right: 16,
              child: StageStatusTags(
                level: _state.affectionLevel.clamp(0, kMaxAffinityLevel),
                mood: moodLabel(_state.mood),
                bpm: _moodBPM,
                spiking: _heartbeatSpikeOverride != null,
                wearing: _herDay?.outfit?.name ?? '',
                onTap: _openHerDaySheet,
                shield: (tag) => HudPointerShield(child: tag),
              ),
            ),
            if (showReadout)
              Positioned(
                left: 16,
                top: 230,
                child: TrackingReadout(lines: _trackingLines),
              ),
          ],
          Positioned(
            left: 0,
            right: 0,
            bottom: 0,
            child: HudPointerShield(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (_activeTools.isNotEmpty) _buildToolStatus(),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    child: DialoguePanel(
                      line: line?.content ?? '',
                      streaming: line?.isStreaming ?? false,
                      status: waiting ? (_thinkingText ?? 'PROCESSING') : null,
                      voiceState: _stageVoice,
                      onVoice: line == null ? null : () => _speakInStage(line),
                      maxTextHeight: keyboard || height < 560 ? 48 : 120,
                    ),
                  ),
                  const SizedBox(height: 2),
                  _buildComposer(),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// `▶ VOICE` in 3D: the avatar says the line once, lip-synced. If the
  /// stage cannot play audio (no stage yet, or its audio would not start),
  /// the line plays through the PWA's own voice channel instead.
  void _speakInStage(ChatMessage line) {
    if (_stageVoice != VoicePlayState.idle) return;
    final id = ++_speakSeq;
    final link = _stageLink;
    if (link == null || !link.speak(line.content, id: id)) {
      _speakHere(line);
      return;
    }
    _stageSpeakTimer?.cancel();
    _update(() {
      _stageVoice = VoicePlayState.loading;
      _pendingSpeakId = id;
      _pendingSpeakLine = line;
    });
    _stageSpeakTimer = Timer(_ChatStage._speakAnswerTimeout, () {
      if (!mounted || _pendingSpeakId != id) return;
      _update(() {
        _stageVoice = VoicePlayState.idle;
        _pendingSpeakId = null;
        _pendingSpeakLine = null;
      });
      _showHudNotice('// 3D LINK DID NOT ANSWER — try again.');
    });
  }

  void _onStageEvent(String type, Map<String, Object?> data) {
    if (!mounted) return;
    switch (type) {
      case 'klukai:stage-ready':
        final ready = data['ready'] == true;
        _update(() => _stageReady = ready);
        // It is listening now: whatever it was sent before may be lost.
        if (ready) {
          _sentHud = null;
          _syncHud();
        }
      case 'klukai:stage-notice':
        final text = data['text'];
        _update(() => _stageNotice = text is String ? text : null);
      case 'klukai:speaking':
        final on = data['on'] == true;
        if (on) {
          _stageSpeakTimer?.cancel();
          _update(() {
            _stageVoice = VoicePlayState.playing;
            _pendingSpeakId = null;
            _pendingSpeakLine = null;
          });
        } else if (_stageVoice == VoicePlayState.playing) {
          _update(() => _stageVoice = VoicePlayState.idle);
        }
      case 'klukai:speak-status':
        final id = data['id'];
        if (id is! num || id.toInt() != _pendingSpeakId) return;
        _stageSpeakTimer?.cancel();
        final line = _pendingSpeakLine;
        final status = data['status'];
        if (status == 'playing') {
          // Synthesizing; `klukai:speaking` flips it to playing. Keep the
          // spinner, but don't wait forever on a line that fails to arrive.
          _stageSpeakTimer = Timer(const Duration(seconds: 30), () {
            if (mounted && _stageVoice == VoicePlayState.loading) {
              _update(() => _stageVoice = VoicePlayState.idle);
            }
          });
          return;
        }
        _update(() {
          _stageVoice = VoicePlayState.idle;
          _pendingSpeakId = null;
          _pendingSpeakLine = null;
        });
        if (status == 'unavailable' && line != null) _speakHere(line);
      case 'klukai:voice-error':
        // One notice per tap: the stage reports each failed sentence.
        if (_stageVoice == VoicePlayState.idle) return;
        _stageSpeakTimer?.cancel();
        _update(() {
          _stageVoice = VoicePlayState.idle;
          _pendingSpeakId = null;
          _pendingSpeakLine = null;
        });
        _showHudNotice('// VOICE SYNTH OFFLINE — her words stand.');
      case 'klukai:herday':
        _openHerDaySheet();
      case 'klukai:auth-expired':
        _handleAuthExpired();
    }
  }

  /// Her line through the PWA's voice channel (/api/tts, then the
  /// gesture-unlocked player): the 3D fallback when the stage can't play.
  Future<void> _speakHere(ChatMessage line) async {
    if (_stageVoice != VoicePlayState.idle) return;
    _update(() => _stageVoice = VoicePlayState.loading);
    // An injected client is the caller's to close; one we open here is ours.
    final client = widget.httpClient ?? http.Client();
    var heard = false;
    try {
      for (final chunk in chunkForTts(line.content)) {
        if (!mounted) break;
        final audio = await TtsAudioRequest(
          client: client,
          baseUrl: widget.serverUrl,
          token: readAuthToken(),
        ).run(chunk);
        if (!mounted) break;
        _update(() => _stageVoice = VoicePlayState.playing);
        heard = await _browser.playVoice('data:audio/wav;base64,$audio');
        if (!heard) break;
      }
    } catch (e) {
      heard = false;
      debugPrint('Voice playback failed: $e');
    } finally {
      if (widget.httpClient == null) client.close();
    }
    if (!mounted) return;
    _update(() => _stageVoice = VoicePlayState.idle);
    if (!heard) _showHudNotice('// VOICE SYNTH OFFLINE — her words stand.');
  }
}

/// The reticle corner at the top-left of the 3D view.
class _Bracket extends StatelessWidget {
  const _Bracket();

  @override
  Widget build(BuildContext context) => Container(
    width: 26,
    height: 26,
    decoration: const BoxDecoration(
      border: Border(
        top: BorderSide(color: HudColors.cyan, width: 2),
        left: BorderSide(color: HudColors.cyan, width: 2),
      ),
    ),
  );
}
