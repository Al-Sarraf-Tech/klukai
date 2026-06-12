@TestOn('browser')
// Unit tests for ChatMessage model: JSON parsing, defaults, copyWith.
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/message.dart';

void main() {
  group('ChatMessage.fromJson', () {
    test('parses a complete assistant payload', () {
      final m = ChatMessage.fromJson({
        'id': 'abc123',
        'role': 'assistant',
        'content': 'Mission accepted, Commander.',
        'mood': 'focused',
        'model': 'venice',
        'created_at': '2026-05-01T12:30:45.000Z',
        'status': 'sent',
      });

      expect(m.id, 'abc123');
      expect(m.role, 'assistant');
      expect(m.content, 'Mission accepted, Commander.');
      expect(m.mood, 'focused');
      expect(m.model, 'venice');
      expect(m.status, 'sent');
      expect(m.createdAt.toUtc(),
          DateTime.parse('2026-05-01T12:30:45.000Z').toUtc());
    });

    test('applies defaults for an empty json map', () {
      final m = ChatMessage.fromJson({});
      expect(m.id, '');
      // role defaults to 'assistant' (server-origin assumption)
      expect(m.role, 'assistant');
      expect(m.content, '');
      expect(m.mood, 'neutral');
      expect(m.model, isNull);
      expect(m.status, 'read');
      expect(m.isStreaming, isFalse);
    });

    test('null role/content/mood fall back to defaults', () {
      final m = ChatMessage.fromJson({
        'id': null,
        'role': null,
        'content': null,
        'mood': null,
        'status': null,
      });
      expect(m.id, '');
      expect(m.role, 'assistant');
      expect(m.content, '');
      expect(m.mood, 'neutral');
      expect(m.status, 'read');
    });

    test('missing created_at defaults to ~now', () {
      final before = DateTime.now();
      final m = ChatMessage.fromJson({'id': 'x', 'content': 'hi'});
      final after = DateTime.now();
      expect(
        m.createdAt.isAfter(before.subtract(const Duration(seconds: 1))) &&
            m.createdAt.isBefore(after.add(const Duration(seconds: 1))),
        isTrue,
      );
    });

    test('preserves a user role distinctly', () {
      final m = ChatMessage.fromJson({'id': '1', 'role': 'user', 'content': 'yo'});
      expect(m.role, 'user');
    });
  });

  group('ChatMessage constructor defaults', () {
    test('mood defaults to neutral and status to read', () {
      final m = ChatMessage(id: '1', role: 'user', content: 'hi');
      expect(m.mood, 'neutral');
      expect(m.status, 'read');
      expect(m.isStreaming, isFalse);
      expect(m.pretextHandle, isNull);
      expect(m.tightBubbleWidth, isNull);
      expect(m.imageData, isNull);
    });

    test('createdAt defaults to now when not provided', () {
      final before = DateTime.now();
      final m = ChatMessage(id: '1', role: 'user', content: 'hi');
      final after = DateTime.now();
      expect(m.createdAt.isBefore(after.add(const Duration(seconds: 1))), isTrue);
      expect(
          m.createdAt.isAfter(before.subtract(const Duration(seconds: 1))), isTrue);
    });

    test('explicit createdAt is preserved exactly', () {
      final ts = DateTime.utc(2025, 1, 2, 3, 4, 5);
      final m = ChatMessage(id: '1', role: 'user', content: 'hi', createdAt: ts);
      expect(m.createdAt, ts);
    });
  });

  group('ChatMessage.copyWith', () {
    final base = ChatMessage(
      id: 'orig',
      role: 'assistant',
      content: 'original',
      mood: 'composed',
      model: 'venice',
      createdAt: DateTime.utc(2026, 1, 1),
      isStreaming: true,
      pretextHandle: 7,
      tightBubbleWidth: 200.0,
      status: 'sending',
    );

    test('overrides only the named fields', () {
      final c = base.copyWith(content: 'updated', status: 'read');
      expect(c.content, 'updated');
      expect(c.status, 'read');
      // unchanged
      expect(c.id, 'orig');
      expect(c.role, 'assistant');
      expect(c.mood, 'composed');
      expect(c.model, 'venice');
      expect(c.isStreaming, isTrue);
      expect(c.pretextHandle, 7);
      expect(c.tightBubbleWidth, 200.0);
    });

    test('preserves id, role and createdAt (not overridable)', () {
      final c = base.copyWith(content: 'x');
      expect(c.id, base.id);
      expect(c.role, base.role);
      expect(c.createdAt, base.createdAt);
    });

    test('null arguments keep prior values (not nulled out)', () {
      final c = base.copyWith();
      expect(c.content, 'original');
      expect(c.mood, 'composed');
      expect(c.model, 'venice');
      expect(c.isStreaming, isTrue);
      expect(c.pretextHandle, 7);
      expect(c.tightBubbleWidth, 200.0);
      expect(c.status, 'sending');
    });

    test('can flip isStreaming false and update mood', () {
      final c = base.copyWith(isStreaming: false, mood: 'tender');
      expect(c.isStreaming, isFalse);
      expect(c.mood, 'tender');
    });

    test('PRESERVES imageData (regression: copyWith used to drop it)', () {
      final withImage = ChatMessage(
        id: 'img',
        role: 'assistant',
        content: '[IMAGE]',
        imageData: 'base64payload==',
      );
      // Any unrelated copyWith (e.g. the done-frame finalization) must not
      // silently strip the image off an image message.
      final c = withImage.copyWith(isStreaming: false, model: 'venice');
      expect(c.imageData, 'base64payload==');
    });

    test('can set imageData via copyWith', () {
      final c = base.copyWith(imageData: 'newimg==');
      expect(c.imageData, 'newimg==');
      // base remains image-free
      expect(base.imageData, isNull);
    });
  });
}
