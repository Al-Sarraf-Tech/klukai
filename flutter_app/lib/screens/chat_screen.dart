import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../models/message.dart';
import '../models/companion_state.dart';
import '../services/websocket_service.dart';
import '../widgets/message_bubble.dart';
import '../widgets/mood_indicator.dart';
import '../widgets/voice_button.dart';

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

  @override
  void initState() {
    super.initState();
    _loadHistory();
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
        _scrollToBottom();
      }
    } catch (e) {
      debugPrint('Failed to load history: $e');
    }
  }

  void _connectWS() {
    final wsUrl = widget.serverUrl.replaceFirst('http', 'ws') + '/ws';
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
              _messages[idx] = _messages[idx].copyWith(
                content: _streamingBuffer,
              );
            }
          }
        });
        _scrollToBottom();

      case 'done':
        final model = msg['model'] as String?;
        setState(() {
          if (_streamingId != null) {
            final idx = _messages.indexWhere((m) => m.id == _streamingId);
            if (idx >= 0) {
              _messages[idx] = _messages[idx].copyWith(
                isStreaming: false,
                model: model,
              );
            }
          }
          _streamingBuffer = '';
          _streamingId = null;
          _state = _state.copyWith(
            isTyping: false,
            currentModel: model,
          );
        });

      case 'mood':
        setState(() {
          _state = _state.copyWith(mood: msg['mood'] as String? ?? 'neutral');
        });

      case 'thinking':
        setState(() => _state = _state.copyWith(isTyping: true));

      case 'tool_use':
        // Could show tool status in UI
        break;

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
        // TODO: Play audio via Web Audio API
        break;
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
      backgroundColor: const Color(0xFF0D1117),
      body: SafeArea(
        child: Column(
          children: [
            // ── Header ──────────────────────────────────
            _buildHeader(),
            // ── Messages ────────────────────────────────
            Expanded(child: _buildMessageList()),
            // ── Typing indicator ────────────────────────
            if (_state.isTyping && _streamingId == null)
              _buildTypingIndicator(),
            // ── Input ───────────────────────────────────
            _buildInputBar(),
          ],
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      decoration: BoxDecoration(
        color: const Color(0xFF161B22),
        border: Border(
          bottom: BorderSide(
            color: Colors.white.withValues(alpha: 0.06),
          ),
        ),
      ),
      child: Row(
        children: [
          // Connection indicator
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: _state.isConnected
                  ? const Color(0xFF4ADE80)
                  : const Color(0xFFEF4444),
            ),
          ),
          const SizedBox(width: 10),
          const Text(
            'Companion',
            style: TextStyle(
              color: Colors.white,
              fontSize: 18,
              fontWeight: FontWeight.w600,
            ),
          ),
          const Spacer(),
          MoodIndicator(mood: _state.mood),
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
            Icon(
              Icons.chat_bubble_outline,
              size: 64,
              color: Colors.white.withValues(alpha: 0.15),
            ),
            const SizedBox(height: 16),
            Text(
              'Say hello!',
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.3),
                fontSize: 16,
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
        return MessageBubble(message: _messages[index]);
      },
    );
  }

  Widget _buildTypingIndicator() {
    return Padding(
      padding: const EdgeInsets.only(left: 20, bottom: 4),
      child: Row(
        children: [
          SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: Colors.white.withValues(alpha: 0.3),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            'thinking...',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.3),
              fontSize: 12,
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
        color: const Color(0xFF161B22),
        border: Border(
          top: BorderSide(
            color: Colors.white.withValues(alpha: 0.06),
          ),
        ),
      ),
      child: Row(
        children: [
          VoiceButton(
            isRecording: _isRecording,
            enabled: _state.isConnected,
            onTapDown: () {
              setState(() => _isRecording = true);
              // TODO: Start audio recording via MediaRecorder API
            },
            onTapUp: () {
              setState(() => _isRecording = false);
              // TODO: Stop recording and send audio via WS
            },
          ),
          const SizedBox(width: 8),
          Expanded(
            child: TextField(
              controller: _textController,
              focusNode: _focusNode,
              style: const TextStyle(color: Colors.white, fontSize: 15),
              maxLines: 4,
              minLines: 1,
              decoration: InputDecoration(
                hintText: 'Message...',
                hintStyle: TextStyle(
                  color: Colors.white.withValues(alpha: 0.3),
                ),
                filled: true,
                fillColor: const Color(0xFF0D1117),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 10,
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(24),
                  borderSide: BorderSide(
                    color: Colors.white.withValues(alpha: 0.1),
                  ),
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(24),
                  borderSide: BorderSide(
                    color: Colors.white.withValues(alpha: 0.1),
                  ),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(24),
                  borderSide: const BorderSide(
                    color: Color(0xFF7C3AED),
                  ),
                ),
              ),
              onSubmitted: (_) => _sendMessage(),
            ),
          ),
          const SizedBox(width: 8),
          GestureDetector(
            onTap: _state.isConnected ? _sendMessage : null,
            child: Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: _state.isConnected
                    ? const Color(0xFF7C3AED)
                    : const Color(0xFF374151),
              ),
              child: const Icon(
                Icons.send,
                color: Colors.white,
                size: 20,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
