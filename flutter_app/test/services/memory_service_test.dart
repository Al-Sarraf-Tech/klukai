@TestOn('browser')
// Tests for MemoryService URL construction.
//
// memory_service.dart imports package:web (localStorage for auth), so it only
// compiles under the chrome platform:
//   flutter test --platform chrome test/services/memory_service_test.dart
//
// We deliberately do NOT hit a real backend here. The fetch* methods require a
// live server, so we cover the deterministic, pure URL-building surface
// (imageUrl / thumbnailUrl) plus construction. Network calls are out of scope
// for a no-backend unit test.
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/services/memory_service.dart';

void main() {
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
