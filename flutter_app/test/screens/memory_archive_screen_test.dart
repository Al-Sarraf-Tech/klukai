@TestOn('browser')
library;

// Widget tests for MemoryArchiveScreen — error/RETRY state and the
// request-generation race guard.
//
// The screen imports package:web (localStorage token), so this runs under
// chrome:  flutter test --platform chrome test/screens/memory_archive_screen_test.dart
//
// A fake MemoryService is injected through the screen's `memoryService`
// constructor param so no network is involved. Each fetchMemories call hands
// the test a Completer, letting tests resolve responses out of order to
// exercise the stale-response guard.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/memory.dart';
import 'package:companion_app/screens/memory_archive_screen.dart';
import 'package:companion_app/services/memory_service.dart';
import 'package:companion_app/widgets/memory_timeline_entry.dart';

class _FakeMemoryService extends MemoryService {
  _FakeMemoryService() : super(serverUrl: 'http://127.0.0.1:1');

  /// One completer per fetchMemories call, in call order.
  final List<Completer<List<Memory>>> memoryCalls = [];

  /// The arguments of each fetchMemories call, in call order.
  final List<
    ({
      String? category,
      int limit,
      String? before,
      String? month,
      String? beforeId,
    })
  >
  memoryArgs = [];
  List<MemoryCategory> categories = [];
  List<MonthGroup> timeline = [];

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
    String? beforeId,
  }) {
    final completer = Completer<List<Memory>>();
    memoryCalls.add(completer);
    memoryArgs.add((
      category: category,
      limit: limit,
      before: before,
      month: month,
      beforeId: beforeId,
    ));
    return completer.future;
  }

  @override
  Future<List<MemoryCategory>> fetchCategories() async => categories;

  @override
  Future<List<MonthGroup>> fetchTimeline() async => timeline;
}

Memory _memory(String id, String annotation) =>
    Memory(id: id, annotation: annotation, createdAt: DateTime(2026, 4, 7));

/// [count] memories, newest first, one hour apart, ids `mem-<start>`…
List<Memory> _page(int start, int count) => [
  for (var i = 0; i < count; i++)
    Memory(
      id: 'mem-${start + i}',
      annotation: 'entry ${start + i}',
      createdAt: DateTime.utc(
        2026,
        4,
        7,
        12,
      ).subtract(Duration(hours: start + i)),
    ),
];

Future<_FakeMemoryService> _pumpArchive(
  WidgetTester tester, {
  VoidCallback? onAuthExpired,
}) async {
  final fake = _FakeMemoryService();
  await tester.pumpWidget(
    MaterialApp(
      home: MemoryArchiveScreen(
        serverUrl: 'http://127.0.0.1:1',
        memoryService: fake,
        onAuthExpired: onAuthExpired,
      ),
    ),
  );
  await tester.pump();
  return fake;
}

const _sentinel = Key('archive-load-more');

void main() {
  testWidgets('the archive header opens The Thread', (tester) async {
    await _pumpArchive(tester);
    expect(find.byTooltip('The Thread'), findsOneWidget);
    expect(find.byTooltip('Back'), findsOneWidget);
  });

  testWidgets('the detail dialog controls are labelled', (tester) async {
    final fake = await _pumpArchive(tester);
    fake.memoryCalls.single.complete([_memory('m1', 'the quiet ride home')]);
    await tester.pump();
    await tester.pump();

    // The entry's GestureDetector defers to its children, so tap the
    // annotation text (as the entry's own test does) rather than the row's
    // empty centre.
    await tester.tap(find.textContaining('the quiet ride home'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byTooltip('Close'), findsOneWidget);
    expect(find.byTooltip('Discard (hide from album)'), findsOneWidget);

    await tester.tap(find.byTooltip('Close'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byTooltip('Close'), findsNothing);
    // The dialog's full-image fetch carries a 20s timeout; let it lapse so
    // no fake-clock timer outlives the test.
    await tester.pump(const Duration(seconds: 21));
  });

  group('MemoryArchiveScreen — a dead token is not a down channel', () {
    testWidgets('401 on the list bounces to login, never SIGNAL LOST', (
      tester,
    ) async {
      var bounced = 0;
      final fake = await _pumpArchive(tester, onAuthExpired: () => bounced++);
      fake.memoryCalls.single.completeError(
        MemoryServiceException(401, '/api/memories'),
      );
      await tester.pump();
      await tester.pump();

      expect(bounced, 1);
      expect(find.text('SIGNAL LOST // ARCHIVE UNREACHABLE'), findsNothing);
      expect(find.text('RETRY'), findsNothing);
      expect(find.text('NO MEMORIES ARCHIVED YET.'), findsNothing);
    });
  });

  group('MemoryArchiveScreen — paging past the first 50', () {
    testWidgets(
      'a full page arms a sentinel that fetches the page before the oldest entry',
      (tester) async {
        final fake = await _pumpArchive(tester);
        expect(fake.memoryArgs.single.limit, 50);
        expect(fake.memoryArgs.single.before, isNull);

        final first = _page(0, 50);
        fake.memoryCalls.single.complete(first);
        await tester.pump();
        await tester.pump();
        // The sentinel is far below the fold: nothing fetched yet.
        expect(fake.memoryCalls, hasLength(1));
        expect(find.byKey(_sentinel), findsNothing);

        await tester.scrollUntilVisible(
          find.byKey(_sentinel),
          500,
          scrollable: find.byType(Scrollable).first,
        );
        await tester.pump(); // post-frame -> _loadMore
        await tester.pump();

        expect(fake.memoryCalls, hasLength(2));
        final args = fake.memoryArgs.last;
        expect(args.before, first.last.createdAt.toUtc().toIso8601String());
        // The id breaks timestamp ties, so equal-time rows are not skipped.
        expect(args.beforeId, first.last.id);
        expect(args.limit, 50);
        expect(args.category, isNull);
        expect(find.text('RETRIEVING OLDER MEMORIES...'), findsOneWidget);

        // A short second page ends the paging; its rows are appended.
        fake.memoryCalls.last.complete(_page(50, 3));
        await tester.pump();
        await tester.pump();
        expect(find.byKey(_sentinel), findsNothing);
        await tester.drag(
          find.byType(Scrollable).first,
          const Offset(0, -5000),
        );
        await tester.pump();
        expect(find.textContaining('entry 52'), findsOneWidget);
        expect(fake.memoryCalls, hasLength(2));
      },
    );

    testWidgets(
      'the cursor is the server\'s exact timestamp, not a rounded one',
      (tester) async {
        final fake = await _pumpArchive(tester);
        // Microseconds survive only in the raw string; a web DateTime keeps
        // milliseconds, which would skip rows inside the lost precision.
        final page = [
          for (var i = 0; i < 50; i++)
            Memory.fromJson({
              'id': 'mem-$i',
              'annotation': 'entry $i',
              'created_at': '2026-04-07T12:00:00.123456+00:00',
            }),
        ];
        fake.memoryCalls.single.complete(page);
        await tester.pump();
        await tester.pump();
        await tester.scrollUntilVisible(
          find.byKey(_sentinel),
          500,
          scrollable: find.byType(Scrollable).first,
        );
        await tester.pump();
        await tester.pump();
        final args = fake.memoryArgs.last;
        expect(args.before, '2026-04-07T12:00:00.123456+00:00');
        expect(args.beforeId, 'mem-49');
      },
    );

    testWidgets('a short first page never asks for more', (tester) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.complete(_page(0, 3));
      await tester.pump();
      await tester.pump();
      expect(find.byKey(_sentinel), findsNothing);
      await tester.drag(find.byType(Scrollable).first, const Offset(0, -5000));
      await tester.pump();
      await tester.pump();
      expect(fake.memoryCalls, hasLength(1));
    });

    testWidgets('a failed next page waits for a tap instead of hammering', (
      tester,
    ) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.complete(_page(0, 50));
      await tester.pump();
      await tester.pump();
      await tester.scrollUntilVisible(
        find.byKey(_sentinel),
        500,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.pump();
      await tester.pump();
      expect(fake.memoryCalls, hasLength(2));

      fake.memoryCalls.last.completeError(
        MemoryServiceException(503, '/api/memories'),
      );
      await tester.pump();
      await tester.pump();
      expect(
        find.text('SIGNAL DROPPED // TAP TO RETRIEVE OLDER'),
        findsOneWidget,
      );
      // The first page is still on screen — no fake-empty, no full-screen error.
      expect(find.text('SIGNAL LOST // ARCHIVE UNREACHABLE'), findsNothing);
      await tester.pump(const Duration(seconds: 1));
      expect(fake.memoryCalls, hasLength(2)); // no automatic retry storm

      await tester.tap(find.text('SIGNAL DROPPED // TAP TO RETRIEVE OLDER'));
      await tester.pump();
      expect(fake.memoryCalls, hasLength(3));
      fake.memoryCalls.last.complete(_page(50, 1));
      await tester.pump();
      await tester.pump();
      expect(find.byKey(_sentinel), findsNothing);
    });

    testWidgets('a filter switch mid-page drops the late page', (tester) async {
      final fake = _FakeMemoryService();
      fake.categories = [MemoryCategory(name: 'Quiet Moments', count: 1)];
      await tester.pumpWidget(
        MaterialApp(
          home: MemoryArchiveScreen(
            serverUrl: 'http://127.0.0.1:1',
            memoryService: fake,
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      fake.memoryCalls.single.complete(_page(0, 50));
      await tester.pump();
      await tester.pump();
      await tester.scrollUntilVisible(
        find.byKey(_sentinel),
        500,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.pump();
      await tester.pump();
      expect(fake.memoryCalls, hasLength(2)); // page 2 of "All" in flight

      await tester.tap(find.text('Quiet Moments'));
      await tester.pump();
      expect(fake.memoryCalls, hasLength(3)); // first page of the new filter
      fake.memoryCalls[2].complete([_memory('q1', 'a quiet one')]);
      await tester.pump();
      await tester.pump();
      expect(find.textContaining('a quiet one'), findsOneWidget);

      // The stale "All" page lands late and must not be appended.
      fake.memoryCalls[1].complete(_page(50, 50));
      await tester.pump();
      await tester.pump();
      expect(find.textContaining('entry 50'), findsNothing);
      expect(find.byKey(_sentinel), findsNothing);
    });
  });

  group('MemoryArchiveScreen — error is NOT a fake-empty archive', () {
    testWidgets(
      'server error shows SIGNAL LOST + RETRY, never the empty state',
      (tester) async {
        final fake = await _pumpArchive(tester);
        expect(fake.memoryCalls, hasLength(1));

        fake.memoryCalls.single.completeError(
          MemoryServiceException(500, '/api/memories'),
        );
        await tester.pump();
        await tester.pump();

        expect(find.text('SIGNAL LOST // ARCHIVE UNREACHABLE'), findsOneWidget);
        expect(find.text('RETRY'), findsOneWidget);
        expect(find.text('NO MEMORIES ARCHIVED YET.'), findsNothing);
      },
    );

    testWidgets('network failure (non-HTTP exception) also shows RETRY', (
      tester,
    ) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.completeError(Exception('connection refused'));
      await tester.pump();
      await tester.pump();

      expect(find.text('RETRY'), findsOneWidget);
      expect(find.text('NO MEMORIES ARCHIVED YET.'), findsNothing);
    });

    testWidgets('RETRY refetches and renders the recovered memories', (
      tester,
    ) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.completeError(
        MemoryServiceException(502, '/api/memories'),
      );
      await tester.pump();
      await tester.pump();
      expect(find.text('RETRY'), findsOneWidget);

      await tester.tap(find.text('RETRY'));
      await tester.pump();
      expect(fake.memoryCalls, hasLength(2));

      fake.memoryCalls.last.complete([_memory('m1', 'the quiet ride home')]);
      await tester.pump();
      await tester.pump();

      expect(find.text('RETRY'), findsNothing);
      expect(find.byType(MemoryTimelineEntry), findsOneWidget);
      expect(find.textContaining('the quiet ride home'), findsOneWidget);
    });

    testWidgets('a genuinely empty archive still shows the empty state', (
      tester,
    ) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.complete(const []);
      await tester.pump();
      await tester.pump();

      expect(find.text('NO MEMORIES ARCHIVED YET.'), findsOneWidget);
      expect(find.text('RETRY'), findsNothing);
    });
  });

  group('MemoryArchiveScreen — stale-response race guard', () {
    testWidgets(
      'a slow response from a previous filter cannot clobber the latest one',
      (tester) async {
        final fake = _FakeMemoryService();
        fake.categories = [MemoryCategory(name: 'Quiet Moments', count: 1)];
        await tester.pumpWidget(
          MaterialApp(
            home: MemoryArchiveScreen(
              serverUrl: 'http://127.0.0.1:1',
              memoryService: fake,
            ),
          ),
        );
        await tester.pump(); // categories resolve; initial memories pending
        await tester.pump();
        expect(fake.memoryCalls, hasLength(1)); // gen 1 (All) — still in flight

        // Switch filters while gen 1 is still pending.
        await tester.tap(find.text('Quiet Moments'));
        await tester.pump();
        expect(fake.memoryCalls, hasLength(2)); // gen 2 (Quiet Moments)

        // Latest request resolves first...
        fake.memoryCalls[1].complete([_memory('b', 'fresh filter result')]);
        await tester.pump();
        await tester.pump();
        expect(find.textContaining('fresh filter result'), findsOneWidget);

        // ...then the STALE gen-1 response lands late. It must be discarded.
        fake.memoryCalls[0].complete([_memory('a', 'stale all-filter result')]);
        await tester.pump();
        await tester.pump();

        expect(find.textContaining('fresh filter result'), findsOneWidget);
        expect(find.textContaining('stale all-filter result'), findsNothing);
      },
    );
  });

  group('MemoryArchiveScreen — entries are keyed by memory id', () {
    testWidgets('timeline entries carry ValueKey(memory.id)', (tester) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.complete([
        _memory('mem-42', 'keyed'),
        _memory('mem-43', 'also'),
      ]);
      await tester.pump();
      await tester.pump();

      expect(find.byKey(const ValueKey('mem-42')), findsOneWidget);
      expect(find.byKey(const ValueKey('mem-43')), findsOneWidget);
    });
  });
}
