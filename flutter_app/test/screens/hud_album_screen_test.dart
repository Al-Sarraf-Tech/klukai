// HudAlbumScreen on the VM: a fake MemoryService, a MockClient for the
// thumbnails and recording AlbumLinks, so no browser is involved.
//   flutter test test/screens/hud_album_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/models/memory.dart';
import 'package:companion_app/screens/hud_album_links.dart';
import 'package:companion_app/screens/hud_album_screen.dart';
import 'package:companion_app/services/memory_service.dart';
import 'package:companion_app/widgets/hud_parts.dart';

class _FakeMemoryService extends MemoryService {
  _FakeMemoryService({
    required this.pages,
    this.categories = const [],
    this.timeline = const [],
    this.error,
  }) : super(serverUrl: 'http://127.0.0.1:1');

  /// Pages handed out in order; the last one repeats.
  final List<List<Memory>> pages;
  List<MemoryCategory> categories;
  List<MonthGroup> timeline;
  Object? error;
  final List<({String? before, String? beforeId, String? month})> calls = [];

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
    String? beforeId,
  }) async {
    calls.add((before: before, beforeId: beforeId, month: month));
    final e = error;
    if (e != null) throw e;
    final i = (calls.length - 1).clamp(0, pages.length - 1);
    return pages[i];
  }

  @override
  Future<List<MemoryCategory>> fetchCategories() async => categories;

  @override
  Future<List<MonthGroup>> fetchTimeline() async => timeline;
}

class _Links implements AlbumLinks {
  final List<String> details = [];
  int herPov = 0;
  int thread = 0;
  void Function(String id)? lastDiscard;

  @override
  Future<void> openDetail(
    BuildContext context, {
    required Memory memory,
    required String serverUrl,
    required void Function(String id) onDiscarded,
  }) async {
    details.add(memory.id);
    lastDiscard = onDiscarded;
  }

  @override
  Future<void> openHerPov(
    BuildContext context, {
    required String serverUrl,
    required int affectionLevel,
    required String affectionLevelName,
    VoidCallback? onAuthExpired,
  }) async => herPov++;

  @override
  Future<void> openThread(
    BuildContext context, {
    required String serverUrl,
    VoidCallback? onAuthExpired,
  }) async => thread++;
}

final _now = DateTime(2026, 10, 19, 22, 0);

Memory _m(
  String id,
  String caption, {
  String category = 'Mission Records',
  List<String> tags = const [],
  DateTime? at,
}) => Memory(
  id: id,
  annotation: caption,
  category: category,
  sceneTags: tags,
  createdAt: at ?? DateTime(2026, 10, 10, 12),
  createdAtRaw: (at ?? DateTime(2026, 10, 10, 12)).toIso8601String(),
);

final _sample = [
  _m(
    'stars',
    'The stars were clear. I checked twice.',
    category: 'The Commander',
    at: DateTime(2026, 10, 19, 21, 40),
  ),
  _m(
    'cat',
    'The white one picked you.',
    category: 'Precious Memories',
    at: DateTime(2026, 10, 18, 15),
  ),
  _m(
    'pov',
    'He fell asleep first. I drew it.',
    category: 'The Commander',
    tags: const ['her_pov', 'from_her_side'],
    at: DateTime(2026, 10, 16, 23),
  ),
  _m('range', 'Every round on target.', at: DateTime(2026, 10, 12, 9)),
  _m(
    'dream',
    'The Elmo was quiet in the dream.',
    category: 'Dreams',
    at: DateTime(2026, 10, 11, 4),
  ),
];

List<MemoryCategory> _cats({bool allOpen = false}) => [
  MemoryCategory(name: 'Tactical Operations', count: 1),
  MemoryCategory(name: 'Mission Records', count: 40),
  MemoryCategory(name: 'Squad Moments', count: 0),
  MemoryCategory(name: 'Dreams', count: 3),
  MemoryCategory(name: 'The Commander', count: 70),
  MemoryCategory(name: 'Quiet Hours', count: 2),
  if (allOpen) MemoryCategory(name: 'Precious Memories', count: 12),
  MemoryCategory(name: 'All', count: 128),
];

Future<(_FakeMemoryService, _Links)> _pump(
  WidgetTester tester, {
  _FakeMemoryService? service,
  double width = 390,
  VoidCallback? onAuthExpired,
  bool settle = true,
}) async {
  tester.view.physicalSize = Size(width, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final fake =
      service ?? _FakeMemoryService(pages: [_sample], categories: _cats());
  final links = _Links();
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: HudAlbumScreen(
          serverUrl: 'http://127.0.0.1:1',
          affectionLevel: 5,
          affectionLevelName: 'Trusted',
          memoryService: fake,
          imageClient: MockClient((_) async => http.Response('', 404)),
          links: links,
          onAuthExpired: onAuthExpired,
          now: () => _now,
        ),
      ),
    ),
  );
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await tester.pump();
    await tester.pump();
  }
  return (fake, links);
}

/// Taps a chip the way he would: scrolled into view first, since the row
/// scrolls sideways once the chips outgrow the screen.
Future<void> _chip(WidgetTester tester, AlbumFilter f) async {
  final chip = find.byKey(ValueKey('album-filter-$f'));
  await tester.ensureVisible(chip);
  await tester.pumpAndSettle();
  await tester.tap(chip);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('header, featured NEW entry and the grid', (tester) async {
    await _pump(tester);
    expect(find.text('ARCHIVE // MEMORY ALBUM'), findsOneWidget);
    expect(find.text('ALBUM'), findsOneWidget);
    expect(find.text('HER COLLECTION'), findsOneWidget);
    // The server's own "All" total, not a sum that counts it twice.
    expect(
      tester.widget<Text>(find.byKey(const Key('album-count'))).data,
      '128 ENTRIES',
    );
    expect(find.byKey(const Key('album-featured-stars')), findsOneWidget);
    expect(find.byKey(const Key('album-featured-tag')), findsOneWidget);
    expect(find.text('NEW'), findsOneWidget);
    expect(find.text('TONIGHT · 21:40 · WITH YOU'), findsOneWidget);
    expect(find.text('YESTERDAY · WITH YOU'), findsOneWidget);
    expect(find.text('OCT 16 · HER POV'), findsOneWidget);
    expect(find.text('OCT 12 · MOMENTS'), findsOneWidget);
    expect(find.text('OCT 11 · DREAM'), findsOneWidget);
    // Chips are real buttons.
    for (final f in AlbumFilter.values) {
      expect(find.byKey(ValueKey('album-filter-$f')), findsOneWidget);
    }
  });

  testWidgets('an older latest entry is LATEST, not NEW', (tester) async {
    await _pump(
      tester,
      service: _FakeMemoryService(pages: [_sample.sublist(3)]),
    );
    expect(find.text('LATEST'), findsOneWidget);
    expect(find.text('NEW'), findsNothing);
  });

  testWidgets('filters map onto the real categories and tag', (tester) async {
    await _pump(tester);

    await _chip(tester, AlbumFilter.herPov);
    expect(find.byKey(const Key('album-featured-pov')), findsOneWidget);
    expect(find.byKey(const Key('album-card-cat')), findsNothing);
    expect(find.byKey(const Key('album-card-range')), findsNothing);
    // The way into Her POV leads the grid.
    expect(find.byKey(const Key('album-her-pov-card')), findsOneWidget);

    await _chip(tester, AlbumFilter.withYou);
    expect(find.byKey(const Key('album-featured-stars')), findsOneWidget);
    expect(find.byKey(const Key('album-card-cat')), findsOneWidget);
    expect(find.byKey(const Key('album-card-pov')), findsOneWidget);
    expect(find.byKey(const Key('album-card-range')), findsNothing);
    expect(find.byKey(const Key('album-card-dream')), findsNothing);

    await _chip(tester, AlbumFilter.moments);
    expect(find.byKey(const Key('album-featured-range')), findsOneWidget);
    expect(find.byKey(const Key('album-card-dream')), findsOneWidget);
    expect(find.byKey(const Key('album-card-cat')), findsNothing);

    await _chip(tester, AlbumFilter.all);
    expect(find.byKey(const Key('album-featured-stars')), findsOneWidget);
  });

  test('filter predicates', () {
    final pov = _sample[2];
    expect(isHerPovMemory(pov), isTrue);
    expect(albumFilterMatches(AlbumFilter.withYou, pov), isTrue);
    expect(albumFilterMatches(AlbumFilter.moments, pov), isFalse);
    expect(albumKindLabel(_sample[4]), 'DREAM');
    expect(albumFilterMatches(AlbumFilter.moments, _sample[4]), isTrue);
  });

  testWidgets('categories she has not opened yet show locked', (tester) async {
    await _pump(tester);
    // Precious Memories is missing from the server's list: locked at LV 6.
    expect(
      find.byKey(const Key('album-locked-Precious Memories')),
      findsOneWidget,
    );
    expect(find.text('UNLOCKS AT LV 6'), findsOneWidget);
    expect(find.byKey(const Key('album-locked-The Commander')), findsNothing);

    await _chip(tester, AlbumFilter.moments);
    expect(
      find.byKey(const Key('album-locked-Precious Memories')),
      findsNothing,
    );
  });

  testWidgets('every category open: nothing locked', (tester) async {
    await _pump(
      tester,
      service: _FakeMemoryService(
        pages: [_sample],
        categories: _cats(allOpen: true),
      ),
    );
    expect(find.textContaining('UNLOCKS AT'), findsNothing);
    expect(find.byKey(const Key('album-her-pov-card')), findsOneWidget);
  });

  testWidgets('tapping opens the existing detail; a discard drops it', (
    tester,
  ) async {
    final (_, links) = await _pump(tester);
    await tester.tap(find.byKey(const Key('album-card-cat')));
    await tester.pump();
    expect(links.details, ['cat']);

    await tester.tap(find.byKey(const Key('album-featured-stars')));
    await tester.pump();
    expect(links.details, ['cat', 'stars']);

    links.lastDiscard!('stars');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('album-featured-stars')), findsNothing);
    expect(find.byKey(const Key('album-featured-cat')), findsOneWidget);
  });

  testWidgets('Her POV and the Thread are one tap away', (tester) async {
    final (fake, links) = await _pump(tester);
    final before = fake.calls.length;

    await tester.tap(find.byKey(const Key('album-her-pov')));
    await tester.pumpAndSettle();
    expect(links.herPov, 1);
    // Back from Her POV: reload, she may have drawn one.
    expect(fake.calls.length, before + 1);

    await tester.tap(find.byKey(const Key('album-her-pov-card')));
    await tester.pumpAndSettle();
    expect(links.herPov, 2);

    await tester.tap(find.byKey(const Key('album-thread')));
    await tester.pumpAndSettle();
    expect(links.thread, 1);
  });

  testWidgets('the month timeline filters by month', (tester) async {
    final (fake, _) = await _pump(
      tester,
      service: _FakeMemoryService(
        pages: [_sample],
        categories: _cats(),
        timeline: [
          MonthGroup(month: '2026-10', count: 5),
          MonthGroup(month: '2026-09', count: 7),
        ],
      ),
    );
    await tester.tap(find.byKey(const Key('album-months')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('album-month-2026-09')));
    await tester.pumpAndSettle();
    expect(fake.calls.last.month, '2026-09');
    expect(find.text('SEP 2026  ×'), findsOneWidget);

    await tester.tap(find.byKey(const Key('album-month-clear')));
    await tester.pumpAndSettle();
    expect(fake.calls.last.month, isNull);
  });

  testWidgets('a full page pages on by the oldest timestamp and id', (
    tester,
  ) async {
    final first = [
      for (var i = 0; i < 50; i++)
        _m(
          'm$i',
          'entry $i',
          at: DateTime(2026, 9, 30).subtract(Duration(hours: i)),
        ),
    ];
    final second = [_m('older', 'the oldest one', at: DateTime(2026, 8, 1))];
    final fake = _FakeMemoryService(
      pages: [first, second],
      categories: _cats(),
    );
    tester.view.physicalSize = const Size(390, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: HudAlbumScreen(
            serverUrl: 'http://127.0.0.1:1',
            memoryService: fake,
            imageClient: MockClient((_) async => http.Response('', 404)),
            links: _Links(),
            now: () => _now,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(fake.calls, hasLength(1));

    // Near the end of what is loaded: the next page is asked for.
    for (var i = 0; i < 6 && fake.calls.length < 2; i++) {
      await tester.drag(
        find.byKey(const Key('album-scroll')),
        const Offset(0, -900),
      );
      await tester.pumpAndSettle();
    }
    expect(fake.calls, hasLength(2));
    expect(find.byKey(const Key('album-card-older')), findsOneWidget);
    expect(fake.calls[1].beforeId, 'm49');
    expect(fake.calls[1].before, first.last.createdAtRaw);
  });

  testWidgets('an unreachable archive shows RETRY, not an empty album', (
    tester,
  ) async {
    final fake = _FakeMemoryService(
      pages: [_sample],
      error: MemoryServiceException(503, '/api/memories'),
    );
    await _pump(tester, service: fake);
    expect(find.byKey(const Key('album-error')), findsOneWidget);
    expect(find.byKey(const Key('album-empty')), findsNothing);

    fake.error = null;
    await tester.tap(find.text('RETRY'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('album-featured-stars')), findsOneWidget);
  });

  testWidgets('a dead token bounces once', (tester) async {
    var bounced = 0;
    await _pump(
      tester,
      service: _FakeMemoryService(
        pages: [_sample],
        error: MemoryServiceException(401, '/api/memories'),
      ),
      onAuthExpired: () => bounced++,
      settle: false,
    );
    expect(bounced, 1);
  });

  testWidgets('empty archive says so', (tester) async {
    await _pump(
      tester,
      service: _FakeMemoryService(
        pages: [[]],
        categories: _cats(allOpen: true),
      ),
    );
    expect(find.byKey(const Key('album-empty')), findsOneWidget);
    expect(find.text('NO MEMORIES ARCHIVED YET.'), findsOneWidget);
  });

  for (final width in [360.0, 390.0, 430.0]) {
    testWidgets('no overflow at $width px; the chips stay on one row', (
      tester,
    ) async {
      await _pump(tester, width: width);
      expect(tester.takeException(), isNull);
      final chips = [
        for (final f in AlbumFilter.values)
          tester.getRect(find.byKey(ValueKey('album-filter-$f'))),
      ];
      for (final c in chips) {
        expect(c.center.dy, moreOrLessEquals(chips.first.center.dy));
      }
      // Edge to edge, but the first chip starts on the 16 px gutter, like
      // the header and the cards.
      final row = tester.getRect(
        find.byKey(const ValueKey('album-filter-row')),
      );
      expect(row.left, 0);
      expect(row.right, lessThanOrEqualTo(width));
      expect(chips.first.left, 16);
      final header = tester.getRect(find.byType(HudScreenHeader));
      expect(header.left, 16);
      expect(header.right, width - 16);
      expect(
        tester.getRect(find.byKey(const Key('album-featured-stars'))).left,
        16,
      );

      await _chip(tester, AlbumFilter.herPov);
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('album-featured-pov')), findsOneWidget);
      await _chip(tester, AlbumFilter.moments);
      expect(find.byKey(const Key('album-featured-range')), findsOneWidget);
      expect(find.byKey(const Key('album-card-cat')), findsNothing);
    });
  }

  testWidgets('the month chip keeps the gutter', (tester) async {
    await _pump(
      tester,
      width: 360,
      service: _FakeMemoryService(
        pages: [_sample],
        categories: _cats(),
        timeline: [MonthGroup(month: '2026-09', count: 7)],
      ),
    );
    await tester.tap(find.byKey(const Key('album-months')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('album-month-2026-09')));
    await tester.pumpAndSettle();
    expect(tester.getRect(find.byKey(const Key('album-month-clear'))).left, 16);
    expect(tester.takeException(), isNull);
  });
}
