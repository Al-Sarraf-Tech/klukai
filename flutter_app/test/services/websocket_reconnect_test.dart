@TestOn('vm')
library;

// WebSocketService reconnect behaviour against a real in-process server:
// resume redial, the stale-channel race, backoff, and auth-failure handling.
//   flutter test test/services/websocket_reconnect_test.dart
import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/services/websocket_service.dart';

/// Accepts sockets, greets each with a frame (so the client confirms), and
/// keeps them so a test can close one at a time.
class _Server {
  late HttpServer _http;
  final sockets = <WebSocket>[];
  final _connected = StreamController<WebSocket>.broadcast();
  bool greet = true;
  int? closeImmediatelyWith;

  String get url => 'ws://127.0.0.1:${_http.port}/ws';
  Stream<WebSocket> get onConnect => _connected.stream;

  Future<void> start() async {
    _http = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _http.listen((req) async {
      final ws = await WebSocketTransformer.upgrade(req);
      sockets.add(ws);
      ws.listen((_) {}, onDone: () {});
      if (closeImmediatelyWith != null) {
        await ws.close(closeImmediatelyWith);
      } else if (greet) {
        ws.add('{"type":"hello"}');
      }
      _connected.add(ws);
    });
  }

  Future<void> stop() async {
    for (final s in sockets) {
      await s.close();
    }
    await _http.close(force: true);
  }
}

Future<void> _until(bool Function() cond, {Duration timeout = const Duration(seconds: 3)}) async {
  final end = DateTime.now().add(timeout);
  while (!cond()) {
    if (DateTime.now().isAfter(end)) fail('condition not met within $timeout');
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

void main() {
  late _Server server;
  late WebSocketService svc;

  setUp(() async {
    server = _Server();
    await server.start();
    svc = WebSocketService(reconnectDelays: const [
      Duration(milliseconds: 20),
      Duration(milliseconds: 40),
    ]);
  });

  tearDown(() async {
    svc.dispose();
    await server.stop();
  });

  test('not "connected" until the server has spoken', () async {
    server.greet = false;
    svc.connect(server.url);
    await _until(() => server.sockets.length == 1);
    expect(svc.isConnected, isFalse); // opening, or a zombie socket
    server.sockets.first.add('{"type":"hello"}');
    await _until(() => svc.isConnected);
  });

  test('reconnectNow redials at once and the replaced socket cannot kill it', () async {
    svc.connect(server.url);
    await _until(() => svc.isConnected);

    svc.reconnectNow();
    await _until(() => server.sockets.length == 2 && svc.isConnected);

    // The OLD socket closes late (iOS resume: its close arrives after the
    // new dial). Before the fix this nulled out the new channel and dialed a
    // third socket.
    await server.sockets.first.close();
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(server.sockets, hasLength(2));
    expect(svc.isConnected, isTrue);
    expect(svc.send({'type': 'typing'}), isTrue);
  });

  test('a dropped link redials with backoff and reports state changes', () async {
    final states = <bool>[];
    svc.connectionState.listen(states.add);
    svc.connect(server.url);
    await _until(() => svc.isConnected);

    await server.sockets.first.close();
    await _until(() => server.sockets.length == 2 && svc.isConnected);
    expect(states, containsAllInOrder([true, false, true]));
  });

  test('reconnectNow is a no-op after an intentional disconnect', () async {
    svc.connect(server.url);
    await _until(() => svc.isConnected);
    svc.disconnect();
    svc.reconnectNow();
    await Future<void>.delayed(const Duration(milliseconds: 100));
    expect(server.sockets, hasLength(1));
    expect(svc.isConnected, isFalse);
  });

  test('reconnectNow before connect() does nothing', () async {
    svc.reconnectNow();
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(server.sockets, isEmpty);
  });

  test('close 4001 signals auth failure and stops redialing', () async {
    server.closeImmediatelyWith = 4001;
    final failures = <void>[];
    svc.authFailure.listen(failures.add);
    svc.connect(server.url);
    await _until(() => failures.isNotEmpty);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(server.sockets, hasLength(1));
  });

  test('connector errors schedule a retry instead of throwing', () async {
    var calls = 0;
    final flaky = WebSocketService(
      connector: (uri) {
        calls++;
        throw const SocketException('unreachable');
      },
      reconnectDelays: const [Duration(milliseconds: 10)],
    );
    flaky.connect('ws://127.0.0.1:1/ws');
    await _until(() => calls >= 3);
    flaky.dispose();
    final after = calls;
    await Future<void>.delayed(const Duration(milliseconds: 60));
    expect(calls, after); // dispose stops the loop
  });
}
