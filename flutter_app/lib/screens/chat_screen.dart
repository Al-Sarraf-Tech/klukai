import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart' show mapEquals;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'dart:js_interop';
import 'package:web/web.dart' as web;
import '../main.dart';
import '../theme/mood_visuals.dart';
import '../models/message.dart';
import '../models/companion_state.dart';
import '../models/her_day.dart';
import '../platform/browser.dart';
import '../services/resume_policy.dart';
import '../services/session_auth.dart';
import '../services/speech_capture.dart';
import '../services/tts_request.dart';
import '../services/voice_settings_service.dart';
import '../services/wardrobe_service.dart';
import '../services/websocket_service.dart';
import '../widgets/chat_composer_bar.dart';
import '../widgets/companion_stage_view.dart';
import '../widgets/message_bubble.dart';
import '../widgets/message_footer.dart';
import '../widgets/stage_hud.dart';
import '../widgets/voice_button.dart';
import '../widgets/tool_status_indicator.dart';
import '../widgets/date_divider.dart';
import '../widgets/chat_header.dart';
import '../widgets/her_day_widgets.dart';
import '../theme/hud.dart';
import 'profile_screen.dart';
import 'her_pov_screen.dart';
import 'subscription_screen.dart';
import 'hud_album_screen.dart';
import 'hud_day_screen.dart';
import 'hud_kit_screen.dart';
import 'hud_me_screen.dart';
import 'thread_screen.dart';

part 'chat_wire_events.dart';

part 'chat_connection.dart';

part 'chat_composer.dart';

part 'chat_voice.dart';

part 'chat_starters.dart';

part 'chat_navigation.dart';

part 'chat_playback.dart';

part 'chat_view.dart';

part 'chat_stage.dart';

part 'chat_voice_language.dart';

@JS('audioRecorder.start')
external JSPromise<JSBoolean> _jsStartRecording();

@JS('audioRecorder.stop')
external JSPromise<JSString?> _jsStopRecording();

@JS('ambientAudio.setMood')
external void _jsSetAmbientMood(String mood);

@JS('ambientAudio.toggleMute')
external JSBoolean _jsToggleAmbientMute();

/// Unregister all service workers so login.html loads from server, not cache.
/// Awaitable so logout can finish the unregistration BEFORE navigating away
/// (navigating first let the old SW survive and serve a cached login page).
Future<void> _unregisterServiceWorker() async {
  try {
    final sw = web.window.navigator.serviceWorker;
    final regs = (await sw.getRegistrations().toDart).toDart;
    for (final reg in regs) {
      await reg.unregister().toDart;
    }
  } catch (_) {}
}

class ChatScreen extends StatefulWidget {
  final String serverUrl;

  /// WebSocket transport. Defaults to a real [WebSocketService] in production;
  /// tests may inject a fake so the screen can be pumped without a live backend.
  final WebSocketService? webSocketService;

  /// Today's Outfit / Her Day. Defaults to a real [WardrobeService]; tests
  /// inject a fake.
  final WardrobeService? wardrobeService;

  /// How often the header status line refreshes while the chat is visible.
  final Duration herDayRefresh;

  /// Browser seam (resume events, Companion window, voice). Defaults to the
  /// real browser; tests inject a fake.
  final BrowserPlatform? browser;

  /// When a resume should redial the socket.
  final ResumePolicy resumePolicy;

  /// Microphone bridge. Defaults to the page audioRecorder; tests inject a fake.
  final SpeechBridge? speechBridge;

  /// HTTP client for speech transcription and the 3D view's voice fallback.
  /// History keeps the shared client.
  final http.Client? httpClient;

  /// Optional deterministic history/affection boundary; caller owns the client.
  final http.Client? historyClient;

  /// Optional login navigation handler, after credentials and chat are revoked.
  final VoidCallback? onAuthExpired;

  /// Her voice language (EN | JP in the composer). Defaults to the real
  /// /api/voice/settings client; tests inject one over a MockClient.
  final VoiceSettingsService? voiceSettings;

  /// Builds the embedded 3D stage. Defaults to the companion page in an
  /// iframe; tests inject a stand-in.
  final CompanionStageBuilder? stageBuilder;

  /// Push-to-talk in the composer. Off: voice input is switched off on the
  /// server (KLUKAI_VOICE_ENABLED), so the mic button is not shown. The
  /// capture path stays wired for when it comes back.
  final bool voiceInputEnabled;

  const ChatScreen({
    super.key,
    required this.serverUrl,
    this.webSocketService,
    this.wardrobeService,
    this.herDayRefresh = const Duration(minutes: 10),
    this.browser,
    this.resumePolicy = const ResumePolicy(),
    this.speechBridge,
    this.httpClient,
    this.historyClient,
    this.onAuthExpired,
    this.voiceSettings,
    this.stageBuilder,
    this.voiceInputEnabled = false,
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  // Tactical HUD shell (2026-10-19): five tabs; Chat stays mounted so its
  // socket and state survive tab switches, the others build on first visit.
  HudTab _tab = HudTab.chat;
  final Set<HudTab> _visited = {HudTab.chat};
  // Bumped when Kit changes her outfit, so Her Day re-reads it.
  final ValueNotifier<int> _outfitChanged = ValueNotifier<int>(0);

  void _selectTab(HudTab tab) {
    if (tab == _tab) return;
    setState(() {
      if (_tab == HudTab.chat) _dropStage();
      _tab = tab;
      _visited.add(tab);
    });
    if (tab == HudTab.chat) {
      _loadHerDay(); // a wardrobe change elsewhere shows here
      _loadVoiceLanguage(); // so does a language change on the Me tab
      _scheduleStageWarmUp();
    }
  }

  Widget _tabBody(HudTab tab) {
    if (!_visited.contains(tab)) return const SizedBox.shrink();
    switch (tab) {
      case HudTab.chat:
        return _buildMobileLayout();
      case HudTab.album:
        return HudAlbumScreen(
          serverUrl: widget.serverUrl,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
          onAuthExpired: _handleAuthExpired,
        );
      case HudTab.day:
        return HudDayScreen(
          serverUrl: widget.serverUrl,
          wardrobeService: _wardrobe,
          refresh: _outfitChanged,
          onAuthExpired: _handleAuthExpired,
        );
      case HudTab.kit:
        return HudKitScreen(
          serverUrl: widget.serverUrl,
          wardrobeService: _wardrobe,
          onOutfitChanged: () {
            _outfitChanged.value++;
            _loadHerDay();
          },
          onAuthExpired: _handleAuthExpired,
        );
      case HudTab.me:
        return HudMeScreen(
          serverUrl: widget.serverUrl,
          affectionScore: _state.affectionScore,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
          onLogout: _logout,
          onAuthExpired: _handleAuthExpired,
        );
    }
  }

  late final WebSocketService _ws =
      widget.webSocketService ?? WebSocketService();
  late final WardrobeService _wardrobe =
      widget.wardrobeService ?? WardrobeService(serverUrl: widget.serverUrl);
  late final VoiceSettingsService _voiceSettings =
      widget.voiceSettings ?? VoiceSettingsService(serverUrl: widget.serverUrl);

  /// The Chat tab's face: transcript or her 3D stage (the CHAT | 3D flip).
  ChatViewMode _view = ChatViewMode.chat;

  /// Her voice language; English until the server says otherwise.
  VoiceLanguage _voiceLanguage = VoiceLanguage.en;
  bool _voiceLanguageSaving = false;

  /// The embedded stage, while it is loaded (no setState: the iframe reports
  /// it during build and dispose).
  CompanionStageLink? _stageLink;

  /// The stage stays loaded under the CHAT face (see [_stageMounted]).
  bool _stageKept = false;

  /// Its URL, fixed when it loads.
  String? _stageSrc;

  /// Loads it in the background a little after the transcript.
  Timer? _stageWarmTimer;
  bool _stageReady = false;
  String? _stageNotice;
  VoicePlayState _stageVoice = VoicePlayState.idle;
  int _speakSeq = 0;
  int? _pendingSpeakId;
  ChatMessage? _pendingSpeakLine;
  Timer? _stageSpeakTimer;

  /// The `klukai:hud` snapshot the stage last got (null: it has none yet).
  Map<String, Object?>? _sentHud;
  bool _hudSyncScheduled = false;

  /// Voice letters with a recorded note: message id → voice id. The note
  /// plays from that message's `▶ VOICE`, never by itself.
  final Map<String, String> _voiceNotes = {};

  /// Where she is and what she's wearing. Null hides the header line; a
  /// failed fetch must never get in the way of chat.
  HerDay? _herDay;
  Timer? _herDayTimer;

  late final BrowserPlatform _browser =
      widget.browser ?? defaultBrowserPlatform();
  StreamSubscription<void>? _resumeSub;

  /// When the page left the screen (tab hidden, app switched, device locked);
  /// null while it is showing. Set by the lifecycle observer, consumed by
  /// [_onPageResume]: together they measure how long she was actually away,
  /// which is what decides whether the socket can still be trusted.
  DateTime? _hiddenAt;
  final _textController = TextEditingController();
  final _scrollController = ScrollController();
  final _focusNode = FocusNode();

  final List<ChatMessage> _messages = [];
  CompanionState _state = const CompanionState();
  String? _authToken;
  bool _authExpired = false;
  String _streamingBuffer = '';
  String? _streamingId;
  final SpeechCapture _speech = SpeechCapture();
  int? _lastAffectionDelta;
  String? _thinkingText;
  final List<Map<String, String>> _activeTools = [];
  final bool _soundMuted = false;
  bool _ambientMuted = true;

  bool _showScrollFAB = false;

  /// He is reading the newest line. While true, a layout change that moves
  /// the end of the transcript (the header growing once her day loads, a
  /// bubble laid out taller than its estimate) keeps the newest line in view.
  bool _pinnedToBottom = true;
  int? _heartbeatSpikeOverride; // Temporary BPM override from heartbeat_spike
  Timer? _spikeDecayTimer;
  Timer? _inputLockTimer; // Safety timeout to auto-unlock input
  Timer?
  _warmupTimer; // Phased "waking up" status while a cold model loads + warms

  /// Glow color for the current mood (shared source of truth: [kMoodVisuals]).
  Color get _moodGlowColor => moodVisualFor(_state.mood).glow;

  /// Heartbeat BPM mapped to mood — reflects Klukai's emotional/physical state
  /// (shared source of truth: [kMoodVisuals]). A heartbeat_spike event
  /// temporarily overrides this with a higher BPM.
  int get _moodBPM {
    if (_heartbeatSpikeOverride != null) return _heartbeatSpikeOverride!;
    return moodVisualFor(_state.mood).bpm;
  }

  /// Lock input with a reason string and auto-unlock safety timeout.
  void _lockInput(
    String reason, {
    Duration timeout = const Duration(seconds: 30),
  }) {
    _inputLockTimer?.cancel();
    setState(() {
      _state = _state.copyWith(isInputLocked: true, inputLockReason: reason);
    });
    _inputLockTimer = Timer(timeout, () {
      if (mounted) _unlockInput();
    });
  }

  /// Unlock input and cancel any pending safety timer.
  void _unlockInput() {
    _inputLockTimer?.cancel();
    if (!mounted) return;
    setState(() {
      _state = _state.copyWith(isInputLocked: false, inputLockReason: null);
    });
  }

  /// Whether input should be disabled (locked OR disconnected).
  bool get _inputDisabled => !_state.isConnected || _state.isInputLocked;

  // All mutations run through this State; parts only group responsibilities.
  void _update(VoidCallback change) => setState(change);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _scrollController.addListener(() {
      final show =
          _scrollController.hasClients &&
          _scrollController.position.maxScrollExtent -
                  _scrollController.position.pixels >
              300;
      if (show != _showScrollFAB) setState(() => _showScrollFAB = show);
    });
    _view = _prefersOpenIn3d() ? ChatViewMode.threeD : ChatViewMode.chat;
    _loadHistory();
    _loadAffection();
    _connectWS();
    _loadHerDay();
    _loadVoiceLanguage();
    _herDayTimer = Timer.periodic(
      widget.herDayRefresh,
      (_) => _refreshHerDayIfVisible(),
    );
    _resumeSub = _browser.onResume.listen((_) => _onPageResume());
  }

  /// Back from the background (iOS app switch, tab return, bfcache restore,
  /// network back). Redial a dead or silent socket at once instead of waiting
  /// for the backoff, and refresh where she is.
  void _onPageResume() {
    if (!mounted || _authExpired) return;
    final hiddenAt = _hiddenAt;
    _hiddenAt = null;
    // How long the page was away — not how long the socket has been quiet. A
    // one-second tab switch after a quiet stretch of chat must leave a healthy
    // socket alone: the server cancels in-flight work (an image, say) when
    // the old socket closes.
    final awayFor = hiddenAt == null
        ? Duration.zero
        : DateTime.now().difference(hiddenAt);
    if (widget.resumePolicy.shouldReconnect(
      connected: _ws.isConnected,
      sinceLastFrame: awayFor,
    )) {
      _ws.reconnectNow(); // the connection listener reloads history on success
    }
    _loadHerDay();
  }

  /// The web engine reports `hidden` when the tab or app leaves the screen
  /// and `resumed` when it is back; a bare `inactive` is only the window
  /// losing focus while still visible — no reason to doubt the socket. The
  /// visibility change that brings `resumed` also fires the browser seam's
  /// onResume, where [_onPageResume] consumes the timestamp.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
      case AppLifecycleState.detached:
        _hiddenAt ??= DateTime.now();
      case AppLifecycleState.resumed:
      case AppLifecycleState.inactive:
        break;
    }
  }

  /// The Me tab's "open in 3D" setting, written either straight to
  /// localStorage or through shared_preferences (which prefixes `flutter.`).
  bool _prefersOpenIn3d() {
    try {
      final storage = web.window.localStorage;
      return storage.getItem(kOpenIn3dPrefKey) == 'true' ||
          storage.getItem('flutter.$kOpenIn3dPrefKey') == 'true';
    } catch (_) {
      return false;
    }
  }

  /// Opens the Companion page: a new window on desktop, the same window in an
  /// iOS home-screen app (companion.html links back). Relative to the base href.
  void _openCompanion() {
    _browser.openUrl(
      'companion.html',
      newWindow: companionOpensInNewWindow(_browser),
    );
  }

  Future<void> _loadHerDay() async {
    try {
      final day = await _wardrobe.fetchHerDay();
      if (!mounted) return;
      setState(() => _herDay = day.hasStatusLine ? day : null);
    } catch (_) {
      if (mounted) setState(() => _herDay = null);
    }
  }

  /// Periodic refresh, skipped while she's off screen (backgrounded tab, or
  /// another screen pushed on top). Reconnect and returning from the dossier
  /// reload it anyway.
  void _refreshHerDayIfVisible() {
    if (!mounted) return;
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    if (lifecycle != null && lifecycle != AppLifecycleState.resumed) return;
    if (!(ModalRoute.of(context)?.isCurrent ?? true)) return;
    _loadHerDay();
  }

  void _openHerDaySheet() {
    final day = _herDay;
    if (day == null) return;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: GFL2Colors.surface,
      isScrollControlled: true,
      builder: (sheetContext) => HerDaySheet(
        day: day,
        onOpenDossier: () {
          Navigator.pop(sheetContext);
          _openProfile();
        },
      ),
    );
  }

  @override
  void didChangeMetrics() {
    if (_isNearBottom()) {
      _scrollToBottom();
    }
  }

  bool _isNearBottom() {
    if (!_scrollController.hasClients) return true;
    final pos = _scrollController.position;
    return pos.maxScrollExtent - pos.pixels < 150;
  }

  void _scrollToBottom({bool instant = false, int retries = 0}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        final target = _scrollController.position.maxScrollExtent;
        if (instant) {
          _scrollController.jumpTo(target);
        } else {
          _scrollController.animateTo(
            target,
            duration: const Duration(milliseconds: 300),
            curve: Curves.easeOutCubic,
          );
        }
        // Images decode asynchronously — re-scroll to catch layout shifts
        if (retries > 0) {
          Future.delayed(const Duration(milliseconds: 150), () {
            _scrollToBottom(instant: true, retries: retries - 1);
          });
        }
      }
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _spikeDecayTimer?.cancel();
    _inputLockTimer?.cancel();
    _warmupTimer?.cancel();
    _herDayTimer?.cancel();
    _stageSpeakTimer?.cancel();
    _stageWarmTimer?.cancel();
    _resumeSub?.cancel();
    _speech.dispose();
    _outfitChanged.dispose();
    _ws.dispose();
    _textController.dispose();
    _scrollController.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: HudColors.ground,
      body: SafeArea(
        bottom: false,
        child: IndexedStack(
          index: _tab.index,
          children: [for (final tab in HudTab.values) _tabBody(tab)],
        ),
      ),
      bottomNavigationBar: HudTabBar(current: _tab, onSelect: _selectTab),
    );
  }
}
