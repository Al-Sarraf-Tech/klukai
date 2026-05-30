import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

class WebSocketService {
  WebSocketChannel? _channel;
  final _messageController = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();
  final _authFailureController = StreamController<void>.broadcast();
  Timer? _reconnectTimer;
  String _url = '';
  bool _intentionalClose = false;
  String? _token;

  Stream<Map<String, dynamic>> get messages => _messageController.stream;
  Stream<bool> get connectionState => _connectionController.stream;

  /// Fires when the server rejects the token (WS close 4001/4003). The UI should
  /// clear the stale token and return to login rather than reconnect forever.
  Stream<void> get authFailure => _authFailureController.stream;
  bool get isConnected => _channel != null;

  void connect(String url, {String? token}) {
    _token = token;
    // Append token as query param for WebSocket auth
    if (token != null && token.isNotEmpty) {
      final uri = Uri.parse(url);
      final sep = uri.queryParameters.isEmpty ? '?' : '&';
      _url = '$url${sep}token=$token';
    } else {
      _url = url;
    }
    _intentionalClose = false;
    _doConnect();
  }

  void _doConnect() {
    try {
      _channel = WebSocketChannel.connect(Uri.parse(_url));
      bool confirmed = false;

      _channel!.stream.listen(
        (data) {
          if (!confirmed) {
            confirmed = true;
            _connectionController.add(true);
          }
          try {
            final msg = jsonDecode(data as String) as Map<String, dynamic>;
            _messageController.add(msg);
          } catch (_) {}
        },
        onDone: () {
          _connectionController.add(false);
          // The server closes with 4001 (and 4003) on a bad/expired token.
          // Reconnecting forever would soft-lock the app (LINK DOWN, empty
          // history, no path back to login) — signal auth failure instead.
          final closeCode = _channel?.closeCode;
          _channel = null;
          if (closeCode == 4001 || closeCode == 4003) {
            _authFailureController.add(null);
            return;
          }
          if (!_intentionalClose) {
            _scheduleReconnect();
          }
        },
        onError: (error) {
          _connectionController.add(false);
          _channel = null;
          if (!_intentionalClose) {
            _scheduleReconnect();
          }
        },
      );
    } catch (e) {
      _connectionController.add(false);
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 3), () {
      if (!_intentionalClose) {
        _doConnect();
      }
    });
  }

  void send(Map<String, dynamic> data) {
    if (_channel != null) {
      _channel!.sink.add(jsonEncode(data));
    }
  }

  void sendMessage(String content) {
    send({'type': 'message', 'content': content, 'attachments': []});
  }

  void sendTyping() {
    send({'type': 'typing'});
  }

  void sendVoiceEnd(String audioBase64) {
    send({'type': 'voice_end', 'audio': audioBase64});
  }

  void disconnect() {
    _intentionalClose = true;
    _reconnectTimer?.cancel();
    _channel?.sink.close();
    _channel = null;
    _connectionController.add(false);
  }

  void dispose() {
    disconnect();
    _messageController.close();
    _connectionController.close();
    _authFailureController.close();
  }
}
