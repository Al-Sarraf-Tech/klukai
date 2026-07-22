@TestOn('browser')
library;

// Unit tests for Memory, MemoryCategory and MonthGroup models.
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/memory.dart';

void main() {
  group('Memory.fromJson', () {
    test('parses a fully populated memory', () {
      final m = Memory.fromJson({
        'id': 'mem-1',
        'annotation': 'We held the line at the bridge.',
        'scene_tags': ['rain', 'bridge', 'night'],
        'mood': 'protective',
        'affection_level': 6,
        'kept_by': 'commander',
        'category': 'Combat Logs',
        'created_at': '2026-04-20T09:15:00.000Z',
      });

      expect(m.id, 'mem-1');
      expect(m.annotation, 'We held the line at the bridge.');
      expect(m.sceneTags, ['rain', 'bridge', 'night']);
      expect(m.mood, 'protective');
      expect(m.affectionLevel, 6);
      expect(m.keptBy, 'commander');
      expect(m.category, 'Combat Logs');
      expect(
        m.createdAt.toUtc(),
        DateTime.parse('2026-04-20T09:15:00.000Z').toUtc(),
      );
    });

    test('applies defaults for empty json', () {
      final m = Memory.fromJson({});
      expect(m.id, '');
      expect(m.annotation, isNull);
      expect(m.sceneTags, isEmpty);
      expect(m.mood, isNull);
      expect(m.affectionLevel, isNull);
      expect(m.keptBy, 'klukai');
      expect(m.category, 'Mission Records');
    });

    test('null scene_tags becomes an empty list', () {
      final m = Memory.fromJson({'id': 'x', 'scene_tags': null});
      expect(m.sceneTags, isEmpty);
    });

    test('scene_tags casts heterogeneous-but-string list to List<String>', () {
      final m = Memory.fromJson({
        'id': 'x',
        'scene_tags': <dynamic>['a', 'b'],
      });
      expect(m.sceneTags, isA<List<String>>());
      expect(m.sceneTags, ['a', 'b']);
    });

    test('missing created_at defaults to ~now', () {
      final before = DateTime.now();
      final m = Memory.fromJson({'id': 'x'});
      final after = DateTime.now();
      expect(
        m.createdAt.isAfter(before.subtract(const Duration(seconds: 1))) &&
            m.createdAt.isBefore(after.add(const Duration(seconds: 1))),
        isTrue,
      );
    });

    test('keptBy and category null fall back to defaults', () {
      final m = Memory.fromJson({'id': 'x', 'kept_by': null, 'category': null});
      expect(m.keptBy, 'klukai');
      expect(m.category, 'Mission Records');
    });
  });

  group('Memory constructor defaults', () {
    test('uses klukai / Mission Records / empty tags', () {
      final m = Memory(id: 'x');
      expect(m.keptBy, 'klukai');
      expect(m.category, 'Mission Records');
      expect(m.sceneTags, isEmpty);
      expect(m.annotation, isNull);
    });
  });

  group('MemoryCategory.fromJson', () {
    test('parses name and count', () {
      final c = MemoryCategory.fromJson({'name': 'Combat Logs', 'count': 12});
      expect(c.name, 'Combat Logs');
      expect(c.count, 12);
    });

    test('defaults missing fields', () {
      final c = MemoryCategory.fromJson({});
      expect(c.name, '');
      expect(c.count, 0);
    });
  });

  group('MonthGroup.fromJson and label', () {
    test('parses month and count', () {
      final g = MonthGroup.fromJson({'month': '2026-04', 'count': 5});
      expect(g.month, '2026-04');
      expect(g.count, 5);
    });

    test('label formats YYYY-MM into "MON YYYY"', () {
      expect(MonthGroup(month: '2026-04', count: 1).label, 'APR 2026');
      expect(MonthGroup(month: '2026-01', count: 1).label, 'JAN 2026');
      expect(MonthGroup(month: '2025-12', count: 1).label, 'DEC 2025');
    });

    test('label returns raw string when too short to parse', () {
      expect(MonthGroup(month: '2026', count: 1).label, '2026');
      expect(MonthGroup(month: '', count: 1).label, '');
    });

    test('label tolerates an out-of-range month number', () {
      // month "13" is invalid -> falls back to the raw numeric part
      expect(MonthGroup(month: '2026-13', count: 1).label, '13 2026');
    });

    test('label tolerates non-numeric month part', () {
      expect(MonthGroup(month: '2026-XX', count: 1).label, 'XX 2026');
    });

    test('default count is 0', () {
      expect(MonthGroup.fromJson({'month': '2026-04'}).count, 0);
    });
  });
}
