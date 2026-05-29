@TestOn('browser')
// Widget tests for MemoryTimelineEntry: timestamp framing, scene tags,
// annotation, and the "saved by" badge (klukai vs commander).
//
// Imports main.dart (GFL2Colors) -> package:web, so run under chrome.
// NOTE: initState kicks off an HTTP thumbnail fetch. We point it at an
// unroutable URL so the fetch fails fast and the widget settles on its
// fallback thumbnail — the text/badge logic under test is unaffected.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/memory_timeline_entry.dart';
import 'package:companion_app/models/memory.dart';

Widget _wrap(Widget child) => MaterialApp(
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

const _badUrl = 'http://127.0.0.1:1'; // closed port -> fetch fails immediately

Memory _memory({
  String keptBy = 'klukai',
  String? annotation,
  List<String> sceneTags = const [],
  DateTime? createdAt,
}) {
  return Memory(
    id: 'mem-1',
    annotation: annotation,
    sceneTags: sceneTags,
    keptBy: keptBy,
    createdAt: createdAt ?? DateTime(2026, 4, 7, 9, 5),
  );
}

void main() {
  group('MemoryTimelineEntry timestamp', () {
    testWidgets('formats as "MON D // HH:MM"', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(createdAt: DateTime(2026, 4, 7, 9, 5)),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump(); // let the failed fetch settle
      expect(find.text('APR 7 // 09:05'), findsOneWidget);
    });

    testWidgets('pads single-digit minutes/hours', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(createdAt: DateTime(2026, 12, 25, 3, 9)),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump();
      expect(find.text('DEC 25 // 03:09'), findsOneWidget);
    });
  });

  group('MemoryTimelineEntry saved-by badge', () {
    testWidgets('keptBy "klukai" -> SAVED BY KLUKAI', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(keptBy: 'klukai'),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump();
      expect(find.text('SAVED BY KLUKAI'), findsOneWidget);
      expect(find.text('SAVED BY COMMANDER'), findsNothing);
    });

    testWidgets('keptBy "commander" -> SAVED BY COMMANDER', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(keptBy: 'commander'),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump();
      expect(find.text('SAVED BY COMMANDER'), findsOneWidget);
      expect(find.text('SAVED BY KLUKAI'), findsNothing);
    });
  });

  group('MemoryTimelineEntry content', () {
    testWidgets('renders the annotation in quotes when present',
        (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(annotation: 'Snow over the depot.'),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump();
      expect(find.text('"Snow over the depot."'), findsOneWidget);
    });

    testWidgets('renders scene tags', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(sceneTags: const ['snow', 'depot', 'dawn']),
        serverUrl: _badUrl,
        authToken: 't',
      )));
      await tester.pump();
      expect(find.text('snow'), findsOneWidget);
      expect(find.text('depot'), findsOneWidget);
      expect(find.text('dawn'), findsOneWidget);
    });

    testWidgets('caps compact tags at 3', (tester) async {
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(sceneTags: const ['a', 'b', 'c', 'd', 'e']),
        serverUrl: _badUrl,
        authToken: 't',
        isCompact: true,
      )));
      await tester.pump();
      expect(find.text('a'), findsOneWidget);
      expect(find.text('c'), findsOneWidget);
      // 4th and 5th tags are dropped in compact mode.
      expect(find.text('d'), findsNothing);
      expect(find.text('e'), findsNothing);
    });

    testWidgets('fires onTap when tapped', (tester) async {
      var tapped = false;
      await tester.pumpWidget(_wrap(MemoryTimelineEntry(
        memory: _memory(annotation: 'tap me'),
        serverUrl: _badUrl,
        authToken: 't',
        onTap: () => tapped = true,
      )));
      await tester.pump();
      await tester.tap(find.text('"tap me"'));
      await tester.pump();
      expect(tapped, isTrue);
    });
  });
}
