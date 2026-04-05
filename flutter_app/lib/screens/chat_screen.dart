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
import '../widgets/klukai_avatar.dart';
import '../widgets/klukai_avatar_panel.dart';
import '../widgets/klukai_avatar_strip.dart';
import '../widgets/speech_bubble.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'profile_screen.dart';

@JS('audioRecorder.start')
external JSPromise<JSBoolean> _jsStartRecording();

@JS('audioRecorder.stop')
external JSPromise<JSString?> _jsStopRecording();

class ChatScreen extends StatefulWidget {
  final String serverUrl;
  const ChatScreen({super.key, required this.serverUrl});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
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

  // 3D Avatar state
  bool _avatarEnabled = false;
  bool _audioEnabled = false;
  final _avatarController = KlukaiAvatarController();
  String _speechBubbleText = '';
  bool _isSpeechStreaming = false;
  DateTime? _lastAssistantMessageTime;
  DateTime? _lastTapTime;
  String _lastAssistantContent = '';

  bool get _isDormMode {
    final hour = DateTime.now().hour;
    return hour >= 21 && _state.affectionLevel >= 2;
  }

  Color get _bgColor => _isDormMode ? const Color(0xFF16131E) : GFL2Colors.background;

  Color get _moodGlowColor {
    return switch (_state.mood) {
      // Core
      'composed'        => GFL2Colors.primary,
      'focused'         => const Color(0xFF3B82F6),
      'prideful'        => GFL2Colors.accent,
      'exasperated'     => const Color(0xFFF59E0B),
      'protective'      => GFL2Colors.success,
      'quietly_pleased' => const Color(0xFF6EE7B7),
      'competitive'     => GFL2Colors.danger,
      'tender'          => GFL2Colors.affinity,
      'longing'         => const Color(0xFF818CF8),
      'battle_ready'    => GFL2Colors.danger,
      // Romantic
      'flustered'       => const Color(0xFFF472B6),
      'affectionate'    => const Color(0xFFFDA4AF),
      'shy'             => const Color(0xFFF9A8D4),
      'yearning'        => const Color(0xFFC084FC),
      'devoted'         => const Color(0xFFFB7185),
      // Tactical
      'vigilant'        => const Color(0xFF22D3EE),
      'calculating'     => const Color(0xFF94A3B8),
      'hunting'         => GFL2Colors.danger,
      'adrenaline'      => const Color(0xFFFBBF24),
      // Relaxed
      'content'         => const Color(0xFF86EFAC),
      'playful'         => const Color(0xFFA78BFA),
      'drowsy'          => const Color(0xFF64748B),
      'amused'          => const Color(0xFF34D399),
      // Dark
      'melancholic'     => const Color(0xFF6366F1),
      'haunted'         => const Color(0xFF7C3AED),
      'conflicted'      => const Color(0xFFD97706),
      'determined'      => const Color(0xFFF97316),
      'vulnerable'      => const Color(0xFFDDD6FE),
      _                 => GFL2Colors.primary,
    };
  }

  @override
  void initState() {
    super.initState();
    _loadHistory();
    _loadAffection();
    _connectWS();
    _loadAvatarPrefs();
  }

  Future<void> _loadAvatarPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _avatarEnabled = prefs.getBool('avatar_enabled') ?? false;
      _audioEnabled = prefs.getBool('avatar_audio_enabled') ?? false;
    });
  }

  Future<void> _toggleAvatar() async {
    final prefs = await SharedPreferences.getInstance();
    final newVal = !_avatarEnabled;
    await prefs.setBool('avatar_enabled', newVal);
    setState(() {
      _avatarEnabled = newVal;
      if (!newVal) {
        _avatarController.dispose();
        _speechBubbleText = '';
      }
    });
  }

  Future<void> _toggleAudio() async {
    final prefs = await SharedPreferences.getInstance();
    final newVal = !_audioEnabled;
    await prefs.setBool('avatar_audio_enabled', newVal);
    setState(() => _audioEnabled = newVal);
  }

  void _handleAvatarTap() {
    final now = DateTime.now();
    if (_lastTapTime != null &&
        now.difference(_lastTapTime!).inSeconds < 5) {
      return;
    }
    _lastTapTime = now;

    _avatarController.playReaction('reaction_tap');

    if (_lastAssistantMessageTime != null &&
        now.difference(_lastAssistantMessageTime!).inSeconds < 30 &&
        _lastAssistantContent.isNotEmpty) {
      setState(() {
        _speechBubbleText = _lastAssistantContent;
        _isSpeechStreaming = false;
      });
      if (_audioEnabled) {
        _playTTS(_lastAssistantContent);
      }
    } else {
      _ws.send({'type': 'tap_interact'});
    }
  }

  Future<void> _playTTS(String text) async {
    _avatarController.setTalking(true);
    try {
      final response = await http.post(
        Uri.parse('${widget.serverUrl}/api/tts'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'text': text, 'language': 'en'}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final audioData = data['audio'] as String?;
        if (audioData != null) {
          final dataUrl = 'data:audio/wav;base64,$audioData';
          final audio = web.HTMLAudioElement()..src = dataUrl;
          audio.onEnded.listen((_) {
            _avatarController.setTalking(false);
          });
          audio.play();
          return;
        }
      }
    } catch (e) {
      debugPrint('TTS failed: $e');
    }
    _avatarController.setTalking(false);
  }

  void _dismissSpeechBubble() {
    setState(() => _speechBubbleText = '');
  }

  Future<void> _loadHistory() async {
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/messages?limit=50');
      final response = await http.get(uri);
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
      final response = await http.get(uri);
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

  void _connectWS() {
    final wsUrl = '${widget.serverUrl.replaceFirst('http', 'ws')}/ws';
    _ws.connect(wsUrl);
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
        if (_avatarEnabled) {
          setState(() {
            _speechBubbleText = _streamingBuffer;
            _isSpeechStreaming = true;
          });
        }

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
        if (completedIdx != null && completedIdx! >= 0) {
          _prepareMessageLayout(completedIdx!);
        }
        _playNotificationSound();
        if (_avatarEnabled) {
          _lastAssistantMessageTime = DateTime.now();
          final completedMsg = completedIdx != null && completedIdx! >= 0
              ? _messages[completedIdx!]
              : null;
          if (completedMsg != null) {
            _lastAssistantContent = completedMsg.content;
          }
          setState(() => _isSpeechStreaming = false);
          if (_audioEnabled && _lastAssistantContent.isNotEmpty) {
            _playTTS(_lastAssistantContent);
          }
        }

      case 'mood':
        setState(() {
          _state = _state.copyWith(mood: msg['mood'] as String? ?? 'composed');
        });
        if (_avatarEnabled) {
          _avatarController.setMood(msg['mood'] as String? ?? 'composed');
        }

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
        setState(() {
          _messages.add(ChatMessage(
            id: 'proactive-${DateTime.now().millisecondsSinceEpoch}',
            role: 'assistant',
            content: message,
          ));
        });
        _scrollToBottom();
        _playNotificationSound();
        if (_avatarEnabled) {
          _lastAssistantMessageTime = DateTime.now();
          _lastAssistantContent = message;
          setState(() {
            _speechBubbleText = message;
            _isSpeechStreaming = false;
          });
          if (_audioEnabled) {
            _playTTS(message);
          }
        }

      case 'voice_audio':
        final audioData = msg['audio'] as String?;
        if (audioData != null) {
          _playAudio(audioData);
        }

      case 'image':
        final imgData = msg['data'] as String?;
        if (imgData != null) {
          setState(() {
            _messages.add(ChatMessage(
              id: 'image-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: '[IMAGE]',
              imageData: imgData,
            ));
          });
          _scrollToBottom();
          _playNotificationSound();
        }
    }
  }

  void _sendMessage() {
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
    // Don't intercept keys when the text input has focus
    if (_focusNode.hasFocus) return;

    final pos = _scrollController.position;
    final pageSize = pos.viewportDimension * 0.8;
    final key = event.logicalKey.keyLabel;

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
        headers: {'Content-Type': 'application/json'},
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

  void _scrollToBottom({bool instant = false}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        if (instant) {
          _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
        } else {
          _scrollController.animateTo(
            _scrollController.position.maxScrollExtent,
            duration: const Duration(milliseconds: 300),
            curve: Curves.easeOutCubic,
          );
        }
      }
    });
  }

  @override
  void dispose() {
    _ws.dispose();
    _textController.dispose();
    _scrollController.dispose();
    _focusNode.dispose();
    if (_avatarEnabled) {
      _avatarController.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final isWide = MediaQuery.of(context).size.width > 768;

    if (_avatarEnabled && _avatarController.isInitialized) {
      _avatarController.setDormMode(_isDormMode);
    }

    return Scaffold(
      backgroundColor: _bgColor,
      body: SafeArea(
        child: isWide && _avatarEnabled
            ? _buildDesktopLayout()
            : _buildMobileLayout(),
      ),
    );
  }

  Widget _buildDesktopLayout() {
    return Row(
      children: [
        SizedBox(
          width: MediaQuery.of(context).size.width * 0.35,
          child: KlukaiAvatarPanel(
            controller: _avatarController,
            modelUrl: 'assets/models/klukai.glb',
            speechText: _speechBubbleText,
            isSpeechStreaming: _isSpeechStreaming,
            audioEnabled: _audioEnabled,
            moodGlowColor: _moodGlowColor,
            onTap: _handleAvatarTap,
            onAudioToggle: _toggleAudio,
            onSpeechDismiss: _dismissSpeechBubble,
          ),
        ),
        Expanded(
          child: Column(
            children: [
              _buildHeader(),
              Expanded(child: _buildMessageList()),
              if (_activeTools.isNotEmpty) _buildToolStatus(),
              if (_state.isTyping && _streamingId == null)
                _buildProcessingIndicator(),
              _buildInputBar(),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildMobileLayout() {
    return Column(
      children: [
        _buildHeader(),
        if (_avatarEnabled)
          KlukaiAvatarStrip(
            controller: _avatarController,
            modelUrl: 'assets/models/klukai.glb',
            speechText: _speechBubbleText,
            isSpeechStreaming: _isSpeechStreaming,
            audioEnabled: _audioEnabled,
            moodGlowColor: _moodGlowColor,
            onTap: _handleAvatarTap,
            onAudioToggle: _toggleAudio,
            onSpeechDismiss: _dismissSpeechBubble,
          ),
        Expanded(child: _buildMessageList()),
        if (_activeTools.isNotEmpty) _buildToolStatus(),
        if (_state.isTyping && _streamingId == null)
          _buildProcessingIndicator(),
        _buildInputBar(),
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
                        MoodIndicator(mood: _state.mood),
                      ],
                    ),
                  ],
                ),
              ),
              // Avatar toggle
              const SizedBox(width: 8),
              IconButton(
                onPressed: _toggleAvatar,
                icon: Icon(
                  _avatarEnabled ? Icons.view_in_ar : Icons.view_in_ar_outlined,
                  color: _avatarEnabled
                      ? GFL2Colors.primary
                      : GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 20,
                ),
                style: IconButton.styleFrom(
                  fixedSize: const Size(32, 32),
                  padding: EdgeInsets.zero,
                ),
                tooltip: _avatarEnabled ? 'Hide 3D Avatar' : 'Show 3D Avatar',
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
            // Conversation starters
            Wrap(
              spacing: 8, runSpacing: 8,
              alignment: WrapAlignment.center,
              children: [
                _starterChip('Mission briefing'),
                _starterChip('Tell me about Belka'),
                _starterChip("How's the squad?"),
                _starterChip("Let's go for a ride"),
              ],
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      controller: _scrollController,
      physics: const BouncingScrollPhysics(parent: AlwaysScrollableScrollPhysics()),
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: _messages.length,
      itemBuilder: (context, index) {
        final msg = _messages[index];
        // Use canvas bubble for finalized Klukai messages (markdown rendering)
        if (msg.role == 'assistant' && !msg.isStreaming && PretextService.isReady) {
          return CanvasMessageBubble(message: msg);
        }
        return MessageBubble(message: msg);
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
            enabled: _state.isConnected,
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
              style: const TextStyle(color: GFL2Colors.textPrimary, fontSize: 14),
              maxLines: 4,
              minLines: 1,
              textInputAction: TextInputAction.send,
              onSubmitted: (_) => _sendMessage(),
                decoration: InputDecoration(
                  hintText: '// ENTER COMMAND...',
                  hintStyle: TextStyle(
                    color: GFL2Colors.textDim.withValues(alpha: 0.35),
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
            onPressed: _state.isConnected ? _sendMessage : null,
            icon: const Icon(Icons.send, color: Colors.white, size: 20),
            style: IconButton.styleFrom(
              backgroundColor: _state.isConnected
                  ? GFL2Colors.accent
                  : GFL2Colors.border,
              fixedSize: const Size(44, 44),
              shape: const CircleBorder(),
            ),
          ),
        ],
      ),
    );
  }
}
