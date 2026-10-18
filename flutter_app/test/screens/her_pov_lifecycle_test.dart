@TestOn('browser')
library;

import 'dart:async';

import 'package:companion_app/models/memory.dart';
import 'package:companion_app/screens/her_pov_screen.dart';
import 'package:companion_app/services/memory_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

class _Gallery extends MemoryService {
  _Gallery() : super(serverUrl: 'http://fixture.invalid');

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
    String? beforeId,
  }) async => [];
}

Future<void> _screen(WidgetTester tester, http.Client client) async {
  tester.view.physicalSize = const Size(1200, 900);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      home: HerPovScreen(
        serverUrl: 'http://fixture.invalid',
        affectionLevel: 0,
        affectionLevelName: 'Cold Assessment',
        memoryService: _Gallery(),
        httpClient: client,
      ),
    ),
  );
  await tester.pump();
}

void lifecycleTest(String name, WidgetTesterCallback body) {
  testWidgets(name, (tester) async {
    try {
      await body(tester);
    } catch (error) {
      // ignore: avoid_print
      print('LIFECYCLE_FAILURE: $error');
      rethrow;
    }
  });
}

void main() {
  lifecycleTest(
    'accepted POST after disposal never creates a poll or resubmits',
    (tester) async {
      final accepted = Completer<http.Response>();
      final requests = <http.Request>[];
      final client = MockClient((request) async {
        requests.add(request);
        if (request.method == 'POST') return accepted.future;
        return http.Response(
          '{"phase":"done","memory_id":"accepted-image"}',
          200,
        );
      });
      await _screen(tester, client);
      await tester.tap(find.text('Find a moment'));
      await tester.pump();
      expect(find.text('Working…'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
      accepted.complete(
        http.Response('{"job_id":"accepted-original-id"}', 202),
      );
      await tester.pump();
      await tester.pump(const Duration(seconds: 8));
      expect(
        requests.map((request) => request.method),
        ['POST'],
        reason:
            'disposal detaches UI; the accepted server job is neither polled, cancelled nor resubmitted',
      );
      expect(requests.single.url.path, '/api/memories/her-pov');
      expect(tester.takeException(), isNull);
    },
  );

  lifecycleTest(
    'one job has one in-flight poll even across multiple timer periods',
    (tester) async {
      final polls = <Completer<http.Response>>[];
      final requests = <http.Request>[];
      final client = MockClient((request) async {
        requests.add(request);
        if (request.method == 'POST') {
          return http.Response('{"job_id":"job-a"}', 202);
        }
        final response = Completer<http.Response>();
        polls.add(response);
        return response.future;
      });
      await _screen(tester, client);
      try {
        await tester.tap(find.text('Find a moment'));
        await tester.pump();
        expect(polls, hasLength(1));
        await tester.pump(const Duration(seconds: 6));
        expect(
          polls,
          hasLength(1),
          reason: 'a slow response must not acquire overlapping poll owners',
        );
        expect(find.text('Working…'), findsOneWidget);
      } finally {
        for (final response in polls) {
          response.complete(
            http.Response(
              '{"phase":"failed","message":"Stopped fixture"}',
              200,
            ),
          );
        }
        await tester.pump();
        await tester.pumpWidget(const SizedBox());
      }
      expect(
        requests.where((request) => request.method == 'POST'),
        hasLength(1),
      );
      expect(
        requests
            .where((request) => request.method == 'GET')
            .map((request) => request.url.path),
        ['/api/memories/her-pov/job-a'],
      );
    },
  );

  lifecycleTest('a new accepted job keeps its own progress and poll identity', (
    tester,
  ) async {
    var submissions = 0;
    final polledIds = <String>[];
    final bFirst = Completer<http.Response>();
    final client = MockClient((request) async {
      if (request.method == 'POST') {
        submissions++;
        return http.Response(
          '{"job_id":"job-${submissions == 1 ? 'a' : 'b'}"}',
          202,
        );
      }
      final id = request.url.pathSegments.last;
      polledIds.add(id);
      if (id == 'job-a') {
        return http.Response(
          '{"phase":"failed","message":"A could not finish."}',
          200,
        );
      }
      if (polledIds.where((id) => id == 'job-b').length == 1) {
        return bFirst.future;
      }
      return http.Response(
        '{"phase":"drawing","message":"B is still drawing."}',
        200,
      );
    });
    await _screen(tester, client);
    await tester.tap(find.text('Find a moment'));
    await tester.pump();
    expect(find.text('A could not finish.'), findsOneWidget);
    await tester.tap(find.text('Find a moment'));
    await tester.pump();
    bFirst.complete(
      http.Response('{"phase":"drawing","message":"B is drawing."}', 200),
    );
    await tester.pump();
    expect(find.text('B is drawing.'), findsOneWidget);
    expect(find.text('A could not finish.'), findsNothing);
    await tester.pump(const Duration(seconds: 2));
    expect(find.text('B is still drawing.'), findsOneWidget);
    expect(find.text('Working…'), findsOneWidget);
    expect(submissions, 2);
    expect(polledIds, ['job-a', 'job-b', 'job-b']);
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 6));
    expect(polledIds, ['job-a', 'job-b', 'job-b']);
  });
}
