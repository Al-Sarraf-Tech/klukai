import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Opens a channel. Injectable so tests can drive the socket without a server.
typedef WebSocketConnector = WebSocketChannel Function(Uri uri);

class WebSocketService {
  WebSocketService({
    WebSocketConnector? connector,
    this.reconnectDelays = const [
      Duration(seconds: 1),
      Duration(seconds: 3),
      Duration(seconds: 6),
      Duration(seconds: 10),
    ],
  }) : _connector = connector ?? WebSocketChannel.connect;

  final WebSocketConnector _connector;

  /// Backoff between automatic reconnect attempts; the last value repeats.
  final List<Duration> reconnectDelays;

  WebSocketChannel? _channel;
  final _messageController = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();
  final _authFailureController = StreamController<void>.broadcast();
  Timer? _reconnectTimer;
  String _url = '';
  bool _intentionalClose = false;
  bool _confirmed = false;
  int _attempt = 0;
  bool _disposed = false;

  Stream<Map<String, dynamic>> get messages => _messageController.stream;
  Stream<bool> get connectionState => _connectionController.stream;

  /// Fires when the server rejects the token (WS close 4001/4003). The UI should
  /// clear the stale token and return to login rather than reconnect forever.
  Stream<void> get authFailure => _authFailureController.stream;

  /// True once the server has spoken on the current channel. A channel that is
  /// still opening (or a dead one iOS froze in the background) is not "connected".
  bool get isConnected => _channel != null && _confirmed;

  void connect(String url, {String? token}) {
    // Append token as query param for WebSocket auth
    if (token != null && token.isNotEmpty) {
      final uri = Uri.parse(url);
      final sep = uri.queryParameters.isEmpty ? '?' : '&';
      _url = '$url${sep}token=$token';
    } else {
      _url = url;
    }
    _intentionalClose = false;
    _attempt = 0;
    _doConnect();
  }

  /// Drop whatever channel we have and dial again now — used when the page
  /// comes back from the background, where iOS has usually killed the socket
  /// without telling us. No-op after an intentional disconnect.
  void reconnectNow() {
    if (_intentionalClose || _disposed || _url.isEmpty) return;
    _reconnectTimer?.cancel();
    _attempt = 0;
    final old = _channel;
    _channel = null; // detach first: its late onDone must not touch the new one
    _confirmed = false;
    try {
      old?.sink.close();
    } catch (_) {}
    _doConnect();
  }

  void _doConnect() {
    if (_disposed) return;
    late final WebSocketChannel channel;
    try {
      channel = _connector(Uri.parse(_url));
    } catch (e) {
      _emitConnection(false);
      _scheduleReconnect();
      return;
    }
    _channel = channel;
    _confirmed = false;

    // Every callback checks it still belongs to the CURRENT channel. Without
    // this, a replaced channel closing late nulls out (and re-dials over) the
    // healthy new one — two sockets, duplicated frames.
    bool current() => identical(_channel, channel);

    channel.stream.listen(
      (data) {
        if (!current()) return;
        if (!_confirmed) {
          _confirmed = true;
          _attempt = 0;
          _emitConnection(true);
        }
        try {
          final msg = jsonDecode(data as String) as Map<String, dynamic>;
          _messageController.add(msg);
        } catch (_) {}
      },
      onDone: () {
        if (!current()) return;
        // The server closes with 4001 (and 4003) on a bad/expired token.
        // Reconnecting forever would soft-lock the app (LINK DOWN, empty
        // history, no path back to login) — signal auth failure instead.
        final closeCode = channel.closeCode;
        _channel = null;
        _confirmed = false;
        _emitConnection(false);
        if (closeCode == 4001 || closeCode == 4003) {
          _authFailureController.add(null);
          return;
        }
        if (!_intentionalClose) _scheduleReconnect();
      },
      onError: (Object error) {
        if (!current()) return;
        _channel = null;
        _confirmed = false;
        _emitConnection(false);
        if (!_intentionalClose) _scheduleReconnect();
      },
      cancelOnError: true,
    );
  }

  void _emitConnection(bool value) {
    if (!_connectionController.isClosed) _connectionController.add(value);
  }

  void _scheduleReconnect() {
    _reconnectTimer?.cancel();
    final delays = reconnectDelays.isEmpty ? const [Duration(seconds: 3)] : reconnectDelays;
    final delay = delays[_attempt.clamp(0, delays.length - 1)];
    _attempt++;
    _reconnectTimer = Timer(delay, () {
      if (!_intentionalClose) _doConnect();
    });
  }

  /// Sends [data] over the channel. Returns false when the channel is gone
  /// (mid-reconnect) so callers can surface the failure instead of silently
  /// dropping the frame.
  bool send(Map<String, dynamic> data) {
    final channel = _channel;
    if (channel == null) return false;
    try {
      channel.sink.add(jsonEncode(data));
      return true;
    } catch (_) {
      return false;
    }
  }

  bool sendMessage(String content) {
    return send({'type': 'message', 'content': content, 'attachments': []});
  }

  bool sendTyping() {
    return send({'type': 'typing'});
  }

  bool sendVoiceEnd(String audioBase64) {
    return send({'type': 'voice_end', 'audio': audioBase64});
  }

  void disconnect() {
    _intentionalClose = true;
    _reconnectTimer?.cancel();
    final channel = _channel;
    _channel = null;
    _confirmed = false;
    try {
      channel?.sink.close();
    } catch (_) {}
    _emitConnection(false);
  }

  void dispose() {
    disconnect();
    _disposed = true;
    _messageController.close();
    _connectionController.close();
    _authFailureController.close();
  }
}
