import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

class WebSocketService {
  WebSocketChannel? _channel;
  final _messageController = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();
  Timer? _reconnectTimer;
  String _url = '';
  bool _intentionalClose = false;

  Stream<Map<String, dynamic>> get messages => _messageController.stream;
  Stream<bool> get connectionState => _connectionController.stream;
  bool get isConnected => _channel != null;

  void connect(String url) {
    _url = url;
    _intentionalClose = false;
    _doConnect();
  }

  void _doConnect() {
    try {
      _channel = WebSocketChannel.connect(Uri.parse(_url));
      _connectionController.add(true);

      _channel!.stream.listen(
        (data) {
          try {
            final msg = jsonDecode(data as String) as Map<String, dynamic>;
            _messageController.add(msg);
          } catch (_) {}
        },
        onDone: () {
          _connectionController.add(false);
          _channel = null;
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
  }
}
