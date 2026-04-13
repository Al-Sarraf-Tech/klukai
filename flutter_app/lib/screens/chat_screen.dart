import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'dart:js_interop';
import 'package:web/web.dart' as web;
import '../main.dart';
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

@JS('audioRecorder.start')
external JSPromise<JSBoolean> _jsStartRecording();

@JS('audioRecorder.stop')
external JSPromise<JSString?> _jsStopRecording();

@JS('ambientAudio.setMood')
external void _jsSetAmbientMood(String mood);

@JS('ambientAudio.toggleMute')
external JSBoolean _jsToggleAmbientMute();

@JS('ambientAudio.isMuted')
external JSBoolean _jsIsAmbientMuted();

/// Unregister all service workers so login.html loads from server, not cache.
void _unregisterServiceWorker() {
  try {
    final sw = web.window.navigator.serviceWorker;
    sw.getRegistrations().toDart.then((regs) {
      for (final reg in regs.toDart) {
        reg.unregister();
      }
    });
  } catch (_) {}
}

class ChatScreen extends StatefulWidget {
  final String serverUrl;
  const ChatScreen({super.key, required this.serverUrl});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  final _ws = WebSocketService();
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
  int? _heartbeatSpikeOverride;  // Temporary BPM override from heartbeat_spike
  Timer? _spikeDecayTimer;
  Timer? _inputLockTimer;  // Safety timeout to auto-unlock input

  DateTime? _lastTapTime;

  bool get _isDormMode {
    final hour = DateTime.now().hour;
    return hour >= 21 && _state.affectionLevel >= 2;
  }

  Color get _bgColor => _isDormMode ? const Color(0xFF16131E) : GFL2Colors.background;

  Color get _moodGlowColor {
    return switch (_state.mood) {
      // Core — blues and cyans
      'composed'        => const Color(0xFF4FC3F7), // calm cyan
      'focused'         => const Color(0xFF3B82F6), // sharp blue
      'prideful'        => const Color(0xFFE8923E), // proud orange
      'exasperated'     => const Color(0xFFF59E0B), // frustrated amber
      'protective'      => const Color(0xFF10B981), // guardian green
      'quietly_pleased' => const Color(0xFF6EE7B7), // subtle mint
      'competitive'     => const Color(0xFFFF6B35), // fierce orange-red
      'tender'          => const Color(0xFFE88CA5), // soft pink
      'longing'         => const Color(0xFF818CF8), // wistful indigo
      'battle_ready'    => const Color(0xFFEF4444), // combat red
      // Romantic — pinks, roses, magentas (each unique)
      'flustered'       => const Color(0xFFF472B6), // hot pink
      'affectionate'    => const Color(0xFFFDA4AF), // warm rose
      'shy'             => const Color(0xFFFFB3C6), // soft blush
      'yearning'        => const Color(0xFFC084FC), // aching purple
      'devoted'         => const Color(0xFFFB7185), // deep rose
      'passionate'      => const Color(0xFFE11D48), // burning crimson
      'jealous'         => const Color(0xFFB91C1C), // dark jealous red
      'possessive'      => const Color(0xFFBE123C), // possessive wine
      'smitten'         => const Color(0xFFFF80AB), // lovesick pink
      'infatuated'      => const Color(0xFFEC4899), // obsessive magenta
      // Tactical — teals and steel
      'vigilant'        => const Color(0xFF22D3EE), // alert cyan
      'calculating'     => const Color(0xFF94A3B8), // cold steel
      'hunting'         => const Color(0xFFD97706), // predator amber
      'adrenaline'      => const Color(0xFFEAB308), // rush gold
      // Mission stress — yellows through deep reds
      'scared'          => const Color(0xFFFACC15), // fear yellow
      'terrified'       => const Color(0xFFEF4444), // terror red
      'panicked'        => const Color(0xFFFF2D2D), // panic bright red
      'desperate'       => const Color(0xFF991B1B), // desperation dark red
      'relieved'        => const Color(0xFF5EEAD4), // relief teal
      // Relaxed — greens and soft purples
      'content'         => const Color(0xFF86EFAC), // peaceful green
      'playful'         => const Color(0xFFA78BFA), // mischief purple
      'drowsy'          => const Color(0xFF64748B), // sleepy grey
      'amused'          => const Color(0xFF34D399), // laughing emerald
      'bored'           => const Color(0xFF78716C), // dull stone
      'excited'         => const Color(0xFFFB923C), // excited tangerine
      // Dark — deep blues, purples, blacks
      'melancholic'     => const Color(0xFF6366F1), // sad indigo
      'haunted'         => const Color(0xFF7C3AED), // ghost violet
      'conflicted'      => const Color(0xFFD97706), // torn amber
      'guilty'          => const Color(0xFF78350F), // guilt brown
      'determined'      => const Color(0xFFF97316), // resolute orange
      'grieving'        => const Color(0xFF312E81), // mourning navy
      'furious'         => const Color(0xFF7F1D1D), // cold fury maroon
      // Additional — each distinct
      'nostalgic'       => const Color(0xFF8B5CF6), // memory violet
      'curious'         => const Color(0xFF06B6D4), // inquisitive cyan
      'irritated'       => const Color(0xFFEA580C), // annoyed burnt orange
      'defiant'         => const Color(0xFFDC2626), // defiance red
      'vulnerable'      => const Color(0xFFDDD6FE), // exposed lavender
      'grateful'        => const Color(0xFF2DD4BF), // thankful turquoise
      'worried'         => const Color(0xFFFCD34D), // anxious yellow
      'embarrassed'     => const Color(0xFFFF6B9D), // mortified coral
      _                 => const Color(0xFF4FC3F7), // default cyan
    };
  }

  /// Heartbeat BPM mapped to mood — reflects Klukai's emotional/physical state.
  /// A heartbeat_spike event temporarily overrides this with a higher BPM.
  int get _moodBPM {
    if (_heartbeatSpikeOverride != null) return _heartbeatSpikeOverride!;
    return switch (_state.mood) {
      // Relaxed (55-70 BPM)
      'composed'        => 65,
      'content'         => 60,
      'drowsy'          => 55,
      'bored'           => 58,
      'relieved'        => 62,
      // Warm (70-85 BPM)
      'quietly_pleased' => 72,
      'tender'          => 75,
      'affectionate'    => 78,
      'grateful'        => 73,
      'amused'          => 74,
      'playful'         => 76,
      'nostalgic'       => 70,
      'curious'         => 72,
      // Emotional (85-105 BPM)
      'flustered'       => 95,
      'shy'             => 88,
      'yearning'        => 85,
      'devoted'         => 82,
      'smitten'         => 92,
      'infatuated'      => 98,
      'longing'         => 80,
      'vulnerable'      => 88,
      'embarrassed'     => 96,
      'melancholic'     => 68,
      'haunted'         => 78,
      'conflicted'      => 85,
      'guilty'          => 82,
      'grieving'        => 72,
      'worried'         => 88,
      // Intense (105-130 BPM)
      'passionate'      => 115,
      'jealous'         => 108,
      'possessive'      => 112,
      'prideful'        => 80,
      'exasperated'     => 90,
      'protective'      => 105,
      'competitive'     => 100,
      'focused'         => 78,
      'determined'      => 95,
      'irritated'       => 92,
      'defiant'         => 98,
      'furious'         => 120,
      'excited'         => 105,
      // Combat (130-180 BPM)
      'vigilant'        => 95,
      'calculating'     => 88,
      'hunting'         => 110,
      'adrenaline'      => 145,
      'battle_ready'    => 130,
      // Mission stress (140-180 BPM)
      'scared'          => 140,
      'terrified'       => 165,
      'panicked'        => 180,
      'desperate'       => 175,
      _                 => 70,
    };
  }

  /// Lock input with a reason string and auto-unlock safety timeout.
  void _lockInput(String reason, {Duration timeout = const Duration(seconds: 30)}) {
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
      final show = _scrollController.hasClients &&
          _scrollController.position.maxScrollExtent - _scrollController.position.pixels > 300;
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

  Future<void> _playTTS(String text) async {
    try {
      final response = await http.post(
        Uri.parse('${widget.serverUrl}/api/tts'),
        headers: _authHeaders,
        body: jsonEncode({'text': text, 'language': 'en'}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final audioData = data['audio'] as String?;
        if (audioData != null) {
          final dataUrl = 'data:audio/wav;base64,$audioData';
          final audio = web.HTMLAudioElement()..src = dataUrl;
          audio.play();
          return;
        }
      }
    } catch (e) {
      debugPrint('TTS failed: $e');
    }
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
        setState(() => _messages.addAll(messages));
        for (var i = 0; i < _messages.length; i++) {
          _prepareMessageLayout(i);
        }
        _scrollToBottom(instant: true);
      }
    } catch (e) {
      debugPrint('Failed to load history: $e');
    }
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
      setState(() => _state = _state.copyWith(isConnected: connected));
    });
    _ws.messages.listen(_handleWSMessage);
  }

  void _handleWSMessage(Map<String, dynamic> msg) {
    final type = msg['type'] as String?;

    switch (type) {
      case 'token':
        final text = msg['text'] as String? ?? '';
        if (!_state.isInputLocked) {
          _lockInput('RECEIVING TRANSMISSION', timeout: const Duration(seconds: 60));
        }
        setState(() {
          _streamingBuffer += text;
          if (_streamingId == null) {
            _streamingId = 'streaming-${DateTime.now().millisecondsSinceEpoch}';
            _messages.add(ChatMessage(
              id: _streamingId!,
              role: 'assistant',
              content: _streamingBuffer,
              isStreaming: true,
            ));
          } else {
            final idx = _messages.indexWhere((m) => m.id == _streamingId);
            if (idx >= 0) {
              _messages[idx] = _messages[idx].copyWith(content: _streamingBuffer);
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
        try { _jsSetAmbientMood(msg['mood'] as String? ?? 'composed'); } catch (_) {}

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
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Row(children: [
              Icon(
                direction == 'up' ? Icons.arrow_upward : Icons.arrow_downward,
                color: direction == 'up' ? GFL2Colors.success : GFL2Colors.danger,
                size: 16,
              ),
              const SizedBox(width: 8),
              Text('TRUST LEVEL: $levelName',
                  style: const TextStyle(
                    color: GFL2Colors.textPrimary,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.8,
                    fontFamily: 'monospace',
                    fontSize: 12,
                  )),
            ]),
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
          ));
        }

      case 'proactive':
        final message = msg['message'] as String? ?? '';
        _lockInput('INCOMING TRANSMISSION', timeout: const Duration(milliseconds: 1500));
        setState(() {
          _messages.add(ChatMessage(
            id: 'proactive-${DateTime.now().millisecondsSinceEpoch}',
            role: 'assistant',
            content: message,
          ));
        });
        _scrollToBottom();
        _playNotificationSound();
        // Auto-unlock after brief pause
        Timer(const Duration(milliseconds: 1500), () {
          if (mounted && _state.inputLockReason == 'INCOMING TRANSMISSION') _unlockInput();
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
            _messages.add(ChatMessage(
              id: 'image-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: '[IMAGE]',
              imageData: imgData,
            ));
          });
          // Retry scroll 3 times to catch image decode layout shifts
          _scrollToBottom(retries: 3);
          _playNotificationSound();
          Timer(const Duration(seconds: 2), () {
            if (mounted && _state.inputLockReason == 'IMAGE INCOMING') _unlockInput();
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
    setState(() {
      _messages.add(ChatMessage(
        id: 'user-${DateTime.now().millisecondsSinceEpoch}',
        role: 'user',
        content: text,
      ));
    });
    _ws.sendMessage(text);
    _textController.clear();
    _focusNode.requestFocus();
    _scrollToBottom();
  }

  void _handleKeyScroll(KeyEvent event) {
    if (event is! KeyDownEvent) return;
    if (!_scrollController.hasClients) return;

    final pos = _scrollController.position;
    final pageSize = pos.viewportDimension * 0.8;
    final key = event.logicalKey.keyLabel;
    // Only handle navigation keys — these don't conflict with text input
    if (!{'Page Down', 'Page Up', 'Home', 'End'}.contains(key)) return;

    double? target;
    if (key == 'Page Down') {
      target = (pos.pixels + pageSize).clamp(pos.minScrollExtent, pos.maxScrollExtent);
    } else if (key == 'Page Up') {
      target = (pos.pixels - pageSize).clamp(pos.minScrollExtent, pos.maxScrollExtent);
    } else if (key == 'Home') {
      target = pos.minScrollExtent;
    } else if (key == 'End') {
      target = pos.maxScrollExtent;
    }

    if (target != null) {
      _scrollController.animateTo(target, duration: const Duration(milliseconds: 400), curve: Curves.easeOutCubic);
    }
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
          setState(() {
            _messages.add(ChatMessage(
              id: 'user-${DateTime.now().millisecondsSinceEpoch}',
              role: 'user',
              content: text,
            ));
          });
          _ws.sendMessage(text);
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
        if (isMorning) 'Good morning' else if (isEvening) 'Good evening' else "How's your day?",
        'Tell me about Belka',
        "How's the squad?",
        "Let's go for a ride",
      ];
    } else if (level <= 6) {
      return [
        if (isMorning) 'Good morning, Klukai' else if (isEvening) "Can't sleep?" else 'I was thinking about you',
        'Tell me about Mechty',
        "What's on your mind?",
        "I missed you",
      ];
    } else {
      return [
        if (isMorning) 'Good morning, beautiful' else if (isEvening) 'Come sit with me' else 'I love you',
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
        child: Text(text, style: TextStyle(
          color: GFL2Colors.primary.withValues(alpha: 0.7),
          fontSize: 12, fontFamily: 'monospace',
        )),
      ),
    );
  }

  void _playNotificationSound() {
    if (_soundMuted) return;
    try {
      final audio = web.HTMLAudioElement()..src = 'audio/comm_beep.wav'..volume = 0.3;
      audio.play();
    } catch (_) {}
  }

  void _openProfile() {
    Navigator.push(context, MaterialPageRoute(builder: (_) => ProfileScreen(
      serverUrl: widget.serverUrl,
      affectionScore: _state.affectionScore,
      affectionLevel: _state.affectionLevel,
      affectionLevelName: _state.affectionLevelName,
      totalInteractions: 0,
    )));
  }

  void _logout() {
    _ws.dispose();
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    // Unregister Flutter service worker so login.html loads from server
    _unregisterServiceWorker();
    web.window.location.href = '/';
  }

  void _openArchive() {
    Navigator.push(context, MaterialPageRoute(
      builder: (_) => MemoryArchiveScreen(
        serverUrl: widget.serverUrl,
        affectionLevel: _state.affectionLevel,
        affectionLevelName: _state.affectionLevelName,
      ),
    ));
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
      body: SafeArea(
        child: _buildMobileLayout(),
      ),
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
              child: const Icon(Icons.keyboard_arrow_down, color: GFL2Colors.primary),
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
                              child: Text('K',
                                  style: TextStyle(
                                    color: GFL2Colors.primary,
                                    fontSize: 22,
                                    fontWeight: FontWeight.w700,
                                  )),
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
                                color: (_state.isConnected
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
                          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                          tooltip: _ambientMuted ? 'Enable ambient audio' : 'Mute ambient audio',
                        ),
                        IconButton(
                          onPressed: _openArchive,
                          icon: Icon(Icons.photo_library_outlined,
                              color: GFL2Colors.primary.withValues(alpha: 0.5), size: 16),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                          tooltip: 'Memory Archive',
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
                              ? const Color(0xFFFF1744)  // Red flash during spike
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
                      child: Text('K',
                          style: TextStyle(
                              color: GFL2Colors.primary,
                              fontSize: 36,
                              fontWeight: FontWeight.w700)),
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
              spacing: 8, runSpacing: 8,
              alignment: WrapAlignment.center,
              children: _getStarters().map(_starterChip).toList(),
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      controller: _scrollController,
      physics: const ClampingScrollPhysics(parent: AlwaysScrollableScrollPhysics()),
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: _messages.length,
      itemBuilder: (context, index) {
        final msg = _messages[index];
        final msgDate = DateTime(msg.createdAt.year, msg.createdAt.month, msg.createdAt.day);
        bool showDivider = false;
        if (index == 0) {
          showDivider = true;
        } else {
          final prev = _messages[index - 1];
          final prevDate = DateTime(prev.createdAt.year, prev.createdAt.month, prev.createdAt.day);
          if (msgDate != prevDate) showDivider = true;
        }
        // Use canvas bubble for finalized Klukai messages (markdown rendering)
        final bubble = (msg.role == 'assistant' && !msg.isStreaming && PretextService.isReady)
            ? CanvasMessageBubble(message: msg)
            : MessageBubble(message: msg);
        if (showDivider) {
          return Column(
            mainAxisSize: MainAxisSize.min,
            children: [DateDivider(date: msg.createdAt), bubble],
          );
        }
        return bubble;
      },
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
              try { await _jsStartRecording().toDart; } catch (_) {}
            },
            onTapUp: () async {
              setState(() => _isRecording = false);
              try {
                final b64 = await _jsStopRecording().toDart;
                if (b64 != null) {
                  _transcribeAndSend(b64.toDart);
                }
              } catch (e) { debugPrint('Recording failed: $e'); }
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
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(4),
                    borderSide: BorderSide(color: GFL2Colors.border.withValues(alpha: 0.3)),
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(4),
                    borderSide: BorderSide(color: GFL2Colors.border.withValues(alpha: 0.3)),
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
