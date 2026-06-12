@TestOn('browser')
// Tests for MemoryService URL construction.
//
// memory_service.dart imports package:web (localStorage for auth), so it only
// compiles under the chrome platform:
//   flutter test --platform chrome test/services/memory_service_test.dart
//
// We deliberately do NOT hit a real backend here. The fetch* methods accept an
// injectable http.Client, so a MockClient covers the success / error-throw
// contract; the URL-building surface (imageUrl / thumbnailUrl) is pure.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/services/memory_service.dart';

MemoryService _serviceReturning(int statusCode, [String body = '[]']) {
  return MemoryService(
    serverUrl: 'https://klukai.example.cc',
    client: MockClient((req) async => http.Response(body, statusCode)),
  );
}

void main() {
  group('MemoryService error contract (no fake-empty on failure)', () {
    test('fetchMemories returns the parsed list on 200', () async {
      final svc = _serviceReturning(
        200,
        jsonEncode([
          {'id': 'm1', 'kept_by': 'klukai'},
          {'id': 'm2', 'kept_by': 'commander'},
        ]),
      );
      final mems = await svc.fetchMemories();
      expect(mems, hasLength(2));
      expect(mems.first.id, 'm1');
    });

    test('fetchMemories THROWS on a server error instead of returning []',
        () async {
      final svc = _serviceReturning(500, 'boom');
      expect(
        () => svc.fetchMemories(),
        throwsA(isA<MemoryServiceException>()
            .having((e) => e.statusCode, 'statusCode', 500)
            .having((e) => e.isAuthExpired, 'isAuthExpired', isFalse)),
      );
    });

    test('fetchMemories flags 401 as auth-expired (login bounce, not retry)',
        () async {
      final svc = _serviceReturning(401, 'expired');
      expect(
        () => svc.fetchMemories(),
        throwsA(isA<MemoryServiceException>()
            .having((e) => e.isAuthExpired, 'isAuthExpired', isTrue)),
      );
    });

    test('fetchTimeline throws on non-200', () async {
      final svc = _serviceReturning(503);
      expect(() => svc.fetchTimeline(),
          throwsA(isA<MemoryServiceException>()));
    });

    test('fetchCategories throws on non-200', () async {
      final svc = _serviceReturning(401);
      expect(
        () => svc.fetchCategories(),
        throwsA(isA<MemoryServiceException>()
            .having((e) => e.isAuthExpired, 'isAuthExpired', isTrue)),
      );
    });

    test('exception toString names the endpoint and code', () {
      final e = MemoryServiceException(500, '/api/memories');
      expect(e.toString(), contains('500'));
      expect(e.toString(), contains('/api/memories'));
    });
  });

  group('MemoryService URL builders', () {
    final svc = MemoryService(serverUrl: 'https://klukai.example.cc');

    test('imageUrl points at the per-id image endpoint', () {
      expect(
        svc.imageUrl('abc-123'),
        'https://klukai.example.cc/api/memories/abc-123/image',
      );
    });

    test('thumbnailUrl points at the per-id thumbnail endpoint', () {
      expect(
        svc.thumbnailUrl('abc-123'),
        'https://klukai.example.cc/api/memories/abc-123/thumbnail',
      );
    });

    test('embeds the exact id (no encoding/munging applied)', () {
      expect(
        svc.imageUrl('weird id'),
        'https://klukai.example.cc/api/memories/weird id/image',
      );
    });

    test('honors the configured server base URL', () {
      final local = MemoryService(serverUrl: 'http://localhost:8300');
      expect(
        local.imageUrl('m1'),
        'http://localhost:8300/api/memories/m1/image',
      );
      expect(
        local.thumbnailUrl('m1'),
        'http://localhost:8300/api/memories/m1/thumbnail',
      );
    });

    test('serverUrl is retained on the instance', () {
      expect(svc.serverUrl, 'https://klukai.example.cc');
    });
  });
}
