import 'dart:async';
import 'dart:convert';
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
import '../services/speech_capture.dart';
import '../services/wardrobe_service.dart';
import '../services/websocket_service.dart';
import '../widgets/message_bubble.dart';
import '../widgets/voice_button.dart';
import '../widgets/tool_status_indicator.dart';
import '../widgets/date_divider.dart';
import '../widgets/chat_header.dart';
import '../widgets/her_day_widgets.dart';
import 'profile_screen.dart';
import 'memory_archive_screen.dart';
import 'her_pov_screen.dart';
import 'subscription_screen.dart';

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

  /// HTTP client for speech transcription. History keeps the shared client.
  final http.Client? httpClient;

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
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  late final WebSocketService _ws =
      widget.webSocketService ?? WebSocketService();
  late final WardrobeService _wardrobe =
      widget.wardrobeService ?? WardrobeService(serverUrl: widget.serverUrl);

  /// Where she is and what she's wearing. Null hides the header line; a
  /// failed fetch must never get in the way of chat.
  HerDay? _herDay;
  Timer? _herDayTimer;

  late final BrowserPlatform _browser = widget.browser ?? defaultBrowserPlatform();
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
  String _streamingBuffer = '';
  String? _streamingId;
  final SpeechCapture _speech = SpeechCapture();
  int? _lastAffectionDelta;
  String? _thinkingText;
  final List<Map<String, String>> _activeTools = [];
  final bool _soundMuted = false;
  bool _ambientMuted = true;

  bool _showScrollFAB = false;
  int? _heartbeatSpikeOverride; // Temporary BPM override from heartbeat_spike
  Timer? _spikeDecayTimer;
  Timer? _inputLockTimer; // Safety timeout to auto-unlock input
  Timer?
  _warmupTimer; // Phased "waking up" status while a cold model loads + warms

  bool get _isDormMode {
    final hour = DateTime.now().hour;
    return hour >= 21 && _state.affectionLevel >= 2;
  }

  Color get _bgColor =>
      _isDormMode ? const Color(0xFF16131E) : GFL2Colors.background;

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
    _loadHistory();
    _loadAffection();
    _connectWS();
    _loadHerDay();
    _herDayTimer = Timer.periodic(widget.herDayRefresh, (_) => _refreshHerDayIfVisible());
    _resumeSub = _browser.onResume.listen((_) => _onPageResume());
  }

  /// Back from the background (iOS app switch, tab return, bfcache restore,
  /// network back). Redial a dead or silent socket at once instead of waiting
  /// for the backoff, and refresh where she is.
  void _onPageResume() {
    if (!mounted) return;
    final hiddenAt = _hiddenAt;
    _hiddenAt = null;
    // How long the page was away — not how long the socket has been quiet. A
    // one-second tab switch after a quiet stretch of chat must leave a healthy
    // socket alone: the server cancels in-flight work (an image, say) when
    // the old socket closes.
    final awayFor =
        hiddenAt == null ? Duration.zero : DateTime.now().difference(hiddenAt);
    if (widget.resumePolicy.shouldReconnect(connected: _ws.isConnected, sinceLastFrame: awayFor)) {
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

  /// Opens the Companion page: a new window on desktop, the same window in an
  /// iOS home-screen app (companion.html links back). Relative to the base href.
  void _openCompanion() {
    _browser.openUrl('companion.html', newWindow: companionOpensInNewWindow(_browser));
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

  Map<String, String> get _authHeaders {
    final token = _authToken ?? _getToken() ?? '';
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  Future<void> _loadHistory() async {
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/messages?limit=50');
      final response = await http.get(uri, headers: _authHeaders);
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final messages = (data['messages'] as List)
            .map((m) => ChatMessage.fromJson(m))
            .toList();
        if (!mounted) return;
        _mergeHistory(messages);
        _scrollToBottom(instant: true);
      } else if (response.statusCode == 401) {
        // Expired token. Don't wait for the WebSocket's 4001 close — under
        // uvicorn that close never reaches the client, and the app would sit
        // soft-locked on LINK DOWN with an empty transcript. Back to login.
        if (mounted) _handleAuthExpired();
      } else if (mounted) {
        // Don't render an empty conversation when the server actually errored —
        // history is sacred; offer a retry so it can't be mistaken for "wiped".
        _showHistoryError();
      }
    } catch (e) {
      debugPrint('Failed to load history: $e');
      if (mounted) _showHistoryError();
    }
  }

  /// Merge fetched history into [_messages] by id — idempotent, so the
  /// initial load, a RETRY, and a reconnect refetch can all share it without
  /// ever duplicating a message. Locally-echoed messages (synthetic ids like
  /// `user-…`/`streaming-…`) are matched by role+content so the server copy
  /// of a message we already rendered isn't shown twice. New messages are
  /// inserted in timestamp order, preserving the live conversation flow.
  void _mergeHistory(List<ChatMessage> fetched) {
    bool isLocalEchoOf(ChatMessage m) => _messages.any(
      (x) =>
          x.id != m.id &&
          x.role == m.role &&
          x.content == m.content &&
          (x.id.startsWith('user-') ||
              x.id.startsWith('streaming-') ||
              x.id.startsWith('proactive-') ||
              x.id.startsWith('image-')),
    );
    final known = _messages.map((m) => m.id).toSet();
    final additions =
        fetched
            .where((m) => !known.contains(m.id) && !isLocalEchoOf(m))
            .toList()
          ..sort((a, b) => a.createdAt.compareTo(b.createdAt));
    if (additions.isEmpty) return;
    setState(() {
      for (final m in additions) {
        var idx = _messages.length;
        while (idx > 0 && _messages[idx - 1].createdAt.isAfter(m.createdAt)) {
          idx--;
        }
        _messages.insert(idx, m);
      }
    });
  }

  void _showHistoryError() {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text(
          "COULDN'T LOAD HISTORY",
          style: TextStyle(
            fontFamily: 'monospace',
            letterSpacing: 1.0,
            fontSize: 12,
          ),
        ),
        backgroundColor: GFL2Colors.surface,
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 6),
        action: SnackBarAction(
          label: 'RETRY',
          textColor: GFL2Colors.primary,
          onPressed: _loadHistory,
        ),
      ),
    );
  }

  Future<void> _loadAffection() async {
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/affection');
      final response = await http.get(uri, headers: _authHeaders);
      if (!mounted) return;
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        setState(() {
          _state = _state.copyWith(
            affectionScore: data['score'] as int? ?? 0,
            affectionLevel: data['level'] as int? ?? 0,
            affectionLevelName:
                data['level_name'] as String? ?? 'Cold Assessment',
          );
        });
      } else if (response.statusCode == 401) {
        _handleAuthExpired(); // same soft-lock as _loadHistory; see there
      }
    } catch (e) {
      debugPrint('Failed to load affection: $e');
    }
  }

  String? _authToken;

  String? _getToken() {
    try {
      return web.window.localStorage.getItem('klukai_token');
    } catch (_) {
      return null;
    }
  }

  void _connectWS() {
    _authToken = _getToken();
    if (_authToken == null || _authToken!.isEmpty) {
      // No token — redirect to login page
      web.window.location.href = '/';
      return;
    }
    final wsUrl = '${widget.serverUrl.replaceFirst('http', 'ws')}/ws';
    _ws.connect(wsUrl, token: _authToken);
    _ws.connectionState.listen((connected) {
      if (!mounted) return;
      final wasConnected = _state.isConnected;
      setState(() {
        _state = _state.copyWith(isConnected: connected);
        // The link died (or was redialed) mid-reply: no more tokens are coming
        // for that bubble. Freeze it where it stands so the next reply gets a
        // bubble of its own instead of being appended to a dead one, and free
        // the composer instead of leaving it locked for the 60s safety timer.
        if (!connected) _finalizeStreamingBubble();
      });
      if (!connected) {
        _clearWarmupHint();
        _unlockInput();
      }
      if (connected && !wasConnected) {
        // (Re)connected — refetch recent history and merge by id so anything
        // missed while the link was down appears, without duplicates.
        _loadHistory();
        _loadHerDay();
      }
    });
    _ws.messages.listen(_handleWSMessage);
    _ws.authFailure.listen((_) => _handleAuthExpired());
  }

  void _handleAuthExpired() {
    // Server rejected the token (expired/invalid). Don't loop reconnecting —
    // clear the stale token and return to login.
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    web.window.location.href = '/';
  }

  void _handleWSMessage(Map<String, dynamic> msg) {
    final type = msg['type'] as String?;

    switch (type) {
      case 'token':
        final text = msg['text'] as String? ?? '';
        _clearWarmupHint(); // she's replying — drop any "waking up" notice
        // The first token always (re)arms the lock for the whole reply. A
        // shorter lock may be active (proactive 1.5s, image 2s, heartbeat 5s);
        // left alone it would lapse mid-reply, freeing the composer while she
        // is still talking. Later tokens only re-arm if the lock has lapsed.
        if (_streamingId == null || !_state.isInputLocked) {
          _lockInput(
            'RECEIVING TRANSMISSION',
            timeout: const Duration(seconds: 60),
          );
        }
        setState(() {
          _streamingBuffer += text;
          if (_streamingId == null) {
            _thinkingText = null;
            _streamingId = 'streaming-${DateTime.now().millisecondsSinceEpoch}';
            _messages.add(
              ChatMessage(
                id: _streamingId!,
                role: 'assistant',
                content: _streamingBuffer,
                isStreaming: true,
              ),
            );
          } else {
            final idx = _messages.indexWhere((m) => m.id == _streamingId);
            if (idx >= 0) {
              _messages[idx] = _messages[idx].copyWith(
                content: _streamingBuffer,
              );
            }
          }
        });
        _scrollToBottom(instant: true);

      case 'done':
        final model = msg['model'] as String?;
        // done.text is her reply after the server's narration fix-ups (e.g.
        // <think> blocks stripped). Showing it instead of the raw stream also
        // keeps the next history merge from adding a second copy of the line.
        final finalText = msg['text'];
        setState(() {
          _finalizeStreamingBubble(
            content: finalText is String && finalText.isNotEmpty
                ? finalText
                : null,
            model: model,
          );
          _state = _state.copyWith(currentModel: model);
        });
        _clearWarmupHint();
        _unlockInput();
        _playNotificationSound();

      case 'read_receipt':
        // Read receipts handled — messages default to 'read' status
        break;

      case 'mood':
        setState(() {
          _state = _state.copyWith(mood: msg['mood'] as String? ?? 'composed');
        });
        try {
          _jsSetAmbientMood(msg['mood'] as String? ?? 'composed');
        } catch (_) {}

      case 'thinking':
        _clearWarmupHint(); // a real status arrived; don't overwrite it later
        // Older frames carry `text`, current ones `content`. An empty frame is
        // the backend clearing its status line — it must take the spinner
        // down, not leave a permanent PROCESSING behind.
        final status = msg['text'] as String? ?? msg['content'] as String?;
        setState(() {
          if (status == null || status.isEmpty) {
            _state = _state.copyWith(isTyping: false);
            _thinkingText = null;
          } else {
            _state = _state.copyWith(isTyping: true);
            _thinkingText = status;
          }
        });

      case 'tool_use':
        final toolName = msg['tool'] as String? ?? 'unknown';
        final toolStatus = msg['status'] as String? ?? 'calling';
        setState(() {
          if (toolStatus == 'calling') {
            _activeTools.add({'tool': toolName, 'status': 'calling'});
          } else {
            for (var i = _activeTools.length - 1; i >= 0; i--) {
              if (_activeTools[i]['tool'] == toolName &&
                  _activeTools[i]['status'] == 'calling') {
                _activeTools[i] = {'tool': toolName, 'status': 'done'};
                break;
              }
            }
          }
        });

      case 'affection':
        setState(() {
          _lastAffectionDelta = msg['delta'] as int? ?? 0;
          _state = _state.copyWith(
            affectionScore: msg['score'] as int? ?? _state.affectionScore,
            affectionLevel: msg['level'] as int? ?? _state.affectionLevel,
            affectionLevelName:
                msg['level_name'] as String? ?? _state.affectionLevelName,
          );
        });

      case 'affection_level_change':
        final levelName = msg['level_name'] as String? ?? '';
        final direction = msg['direction'] as String? ?? 'up';
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Row(
                children: [
                  Icon(
                    direction == 'up'
                        ? Icons.arrow_upward
                        : Icons.arrow_downward,
                    color: direction == 'up'
                        ? GFL2Colors.success
                        : GFL2Colors.danger,
                    size: 16,
                  ),
                  const SizedBox(width: 8),
                  Text(
                    'TRUST LEVEL: $levelName',
                    style: const TextStyle(
                      color: GFL2Colors.textPrimary,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.8,
                      fontFamily: 'monospace',
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
              backgroundColor: GFL2Colors.surface,
              behavior: SnackBarBehavior.floating,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(4),
                side: BorderSide(
                  color: direction == 'up'
                      ? GFL2Colors.primary.withValues(alpha: 0.4)
                      : GFL2Colors.danger.withValues(alpha: 0.4),
                ),
              ),
              duration: const Duration(seconds: 4),
            ),
          );
        }

      case 'proactive':
        final message = msg['message'] as String? ?? '';
        _lockInput(
          'INCOMING TRANSMISSION',
          timeout: const Duration(milliseconds: 1500),
        );
        setState(() {
          _messages.add(
            ChatMessage(
              id: 'proactive-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: message,
            ),
          );
        });
        _scrollToBottom();
        _playNotificationSound();
        // Auto-unlock after brief pause
        Timer(const Duration(milliseconds: 1500), () {
          if (mounted && _state.inputLockReason == 'INCOMING TRANSMISSION') {
            _unlockInput();
          }
        });

      case 'voice_audio':
        final audioData = msg['audio'] as String?;
        if (audioData != null) {
          _playAudio(audioData);
        }

      case 'voice_letter':
        // A voice note she left while you were away. Show the text like a
        // proactive line, then fetch + play the audio (fail-soft: text stands
        // on its own if the audio can't be fetched).
        final letter = msg['message'] as String? ?? '';
        final voiceId = msg['voice_id'] as String?;
        if (letter.isNotEmpty) {
          setState(() {
            _messages.add(
              ChatMessage(
                id: 'voice-letter-${DateTime.now().millisecondsSinceEpoch}',
                role: 'assistant',
                content: letter,
              ),
            );
          });
          _scrollToBottom();
          _playNotificationSound();
        }
        if (voiceId != null && voiceId.isNotEmpty) {
          _fetchAndPlayVoiceNote(voiceId);
        }

      case 'image':
        final imgData = msg['data'] as String?;
        if (imgData != null) {
          _lockInput('IMAGE INCOMING', timeout: const Duration(seconds: 2));
          setState(() {
            _messages.add(
              ChatMessage(
                id: 'image-${DateTime.now().millisecondsSinceEpoch}',
                role: 'assistant',
                content: '[IMAGE]',
                imageData: imgData,
              ),
            );
          });
          // Retry scroll 3 times to catch image decode layout shifts
          _scrollToBottom(retries: 3);
          _playNotificationSound();
          Timer(const Duration(seconds: 2), () {
            if (mounted && _state.inputLockReason == 'IMAGE INCOMING') {
              _unlockInput();
            }
          });
        }

      case 'outfit':
        // She changed because he asked in chat. Show it now, then refetch so
        // the status line and schedule agree with the server.
        final raw = msg['outfit'];
        if (raw is Map<String, dynamic> && _herDay != null) {
          setState(() => _herDay = _herDay!.withOutfit(OutfitInfo.fromJson(raw)));
        }
        _loadHerDay();

      case 'heartbeat_spike':
        final spikeBpm = msg['bpm'] as int? ?? 160;
        _lockInput('HEARTBEAT SURGE', timeout: const Duration(seconds: 5));
        setState(() {
          _heartbeatSpikeOverride = spikeBpm;
        });
        // Decay back to normal BPM after 5 seconds
        _spikeDecayTimer?.cancel();
        _spikeDecayTimer = Timer(const Duration(seconds: 5), () {
          if (mounted) {
            setState(() {
              _heartbeatSpikeOverride = null;
            });
            if (_state.inputLockReason == 'HEARTBEAT SURGE') _unlockInput();
          }
        });

      case 'error':
        // Backend hit a snag (handler crash / stream failure). Clear all the
        // in-flight indicators — including the warmup hint — so the UI never
        // sticks on "WAKING UP", and show her message as a normal bubble. A
        // reply cut off mid-stream keeps what arrived, frozen: no cursor
        // blinking forever on a bubble nothing will ever finish.
        _clearWarmupHint();
        final errText =
            msg['message'] as String? ??
            '...Something glitched on my end, Commander. Try again.';
        setState(() {
          _finalizeStreamingBubble();
          _messages.add(
            ChatMessage(
              id: 'error-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: errText,
            ),
          );
        });
        _unlockInput();
        _scrollToBottom();
    }
  }

  /// Ends the in-progress reply where it stands: the streaming bubble (if any)
  /// stops blinking and keeps what streamed — or takes [content], the server's
  /// final text — and every per-reply indicator (buffer, status line, tool
  /// list, typing) is reset. Shared by `done`, `error` and a dropped link.
  /// Must be called inside setState. Returns the finalized bubble's index so
  /// the caller can prepare its layout, or null when nothing was streaming.
  int? _finalizeStreamingBubble({String? content, String? model}) {
    int? idx;
    final id = _streamingId;
    if (id != null) {
      final i = _messages.indexWhere((m) => m.id == id);
      if (i >= 0) {
        _messages[i] = _messages[i].copyWith(
          isStreaming: false,
          content: content,
          model: model,
        );
        idx = i;
      }
    }
    _streamingBuffer = '';
    _streamingId = null;
    _thinkingText = null;
    _activeTools.clear();
    _state = _state.copyWith(isTyping: false);
    return idx;
  }

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
    setState(() {
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
      setState(() {
        _state = _state.copyWith(isTyping: true);
        _thinkingText = 'WAKING HER UP // loading the model from cold';
      });
      _warmupTimer = Timer(const Duration(seconds: 15), () {
        if (!mounted || _streamingId != null) return;
        setState(() {
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

  /// Whisper on a cold GPU takes a while; a powered-off GPU host hangs the
  /// proxy until it gives up. Bound the wait so he isn't left wondering.
  static const _sttTimeout = Duration(seconds: 35);

  Future<bool> _startRecorder() async {
    final bridge = widget.speechBridge;
    if (bridge != null) return bridge.start();
    return (await _jsStartRecording().toDart).toDart;
  }

  Future<String?> _stopRecorder() async {
    final bridge = widget.speechBridge;
    if (bridge != null) return bridge.stop();
    final audio = await _jsStopRecording().toDart;
    return audio?.toDart;
  }

  Future<void> _onVoiceDown() async {
    final epoch = _speech.begin();
    if (epoch == null) return;
    if (mounted) setState(() {});
    var started = false;
    try {
      started = await _startRecorder();
    } catch (_) {
      started = false;
    }
    if (!mounted || !_speech.isCurrent(epoch)) return;
    if (!started) {
      setState(() => _speech.fail(epoch, 'permission'));
      _showVoiceLinkError();
      return;
    }
    setState(() => _speech.markRecording(epoch));
  }

  Future<void> _onVoiceUp() async {
    final epoch = _speech.epoch;
    if (_speech.phase == SpeechPhase.requestingPermission) {
      _speech.cancel();
      if (mounted) setState(() {});
      try {
        await _stopRecorder();
      } catch (_) {}
      return;
    }
    if (_speech.phase != SpeechPhase.recording) return;
    setState(() => _speech.markTranscribing(epoch));
    String? audio;
    try {
      audio = await _stopRecorder();
    } catch (_) {
      audio = null;
    }
    if (!mounted || !_speech.isCurrent(epoch)) return;
    if (audio == null || audio.isEmpty) {
      setState(() => _speech.fail(epoch, 'empty'));
      _showVoiceLinkError();
      return;
    }
    await _transcribeAndSend(audio, epoch);
  }

  Future<void> _cancelVoice() async {
    if (_speech.phase == SpeechPhase.idle) return;
    _speech.cancel();
    if (mounted) setState(() {});
    try {
      await _stopRecorder();
    } catch (_) {}
  }

  Future<http.Response> _postStt(String audioBase64) {
    final uri = Uri.parse('${widget.serverUrl}/api/stt');
    final body = jsonEncode({'audio': audioBase64});
    final client = widget.httpClient;
    if (client != null) {
      return client.post(uri, headers: _authHeaders, body: body);
    }
    return http.post(uri, headers: _authHeaders, body: body);
  }

  Future<void> _transcribeAndSend(String audioBase64, int epoch) async {
    var text = '';
    try {
      final response = await _postStt(audioBase64).timeout(_sttTimeout);
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        text = (data['text'] as String? ?? '').trim();
      } else {
        debugPrint('STT failed: HTTP ${response.statusCode}');
      }
    } catch (e) {
      debugPrint('STT failed: $e');
    }
    if (!mounted || !_speech.isCurrent(epoch)) return;
    final spoken = SpeechCapture.voiceUtterance(text);
    final kept = SpeechCapture.draftAfterTranscript(_textController.text, spoken);
    if (kept != _textController.text) _textController.text = kept;
    // GPU host off (502), request timed out, or nothing intelligible came
    // back: say so instead of swallowing his words. The typed draft stays.
    if (spoken.isEmpty) {
      setState(() => _speech.fail(epoch, 'stt'));
      _showVoiceLinkError();
      return;
    }
    if (!_ws.sendMessage(spoken)) {
      setState(() => _speech.fail(epoch, 'send'));
      _showSendError();
      return;
    }
    setState(() {
      _speech.succeed(epoch);
      _messages.add(
        ChatMessage(
          id: 'user-${DateTime.now().millisecondsSinceEpoch}',
          role: 'user',
          content: spoken,
        ),
      );
    });
    _scrollToBottom();
    _startWarmupHint(); // same cold-model wait as a typed message
  }

  List<String> _getStarters() {
    final level = _state.affectionLevel;
    final hour = DateTime.now().hour;
    final isMorning = hour >= 6 && hour < 12;
    final isEvening = hour >= 18 || hour < 6;

    if (level <= 2) {
      return [
        'Mission briefing',
        'Status report',
        if (isMorning) "What's the plan today?" else "How's the squad?",
        'Tell me about yourself',
      ];
    } else if (level <= 4) {
      return [
        if (isMorning)
          'Good morning'
        else if (isEvening)
          'Good evening'
        else
          "How's your day?",
        'Tell me about Belka',
        "How's the squad?",
        "Let's go for a ride",
      ];
    } else if (level <= 6) {
      return [
        if (isMorning)
          'Good morning, Klukai'
        else if (isEvening)
          "Can't sleep?"
        else
          'I was thinking about you',
        'Tell me about Mechty',
        "What's on your mind?",
        "I missed you",
      ];
    } else {
      return [
        if (isMorning)
          'Good morning, beautiful'
        else if (isEvening)
          'Come sit with me'
        else
          'I love you',
        "What are you thinking about?",
        "Tell me a memory",
        "I'm here",
      ];
    }
  }

  Widget _starterChip(String text) {
    return GestureDetector(
      onTap: () {
        _textController.text = text;
        _sendMessage();
      },
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: GFL2Colors.surface,
          borderRadius: BorderRadius.circular(4),
          border: Border.all(color: GFL2Colors.primary.withValues(alpha: 0.3)),
        ),
        child: Text(
          text,
          style: TextStyle(
            color: GFL2Colors.primary.withValues(alpha: 0.7),
            fontSize: 12,
            fontFamily: 'monospace',
          ),
        ),
      ),
    );
  }

  void _playNotificationSound() {
    if (_soundMuted) return;
    try {
      final audio = web.HTMLAudioElement()
        ..src = 'audio/comm_beep.wav'
        ..volume = 0.3;
      // play() rejects without a prior gesture (iOS, Safari autoplay rules);
      // a missed beep is fine, an unhandled rejection is console noise.
      audio.play().toDart.catchError((Object _) => null);
    } catch (_) {}
  }

  void _openProfile() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ProfileScreen(
          serverUrl: widget.serverUrl,
          affectionScore: _state.affectionScore,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
          wardrobeService: _wardrobe,
          onAuthExpired: _handleAuthExpired,
        ),
      ),
    ).then((_) => _loadHerDay()); // a wardrobe change in the dossier shows here
  }

  Future<void> _logout() async {
    _ws.dispose();
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    // Unregister the Flutter service worker BEFORE navigating, so login.html
    // is fetched from the server instead of being served by a stale SW cache.
    await _unregisterServiceWorker();
    web.window.location.href = '/';
  }

  void _openHerPov() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => HerPovScreen(
          serverUrl: widget.serverUrl,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
        ),
      ),
    );
  }

  void _openArchive() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => MemoryArchiveScreen(
          serverUrl: widget.serverUrl,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
        ),
      ),
    );
  }

  void _openSubscription() {
    String? token;
    try {
      token = web.window.localStorage.getItem('klukai_token');
    } catch (_) {}
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) =>
            SubscriptionScreen(
              serverUrl: widget.serverUrl,
              authToken: token,
              onAuthExpired: _handleAuthExpired,
            ),
      ),
    );
  }

  /// Her voice goes through the shared, gesture-unlocked channel (web/js/
  /// voice.js): a fresh Audio element per WebSocket frame is refused on iOS,
  /// and two frames close together would otherwise talk over each other.
  void _playAudio(String base64Audio) {
    _browser.playVoice('data:audio/wav;base64,$base64Audio').then((ok) {
      if (!ok) debugPrint('Voice playback refused (no gesture yet?)');
    });
  }

  Future<void> _fetchAndPlayVoiceNote(String voiceId) async {
    // Image.network(headers:) is ignored on web, and the same applies to audio
    // src — so fetch the bytes with the bearer token and play them inline.
    try {
      final uri = Uri.parse(
        '${widget.serverUrl}/api/voice-notes/$voiceId/audio',
      );
      final resp = await http
          .get(uri, headers: _authHeaders)
          .timeout(const Duration(seconds: 20));
      if (resp.statusCode == 200 && resp.bodyBytes.isNotEmpty) {
        _playAudio(base64Encode(resp.bodyBytes));
      } else {
        debugPrint('Voice note fetch failed: HTTP ${resp.statusCode}');
      }
    } catch (e) {
      debugPrint('Voice note fetch failed: $e');
    }
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
    _resumeSub?.cancel();
    _speech.dispose();
    _ws.dispose();
    _textController.dispose();
    _scrollController.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bgColor,
      body: SafeArea(child: _buildMobileLayout()),
    );
  }

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
          setState(() => _ambientMuted = !isOn);
        } catch (_) {
          setState(() => _ambientMuted = !_ambientMuted);
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
            isRecording: _speech.phase == SpeechPhase.recording ||
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
