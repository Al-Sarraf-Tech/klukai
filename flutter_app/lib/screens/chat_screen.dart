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
import '../services/websocket_service.dart';
import '../services/pretext_interop.dart';
import '../widgets/message_bubble.dart';
import '../widgets/mood_indicator.dart';
import '../widgets/voice_button.dart';
import '../widgets/affection_gauge.dart';
import '../widgets/tool_status_indicator.dart';
import '../widgets/canvas_message_bubble.dart';
import '../widgets/date_divider.dart';
import '../widgets/heartbeat_sensor.dart';
import '../widgets/exit_icon.dart';
import 'profile_screen.dart';
import 'memory_archive_screen.dart';
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

  const ChatScreen({super.key, required this.serverUrl, this.webSocketService});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  late final WebSocketService _ws =
      widget.webSocketService ?? WebSocketService();
  final _textController = TextEditingController();
  final _scrollController = ScrollController();
  final _focusNode = FocusNode();

  final List<ChatMessage> _messages = [];
  CompanionState _state = const CompanionState();
  String _streamingBuffer = '';
  String? _streamingId;
  bool _isRecording = false;
  int? _lastAffectionDelta;
  String? _thinkingText;
  final List<Map<String, String>> _activeTools = [];
  final bool _soundMuted = false;
  bool _ambientMuted = true;

  bool _showScrollFAB = false;
  int? _heartbeatSpikeOverride; // Temporary BPM override from heartbeat_spike
  Timer? _spikeDecayTimer;
  Timer? _inputLockTimer; // Safety timeout to auto-unlock input

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
      } else if (response.statusCode != 401 && mounted) {
        // Don't render an empty conversation when the server actually errored —
        // history is sacred; offer a retry so it can't be mistaken for "wiped".
        // (401 is handled by the auth-expiry redirect, not a retry.)
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
    final additions = fetched
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
    for (var i = 0; i < _messages.length; i++) {
      _prepareMessageLayout(i);
    }
  }

  void _showHistoryError() {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text("COULDN'T LOAD HISTORY",
            style: TextStyle(
                fontFamily: 'monospace', letterSpacing: 1.0, fontSize: 12)),
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
      setState(() => _state = _state.copyWith(isConnected: connected));
      if (connected && !wasConnected) {
        // (Re)connected — refetch recent history and merge by id so anything
        // missed while the link was down appears, without duplicates.
        _loadHistory();
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
        if (!_state.isInputLocked) {
          _lockInput(
            'RECEIVING TRANSMISSION',
            timeout: const Duration(seconds: 60),
          );
        }
        setState(() {
          _streamingBuffer += text;
          if (_streamingId == null) {
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
        int? completedIdx;
        setState(() {
          if (_streamingId != null) {
            completedIdx = _messages.indexWhere((m) => m.id == _streamingId);
            if (completedIdx! >= 0) {
              _messages[completedIdx!] = _messages[completedIdx!].copyWith(
                isStreaming: false,
                model: model,
              );
            }
          }
          _streamingBuffer = '';
          _streamingId = null;
          _thinkingText = null;
          _activeTools.clear();
          _state = _state.copyWith(isTyping: false, currentModel: model);
        });
        _unlockInput();
        if (completedIdx != null && completedIdx! >= 0) {
          _prepareMessageLayout(completedIdx!);
        }
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
        setState(() {
          _state = _state.copyWith(isTyping: true);
          _thinkingText = msg['text'] as String?;
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
          if (mounted && _state.inputLockReason == 'INCOMING TRANSMISSION')
            _unlockInput();
        });

      case 'voice_audio':
        final audioData = msg['audio'] as String?;
        if (audioData != null) {
          _playAudio(audioData);
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
            if (mounted && _state.inputLockReason == 'IMAGE INCOMING')
              _unlockInput();
          });
        }

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
    }
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

  Future<void> _transcribeAndSend(String audioBase64) async {
    try {
      final serverUrl = widget.serverUrl;
      final response = await http.post(
        Uri.parse('$serverUrl/api/stt'),
        headers: _authHeaders,
        body: jsonEncode({'audio': audioBase64}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final text = (data['text'] as String? ?? '').trim();
        if (text.isNotEmpty) {
          if (!_ws.sendMessage(text)) {
            _showSendError();
            return;
          }
          if (!mounted) return;
          setState(() {
            _messages.add(
              ChatMessage(
                id: 'user-${DateTime.now().millisecondsSinceEpoch}',
                role: 'user',
                content: text,
              ),
            );
          });
          _scrollToBottom();
        }
      }
    } catch (e) {
      debugPrint('STT failed: $e');
    }
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
      audio.play();
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
        ),
      ),
    );
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
            SubscriptionScreen(serverUrl: widget.serverUrl, authToken: token),
      ),
    );
  }

  void _playAudio(String base64Audio) {
    try {
      final dataUrl = 'data:audio/wav;base64,$base64Audio';
      final audio = web.HTMLAudioElement()..src = dataUrl;
      audio.play();
    } catch (e) {
      debugPrint('Audio playback failed: $e');
    }
  }

  void _prepareMessageLayout(int messageIndex) {
    if (!PretextService.isReady) return;
    final msg = _messages[messageIndex];
    if (msg.pretextHandle != null || msg.content.isEmpty) return;
    final handle = PretextService.prepare(msg.content);
    if (handle < 0) return;
    final tightWidth = PretextService.tightBubbleWidth(handle, 360.0);
    setState(() {
      _messages[messageIndex] = msg.copyWith(
        pretextHandle: handle,
        tightBubbleWidth: tightWidth,
      );
    });
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
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        border: Border(
          bottom: BorderSide(color: GFL2Colors.border.withValues(alpha: 0.4)),
        ),
      ),
      child: Column(
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Portrait — tap to open profile
              GestureDetector(
                onTap: _openProfile,
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 600),
                  curve: Curves.easeInOut,
                  width: 52,
                  height: 52,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(4),
                    border: Border.all(
                      color: _moodGlowColor.withValues(alpha: 0.6),
                      width: 1.5,
                    ),
                    boxShadow: [
                      BoxShadow(
                        color: _moodGlowColor.withValues(alpha: 0.2),
                        blurRadius: 12,
                        spreadRadius: 2,
                      ),
                    ],
                  ),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(3),
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
                              fontSize: 22,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              // Name + designation + status
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Name with orange underline accent
                    const Text(
                      'KLUKAI',
                      style: TextStyle(
                        color: GFL2Colors.textPrimary,
                        fontSize: 16,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 2.0,
                      ),
                    ),
                    Container(
                      width: 40,
                      height: 2,
                      margin: const EdgeInsets.only(top: 2),
                      color: GFL2Colors.accent,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'SST-05  //  H.I.D.E. 404',
                      style: TextStyle(
                        color: GFL2Colors.textDim.withValues(alpha: 0.6),
                        fontSize: 10,
                        letterSpacing: 0.8,
                        fontFamily: 'monospace',
                      ),
                    ),
                    const SizedBox(height: 6),
                    // Link status + mood
                    Row(
                      children: [
                        // Connection dot — green/red with glow
                        Container(
                          width: 8,
                          height: 8,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: _state.isConnected
                                ? GFL2Colors.success
                                : GFL2Colors.danger,
                            boxShadow: [
                              BoxShadow(
                                color:
                                    (_state.isConnected
                                            ? GFL2Colors.success
                                            : GFL2Colors.danger)
                                        .withValues(alpha: 0.6),
                                blurRadius: 6,
                                spreadRadius: 1,
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(width: 6),
                        Text(
                          _state.isConnected ? 'LINK ACTIVE' : 'LINK DOWN',
                          style: TextStyle(
                            color: _state.isConnected
                                ? GFL2Colors.success.withValues(alpha: 0.8)
                                : GFL2Colors.danger.withValues(alpha: 0.8),
                            fontSize: 10,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 1.0,
                            fontFamily: 'monospace',
                          ),
                        ),
                        const Spacer(),
                        // Icon cluster: mute | archive | exit | mood
                        IconButton(
                          onPressed: () {
                            try {
                              final isOn = _jsToggleAmbientMute().toDart;
                              setState(() => _ambientMuted = !isOn);
                            } catch (_) {
                              setState(() => _ambientMuted = !_ambientMuted);
                            }
                          },
                          icon: Icon(
                            _ambientMuted ? Icons.music_off : Icons.music_note,
                            color: _ambientMuted
                                ? GFL2Colors.primary.withValues(alpha: 0.3)
                                : _moodGlowColor.withValues(alpha: 0.8),
                            size: 16,
                          ),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(
                            minWidth: 28,
                            minHeight: 28,
                          ),
                          tooltip: _ambientMuted
                              ? 'Enable ambient audio'
                              : 'Mute ambient audio',
                        ),
                        IconButton(
                          onPressed: _openArchive,
                          icon: Icon(
                            Icons.photo_library_outlined,
                            color: GFL2Colors.primary.withValues(alpha: 0.5),
                            size: 16,
                          ),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(
                            minWidth: 28,
                            minHeight: 28,
                          ),
                          tooltip: 'Memory Archive',
                        ),
                        IconButton(
                          onPressed: _openSubscription,
                          icon: Icon(
                            Icons.workspace_premium_outlined,
                            color: GFL2Colors.primary.withValues(alpha: 0.5),
                            size: 16,
                          ),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(
                            minWidth: 28,
                            minHeight: 28,
                          ),
                          tooltip: 'Subscription',
                        ),
                        GestureDetector(
                          onTap: _logout,
                          child: Tooltip(
                            message: 'Disconnect',
                            child: ExitIcon(
                              size: 18,
                              color: GFL2Colors.danger.withValues(alpha: 0.8),
                            ),
                          ),
                        ),
                        const SizedBox(width: 6),
                        MoodIndicator(mood: _state.mood),
                      ],
                    ),
                    // Heartbeat sensor below status line
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        const Spacer(),
                        HeartbeatSensor(
                          bpm: _moodBPM,
                          color: _heartbeatSpikeOverride != null
                              ? const Color(
                                  0xFFFF1744,
                                ) // Red flash during spike
                              : _moodGlowColor,
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          // Full-width affection gauge
          AffectionGauge(
            score: _state.affectionScore,
            level: _state.affectionLevel,
            levelName: _state.affectionLevelName,
            lastDelta: _lastAffectionDelta,
          ),
        ],
      ),
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
          // Use canvas bubble for finalized Klukai messages (markdown rendering)
          final bubble =
              (msg.role == 'assistant' &&
                  !msg.isStreaming &&
                  PretextService.isReady)
              ? CanvasMessageBubble(message: msg)
              : MessageBubble(message: msg);
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
      padding: const EdgeInsets.only(left: 14, bottom: 4),
      child: Row(
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
            isRecording: _isRecording,
            enabled: !_inputDisabled,
            onTapDown: () async {
              setState(() => _isRecording = true);
              try {
                await _jsStartRecording().toDart;
              } catch (_) {}
            },
            onTapUp: () async {
              setState(() => _isRecording = false);
              try {
                final b64 = await _jsStopRecording().toDart;
                if (b64 != null) {
                  _transcribeAndSend(b64.toDart);
                }
              } catch (e) {
                debugPrint('Recording failed: $e');
              }
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
