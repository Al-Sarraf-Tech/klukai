@TestOn('browser')
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
  List<MemoryCategory> categories = [];
  List<MonthGroup> timeline = [];

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
  }) {
    final completer = Completer<List<Memory>>();
    memoryCalls.add(completer);
    return completer.future;
  }

  @override
  Future<List<MemoryCategory>> fetchCategories() async => categories;

  @override
  Future<List<MonthGroup>> fetchTimeline() async => timeline;
}

Memory _memory(String id, String annotation) =>
    Memory(id: id, annotation: annotation, createdAt: DateTime(2026, 4, 7));

Future<_FakeMemoryService> _pumpArchive(WidgetTester tester) async {
  final fake = _FakeMemoryService();
  await tester.pumpWidget(
    MaterialApp(
      home: MemoryArchiveScreen(
        serverUrl: 'http://127.0.0.1:1',
        memoryService: fake,
      ),
    ),
  );
  await tester.pump();
  return fake;
}

void main() {
  group('MemoryArchiveScreen — error is NOT a fake-empty archive', () {
    testWidgets('server error shows SIGNAL LOST + RETRY, never the empty state',
        (tester) async {
      final fake = await _pumpArchive(tester);
      expect(fake.memoryCalls, hasLength(1));

      fake.memoryCalls.single
          .completeError(MemoryServiceException(500, '/api/memories'));
      await tester.pump();
      await tester.pump();

      expect(find.text('SIGNAL LOST // ARCHIVE UNREACHABLE'), findsOneWidget);
      expect(find.text('RETRY'), findsOneWidget);
      expect(find.text('NO MEMORIES ARCHIVED YET.'), findsNothing);
    });

    testWidgets('network failure (non-HTTP exception) also shows RETRY',
        (tester) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single.completeError(Exception('connection refused'));
      await tester.pump();
      await tester.pump();

      expect(find.text('RETRY'), findsOneWidget);
      expect(find.text('NO MEMORIES ARCHIVED YET.'), findsNothing);
    });

    testWidgets('RETRY refetches and renders the recovered memories',
        (tester) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single
          .completeError(MemoryServiceException(502, '/api/memories'));
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

    testWidgets('a genuinely empty archive still shows the empty state',
        (tester) async {
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
    });
  });

  group('MemoryArchiveScreen — entries are keyed by memory id', () {
    testWidgets('timeline entries carry ValueKey(memory.id)', (tester) async {
      final fake = await _pumpArchive(tester);
      fake.memoryCalls.single
          .complete([_memory('mem-42', 'keyed'), _memory('mem-43', 'also')]);
      await tester.pump();
      await tester.pump();

      expect(find.byKey(const ValueKey('mem-42')), findsOneWidget);
      expect(find.byKey(const ValueKey('mem-43')), findsOneWidget);
    });
  });
}
