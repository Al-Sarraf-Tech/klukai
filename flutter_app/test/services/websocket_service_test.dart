@TestOn('vm')
library;

// Tests for WebSocketService.
//
// websocket_service.dart has NO web-only imports, so these run on the default
// VM platform (`flutter test test/services/websocket_service_test.dart`),
// which lets us stand up a real in-process WebSocket echo server instead of
// mocking. This exercises connect/send/receive/disconnect against actual
// web_socket_channel wiring without touching any real backend.
import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/services/websocket_service.dart';

/// Minimal in-process echo server. Captures the request URI of the last
/// upgraded socket so we can assert on the token query param, and echoes
/// every text frame back to the client.
class _EchoServer {
  late HttpServer _server;
  final List<Uri> requestUris = [];
  WebSocket? lastSocket;
  final _socketReady = Completer<WebSocket>();

  int get port => _server.port;
  String get wsUrl => 'ws://127.0.0.1:$port/ws';

  /// Completes with the first server-side socket once a client connects.
  Future<WebSocket> get firstSocket => _socketReady.future;

  Future<void> start() async {
    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _server.listen((HttpRequest req) async {
      requestUris.add(req.uri);
      if (WebSocketTransformer.isUpgradeRequest(req)) {
        final ws = await WebSocketTransformer.upgrade(req);
        lastSocket = ws;
        if (!_socketReady.isCompleted) _socketReady.complete(ws);
        ws.listen((data) {
          // Echo text frames straight back.
          ws.add(data);
        });
      } else {
        req.response.statusCode = HttpStatus.badRequest;
        await req.response.close();
      }
    });
  }

  Future<void> stop() => _server.close(force: true);
}

void main() {
  group('WebSocketService initial state', () {
    test('is not connected before connect()', () {
      final svc = WebSocketService();
      expect(svc.isConnected, isFalse);
      svc.dispose();
    });

    test('exposes broadcast streams (multiple listeners allowed)', () {
      final svc = WebSocketService();
      // Broadcast streams permit more than one subscription without throwing.
      final s1 = svc.messages.listen((_) {});
      final s2 = svc.messages.listen((_) {});
      final c1 = svc.connectionState.listen((_) {});
      final c2 = svc.connectionState.listen((_) {});
      s1.cancel();
      s2.cancel();
      c1.cancel();
      c2.cancel();
      svc.dispose();
    });
  });

  group('WebSocketService against a live echo server', () {
    late _EchoServer server;

    setUp(() async {
      server = _EchoServer();
      await server.start();
    });

    tearDown(() async {
      await server.stop();
    });

    test('connect emits connectionState=true after first frame', () async {
      final svc = WebSocketService();
      final connected = svc.connectionState.firstWhere((c) => c == true);

      svc.connect(server.wsUrl);
      // The service only confirms connection upon receiving a frame, so we
      // send one to trigger the echo.
      // Give the socket a beat to open, then poke it.
      await server.firstSocket;
      svc.send({'type': 'ping'});

      await connected.timeout(const Duration(seconds: 5));
      expect(svc.isConnected, isTrue);

      svc.dispose();
    });

    test('sendMessage serializes the expected JSON envelope', () async {
      final svc = WebSocketService();
      final firstMsg = svc.messages.first;

      svc.connect(server.wsUrl);
      await server.firstSocket;
      svc.sendMessage('hello there');

      final echoed = await firstMsg.timeout(const Duration(seconds: 5));
      expect(echoed['type'], 'message');
      expect(echoed['content'], 'hello there');
      expect(echoed['attachments'], isEmpty);

      svc.dispose();
    });

    test('sendTyping emits a typing envelope', () async {
      final svc = WebSocketService();
      final firstMsg = svc.messages.first;

      svc.connect(server.wsUrl);
      await server.firstSocket;
      svc.sendTyping();

      final echoed = await firstMsg.timeout(const Duration(seconds: 5));
      expect(echoed['type'], 'typing');

      svc.dispose();
    });

    test('sendVoiceEnd carries the base64 audio payload', () async {
      final svc = WebSocketService();
      final firstMsg = svc.messages.first;

      svc.connect(server.wsUrl);
      await server.firstSocket;
      svc.sendVoiceEnd('QUJDRA==');

      final echoed = await firstMsg.timeout(const Duration(seconds: 5));
      expect(echoed['type'], 'voice_end');
      expect(echoed['audio'], 'QUJDRA==');

      svc.dispose();
    });

    test('appends token as a query param when none present', () async {
      final svc = WebSocketService();
      svc.connect(server.wsUrl, token: 'secrettoken');
      await Future<void>.delayed(const Duration(milliseconds: 150));

      expect(server.requestUris, isNotEmpty);
      final uri = server.requestUris.first;
      expect(uri.queryParameters['token'], 'secrettoken');

      svc.dispose();
    });

    test('appends token with & when the URL already has a query', () async {
      final svc = WebSocketService();
      svc.connect('${server.wsUrl}?room=alpha', token: 'tkn');
      await Future<void>.delayed(const Duration(milliseconds: 150));

      final uri = server.requestUris.first;
      expect(uri.queryParameters['room'], 'alpha');
      expect(uri.queryParameters['token'], 'tkn');

      svc.dispose();
    });

    test('omits token query param when token is empty', () async {
      final svc = WebSocketService();
      svc.connect(server.wsUrl, token: '');
      await Future<void>.delayed(const Duration(milliseconds: 150));

      final uri = server.requestUris.first;
      expect(uri.queryParameters.containsKey('token'), isFalse);

      svc.dispose();
    });

    test('confirms connection on first frame and decodes valid JSON', () async {
      final svc = WebSocketService();
      final firstMsg = svc.messages.first;
      final connected = svc.connectionState.firstWhere((c) => c == true);

      svc.connect(server.wsUrl);
      await server.firstSocket; // server-side socket established
      svc.send({'type': 'noop', 'n': 1});

      await connected.timeout(const Duration(seconds: 5));
      final decoded = await firstMsg.timeout(const Duration(seconds: 5));
      expect(svc.isConnected, isTrue);
      expect(decoded['type'], 'noop');
      expect(decoded['n'], 1);

      svc.dispose();
    });

    test(
      'swallows a malformed (non-JSON) frame without crashing the stream',
      () async {
        // The service jsonDecodes every inbound frame inside a try/catch and
        // drops anything that fails to parse. Push a raw garbage frame straight
        // from the server, then a valid one, and assert only the valid one
        // surfaces on `messages` (and the socket stays alive).
        final svc = WebSocketService();
        final received = <Map<String, dynamic>>[];
        final sub = svc.messages.listen(received.add);
        final connected = svc.connectionState.firstWhere((c) => c == true);

        svc.connect(server.wsUrl);
        final socket = await server.firstSocket;

        // 1) malformed frame -> must be swallowed, but still confirms connection
        socket.add('this is not json {');
        await connected.timeout(const Duration(seconds: 5));
        expect(svc.isConnected, isTrue);

        // 2) valid frame -> must be decoded and delivered
        socket.add('{"type":"ok","v":42}');
        await Future<void>.delayed(const Duration(milliseconds: 200));

        // Exactly one (the valid) message should have come through.
        expect(received.length, 1);
        expect(received.single['type'], 'ok');
        expect(received.single['v'], 42);

        await sub.cancel();
        svc.dispose();
      },
    );

    test(
      'disconnect emits connectionState=false and clears isConnected',
      () async {
        final svc = WebSocketService();
        final connected = svc.connectionState.firstWhere((c) => c == true);
        svc.connect(server.wsUrl);
        await server.firstSocket;
        svc.send({'type': 'ping'});
        await connected.timeout(const Duration(seconds: 5));

        final disconnected = svc.connectionState.firstWhere((c) => c == false);
        svc.disconnect();
        await disconnected.timeout(const Duration(seconds: 5));
        expect(svc.isConnected, isFalse);

        svc.dispose();
      },
    );
  });

  group('WebSocketService.send when not connected', () {
    test('is a no-op (does not throw) before connect', () {
      final svc = WebSocketService();
      // No channel yet -> guarded by the null-channel check.
      expect(() => svc.send({'type': 'x'}), returnsNormally);
      expect(() => svc.sendMessage('hi'), returnsNormally);
      expect(() => svc.sendTyping(), returnsNormally);
      svc.dispose();
    });

    test('returns false before connect (no silent drop)', () {
      // A dropped frame must be observable: send() reports failure so the UI
      // can avoid echoing a message that never left.
      final svc = WebSocketService();
      expect(svc.send({'type': 'x'}), isFalse);
      expect(svc.sendMessage('hi'), isFalse);
      expect(svc.sendTyping(), isFalse);
      expect(svc.sendVoiceEnd('QUJDRA=='), isFalse);
      svc.dispose();
    });

    test('returns false again after disconnect()', () async {
      final server = _EchoServer();
      await server.start();
      final svc = WebSocketService();
      final connected = svc.connectionState.firstWhere((c) => c == true);
      svc.connect(server.wsUrl);
      await server.firstSocket;
      expect(svc.send({'type': 'ping'}), isTrue);
      await connected.timeout(const Duration(seconds: 5));

      svc.disconnect();
      expect(svc.sendMessage('too late'), isFalse);

      svc.dispose();
      await server.stop();
    });
  });

  group('WebSocketService.send when connected', () {
    test('returns true once the channel is open', () async {
      final server = _EchoServer();
      await server.start();
      final svc = WebSocketService();
      svc.connect(server.wsUrl);
      await server.firstSocket;
      expect(svc.sendMessage('hello'), isTrue);
      svc.dispose();
      await server.stop();
    });
  });
}
