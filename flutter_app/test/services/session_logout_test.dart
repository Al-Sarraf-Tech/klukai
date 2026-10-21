import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/services/session_logout.dart';

void main() {
  test('posts the bearer to /api/auth/logout', () async {
    final seen = <http.Request>[];
    final client = MockClient((request) async {
      seen.add(request);
      return http.Response('{"ok":true}', 200);
    });
    await endServerSession('https://k.example', 'tok-1', client: client);
    expect(seen, hasLength(1));
    expect(seen.single.method, 'POST');
    expect(seen.single.url.toString(), 'https://k.example/api/auth/logout');
    expect(seen.single.headers['Authorization'], 'Bearer tok-1');
  });

  test('no token, no request', () async {
    var calls = 0;
    final client = MockClient((_) async {
      calls++;
      return http.Response('', 200);
    });
    await endServerSession('https://k.example', '', client: client);
    expect(calls, 0);
  });

  test('a dead link never holds up sign-out', () async {
    final client = MockClient((_) => Completer<http.Response>().future);
    final watch = Stopwatch()..start();
    await endServerSession(
      'https://k.example',
      'tok-1',
      client: client,
      timeout: const Duration(milliseconds: 50),
    );
    expect(watch.elapsed, lessThan(const Duration(seconds: 2)));
  });

  test('a server error is swallowed', () async {
    final client = MockClient((_) async => throw http.ClientException('down'));
    await endServerSession('https://k.example', 'tok-1', client: client);
  });
}
