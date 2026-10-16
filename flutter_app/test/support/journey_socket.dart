import 'dart:async';

import 'package:companion_app/services/websocket_service.dart';

/// A deterministic wire boundary: tests drive connection and server events.
class JourneySocket implements WebSocketService {
  final _frames = StreamController<Map<String, dynamic>>.broadcast();
  final _links = StreamController<bool>.broadcast();
  final _revocations = StreamController<void>.broadcast();
  final List<Map<String, dynamic>> sent = [];
  bool _connected = false;
  bool _disposed = false;

  void receive(Map<String, dynamic> frame) => _frames.add(frame);
  void revoke() => _revocations.add(null);
  void link(bool connected) {
    _connected = connected;
    _links.add(connected);
  }

  @override
  bool get isConnected => _connected;
  @override
  Stream<Map<String, dynamic>> get messages => _frames.stream;
  @override
  Stream<bool> get connectionState => _links.stream;
  @override
  Stream<void> get authFailure => _revocations.stream;
  @override
  List<Duration> get reconnectDelays => const [];
  @override
  void connect(String url, {String? token}) =>
      scheduleMicrotask(() => link(true));
  @override
  void reconnectNow() => link(true);
  @override
  void disconnect() => link(false);
  @override
  bool send(Map<String, dynamic> data) {
    if (!_connected) return false;
    sent.add(data);
    return true;
  }

  @override
  bool sendMessage(String content) =>
      send({'type': 'message', 'content': content});
  @override
  bool sendTyping() => send({'type': 'typing'});
  @override
  bool sendVoiceEnd(String audioBase64) =>
      send({'type': 'voice_end', 'audio': audioBase64});
  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _frames.close();
    _links.close();
    _revocations.close();
  }
}
