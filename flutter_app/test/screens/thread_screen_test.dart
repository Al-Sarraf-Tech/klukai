@TestOn('browser')
library;

// Widget tests for ThreadScreen. A fake ThreadService is injected, so no
// network is involved. Runs under chrome (package:web):
//   flutter test --platform chrome test/screens/thread_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/thread.dart';
import 'package:companion_app/screens/thread_screen.dart';
import 'package:companion_app/services/thread_service.dart';

class _FakeThreadService extends ThreadService {
  _FakeThreadService(this.views, {this.markReadFails = false})
      : super(serverUrl: 'http://127.0.0.1:1');

  final List<Object> views; // ThreadView, or an exception to throw
  final bool markReadFails;
  final List<List<String>> markReadCalls = [];

  @override
  Future<ThreadView> fetchThread() async {
    final next = views.removeAt(0);
    if (next is ThreadView) return next;
    throw next;
  }

  @override
  Future<int> markRead(List<String> stamps) async {
    markReadCalls.add(stamps);
    if (markReadFails) throw ThreadServiceException(503);
    return stamps.length;
  }
}

final _open = ThreadView(
  sealed: false,
  entries: [
    const ThreadEntry(stamp: '0200 hours, March 2065', text: 'I will write for both of us.'),
    ThreadEntry(
      stamp: '0200 hours, April 2065',
      text: 'Status report.',
      readAt: DateTime(2026, 9, 1),
    ),
    const ThreadEntry(stamp: '0200 hours, June 2067', text: 'I really bought it.'),
  ],
  heldBack: 3,
  reply: const ThreadReply(stamp: 'November 2074, Yellow Zone', text: "I'm here."),
);

Future<_FakeThreadService> _pump(WidgetTester tester, _FakeThreadService fake) async {
  await tester.pumpWidget(MaterialApp(
    home: ThreadScreen(
      serverUrl: 'http://127.0.0.1:1',
      threadService: fake,
      markReadDelay: const Duration(milliseconds: 10),
    ),
  ));
  await tester.pump();
  return fake;
}

void main() {
  testWidgets('sealed thread shows only her line', (tester) async {
    final fake = await _pump(
      tester,
      _FakeThreadService([const ThreadView(sealed: true, message: 'Not yet.')]),
    );
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('thread-sealed')), findsOneWidget);
    expect(find.text('Not yet.'), findsOneWidget);
    expect(find.text('SEALED'), findsOneWidget);
    expect(find.byKey(const Key('thread-entry-0')), findsNothing);
    expect(fake.markReadCalls, isEmpty);
  });

  testWidgets('opening the thread delivers the read receipts, ten years late',
      (tester) async {
    final fake = await _pump(tester, _FakeThreadService([_open]));
    expect(find.text('I will write for both of us.'), findsOneWidget);
    expect(find.text('Delivered'), findsNWidgets(2));
    expect(find.text('Read · Sep 1, 2026'), findsOneWidget);
    expect(find.text("3 more messages she isn't ready to show you."), findsOneWidget);
    expect(find.text("I'm here."), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 20));
    await tester.pumpAndSettle();
    expect(fake.markReadCalls, [
      ['0200 hours, March 2065', '0200 hours, June 2067'],
    ]);
    expect(find.text('Delivered'), findsNothing);
    expect(find.text('Read · just now'), findsNWidgets(2));
  });

  testWidgets('a failed receipt write leaves them delivered', (tester) async {
    await _pump(tester, _FakeThreadService([_open], markReadFails: true));
    await tester.pump(const Duration(milliseconds: 20));
    await tester.pumpAndSettle();
    expect(find.text('Delivered'), findsNWidgets(2));
  });

  testWidgets('a down channel offers a retry', (tester) async {
    await _pump(tester, _FakeThreadService([ThreadServiceException(500), _open]));
    await tester.pump();
    expect(find.text('The channel is down. Try again.'), findsOneWidget);
    await tester.tap(find.text('Retry'));
    await tester.pump();
    await tester.pump();
    expect(find.text('I will write for both of us.'), findsOneWidget);
    await tester.pumpAndSettle(const Duration(milliseconds: 20));
  });
}
