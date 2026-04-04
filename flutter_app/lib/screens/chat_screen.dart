import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
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

  @override
  void initState() {
    super.initState();
    _loadHistory();
    _loadAffection();
    _connectWS();
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
        _scrollToBottom();
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
        _scrollToBottom();

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

      case 'mood':
        setState(() {
          _state = _state.copyWith(mood: msg['mood'] as String? ?? 'composed');
        });

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

      case 'voice_audio':
        // Play Klukai's voice via Web Audio
        final audioData = msg['audio'] as String?;
        if (audioData != null) {
          _playAudio(audioData);
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
      _scrollController.animateTo(target, duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
    }
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

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  void dispose() {
    _ws.dispose();
    _textController.dispose();
    _scrollController.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GFL2Colors.background,
      body: SafeArea(
        child: KeyboardListener(
          focusNode: FocusNode()..requestFocus(),
          autofocus: true,
          onKeyEvent: _handleKeyScroll,
          child: Column(
            children: [
              _buildHeader(),
              Expanded(child: _buildMessageList()),
              if (_activeTools.isNotEmpty) _buildToolStatus(),
              if (_state.isTyping && _streamingId == null) _buildProcessingIndicator(),
              _buildInputBar(),
            ],
          ),
        ),
      ),
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
              // Portrait with angular cyan border
              Container(
                width: 52,
                height: 52,
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(4),
                  border: Border.all(
                    color: GFL2Colors.primary.withValues(alpha: 0.5),
                    width: 1.5,
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: GFL2Colors.primary.withValues(alpha: 0.1),
                      blurRadius: 8,
                      spreadRadius: 1,
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
          ],
        ),
      );
    }

    return ListView.builder(
      controller: _scrollController,
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
            onTapDown: () => setState(() => _isRecording = true),
            onTapUp: () => setState(() => _isRecording = false),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: KeyboardListener(
              focusNode: FocusNode(),
              onKeyEvent: (event) {
                if (event is KeyDownEvent &&
                    event.logicalKey.keyLabel == 'Enter' &&
                    !HardwareKeyboard.instance.isShiftPressed) {
                  _sendMessage();
                }
              },
              child: TextField(
                controller: _textController,
                focusNode: _focusNode,
                style: const TextStyle(color: GFL2Colors.textPrimary, fontSize: 14),
                maxLines: 4,
                minLines: 1,
                textInputAction: TextInputAction.send,
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
