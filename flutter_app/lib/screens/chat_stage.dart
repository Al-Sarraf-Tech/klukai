part of 'chat_screen.dart';

/// The Chat tab's 3D face: her companion stage in an iframe, between a solid
/// top bar (title, CHAT | 3D, menu) and a solid composer strip, with nothing
/// Flutter draws over it. (On the web, Flutter content painted over a
/// platform view goes into overlay canvases, and iOS Safari flickers as they
/// repaint.) The stage draws her HUD itself — status, her line, `▶ VOICE`
/// (a tap inside the iframe is what lets iOS start its audio) — from the
/// `klukai:hud` snapshots sent here. Shares the screen's single State owner.
extension _ChatStage on _ChatScreenState {
  /// How long after her transcript arrives the stage starts loading behind
  /// the CHAT face: the transcript, its pictures and the socket go first.
  static const _stageWarmUpDelay = Duration(seconds: 3);

  /// One voice-error notice covers this long: the stage may report each
  /// failed sentence of a line.
  static const _voiceErrorQuiet = Duration(seconds: 4);

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
        // A menu or sheet open over her gets his taps, not the iframe.
        interactive: ModalRoute.isCurrentOf(context) ?? true,
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
    _stageKept = false;
    _stageSrc = null;
    _stageVoice = VoicePlayState.idle;
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

  /// The top bar's title in 3D: what the stage is doing.
  String get _stageTitle {
    final notice = _stageNotice?.trim() ?? '';
    final String doing;
    if (notice.isNotEmpty) {
      doing = notice.toUpperCase();
    } else if (!_stageReady) {
      doing = 'LINKING…';
    } else if (_stageVoice == VoicePlayState.playing) {
      doing = 'VOICE';
    } else {
      doing = 'LIVE';
    }
    return '3D LINK // $doing';
  }

  /// Above her stage: the same top bar as the CHAT face, in the same place,
  /// on solid ground.
  Widget _buildStageTopStrip() => DecoratedBox(
    key: const Key('chat-stage-top'),
    decoration: BoxDecoration(
      color: HudColors.ground,
      border: Border(bottom: BorderSide(color: HudColors.cyanA(0.25))),
    ),
    child: Padding(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 4),
      child: HudTopBar(
        title: _stageTitle,
        view: _view,
        onViewChanged: _setView,
        onMenu: _onMenu,
      ),
    ),
  );

  /// Under her stage: tool status while a tool runs, and the composer, on
  /// solid ground. With the keyboard up it stays; the stage gives way.
  Widget _buildStageBottomStrip() => LayoutBuilder(
    builder: (context, box) => DecoratedBox(
      key: const Key('chat-stage-bottom'),
      decoration: BoxDecoration(
        color: HudColors.ground,
        border: Border(top: BorderSide(color: HudColors.cyanA(0.25))),
      ),
      // Scrolls, rather than overflowing, only in a window too short for it.
      child: SingleChildScrollView(
        reverse: true,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (_activeTools.isNotEmpty)
              ConstrainedBox(
                constraints: BoxConstraints(maxHeight: box.maxHeight * 0.2),
                child: SingleChildScrollView(child: _buildToolStatus()),
              ),
            _buildComposer(),
          ],
        ),
      ),
    ),
  );

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
          _update(() => _stageVoice = VoicePlayState.playing);
        } else if (_stageVoice == VoicePlayState.playing) {
          _update(() => _stageVoice = VoicePlayState.idle);
        }
      case 'klukai:speak-status':
        // Her `▶ VOICE` in the stage could not start its audio: the line
        // plays through the PWA's own voice channel instead. Only a line of
        // hers, named by its id; anything else is ignored.
        if (data['status'] != 'unavailable') return;
        final id = data['id'];
        final line = _messages.where(
          (m) => m.role != 'user' && id != null && m.id == '$id',
        );
        if (line.isNotEmpty) _speakHere(line.last);
      case 'klukai:voice-error':
        if (_stageVoice == VoicePlayState.playing) {
          _update(() => _stageVoice = VoicePlayState.idle);
        }
        final now = DateTime.now();
        final last = _voiceErrorShownAt;
        if (last != null &&
            now.difference(last) < _ChatStage._voiceErrorQuiet) {
          return;
        }
        _voiceErrorShownAt = now;
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

/// The Chat tab's parts: the CHAT face, or the 3D strips; and her stage.
enum _ChatTabSlot { face, top, bottom, stage }

/// The strip heights her stage last had in 3D. Behind the CHAT face it keeps
/// that rect, so a flip shows her at once without resizing (and reframing)
/// her page.
class _StageFrame {
  double top = 54;
  double bottom = 60;
}

/// Lays out the Chat tab. CHAT: the face fills it. 3D: the top strip and
/// the composer strip take what they need, and her stage gets exactly the
/// rect between them, so nothing Flutter paints overlaps the iframe.
///
/// Paint order is child order, and the stage is always the last child: in
/// 3D everything Flutter draws comes before it (the engine needs no overlay
/// canvas), and under the CHAT face the parked stage is hidden anyway.
class _ChatTabLayout extends MultiChildLayoutDelegate {
  _ChatTabLayout(this.frame);

  final _StageFrame frame;

  @override
  void performLayout(Size size) {
    if (hasChild(_ChatTabSlot.face)) {
      layoutChild(_ChatTabSlot.face, BoxConstraints.tight(size));
      positionChild(_ChatTabSlot.face, Offset.zero);
    }
    final strip = BoxConstraints(
      minWidth: size.width,
      maxWidth: size.width,
      maxHeight: size.height,
    );
    if (hasChild(_ChatTabSlot.top)) {
      frame.top = layoutChild(_ChatTabSlot.top, strip).height;
      positionChild(_ChatTabSlot.top, Offset.zero);
    }
    if (hasChild(_ChatTabSlot.bottom)) {
      final room = (size.height - frame.top).clamp(0.0, size.height);
      frame.bottom = layoutChild(
        _ChatTabSlot.bottom,
        strip.copyWith(maxHeight: room),
      ).height;
      positionChild(_ChatTabSlot.bottom, Offset(0, size.height - frame.bottom));
    }
    if (hasChild(_ChatTabSlot.stage)) {
      final top = frame.top.clamp(0.0, size.height);
      final height = (size.height - top - frame.bottom).clamp(0.0, size.height);
      layoutChild(
        _ChatTabSlot.stage,
        BoxConstraints.tight(Size(size.width, height)),
      );
      positionChild(_ChatTabSlot.stage, Offset(0, top));
    }
  }

  /// A flip changes the children, which lays them out again by itself.
  @override
  bool shouldRelayout(_ChatTabLayout oldDelegate) => oldDelegate.frame != frame;
}
