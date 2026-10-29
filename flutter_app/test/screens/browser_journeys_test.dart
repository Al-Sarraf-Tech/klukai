@TestOn('browser')
library;

import 'dart:async';
import 'dart:convert';

import 'package:companion_app/models/memory.dart';
import 'package:companion_app/screens/chat_screen.dart';
import 'package:companion_app/screens/her_pov_screen.dart';
import 'package:companion_app/services/memory_service.dart';
import 'package:companion_app/services/speech_capture.dart';
import 'package:companion_app/services/voice_settings_service.dart';
import 'package:companion_app/widgets/chat_composer_bar.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/widgets/voice_button.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:web/web.dart' as web;

import '../support/fake_browser.dart';
import '../support/her_day_fixtures.dart';
import '../support/journey_socket.dart';

class _Gallery extends MemoryService {
  _Gallery() : super(serverUrl: 'http://fixture.invalid');
  bool unavailable = true;

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
    String? beforeId,
  }) async {
    if (unavailable) throw MemoryServiceException(503, '/api/memories');
    return [];
  }
}

Future<void> _chat(
  WidgetTester tester,
  JourneySocket socket, {
  http.Client? history,
  http.Client? speech,
  SpeechBridge? bridge,
  VoidCallback? revoked,
}) async {
  web.window.localStorage.setItem('klukai_token', 'isolated-journey-token');
  await tester.pumpWidget(
    MaterialApp(
      home: ChatScreen(
        serverUrl: 'http://fixture.invalid',
        webSocketService: socket,
        browser: FakeBrowserPlatform(),
        wardrobeService: FakeWardrobeService(),
        historyClient:
            history ??
            MockClient(
              (request) async => http.Response(
                request.url.path == '/api/affection'
                    ? '{"score":0,"level":0}'
                    : '{"messages":[]}',
                200,
              ),
            ),
        httpClient: speech,
        speechBridge: bridge,
        voiceInputEnabled: bridge != null,
        voiceSettings: VoiceSettingsService(
          serverUrl: 'http://fixture.invalid',
          client: MockClient(
            (_) async => http.Response('{"language":"en"}', 200),
          ),
        ),
        onAuthExpired: revoked,
      ),
    ),
  );
  await tester.pump();
  await tester.pump();
}

void journeyTest(String name, WidgetTesterCallback body) {
  testWidgets(name, (tester) async {
    try {
      await body(tester);
    } catch (error) {
      // Browser Flutter exceptions otherwise lose their details in machine logs.
      // ignore: avoid_print
      print('JOURNEY_FAILURE: $error');
      rethrow;
    }
  });
}

void main() {
  tearDown(() => web.window.localStorage.removeItem('klukai_token'));

  journeyTest(
    'reconnect freezes the interrupted reply and restores missed history once',
    (tester) async {
      final socket = JourneySocket();
      var restored = false;
      final history = MockClient(
        (request) async => http.Response(
          jsonEncode(
            request.url.path == '/api/affection'
                ? {'score': 0, 'level': 0}
                : {
                    'messages': restored
                        ? [
                            {
                              'id': 'missed-1',
                              'role': 'assistant',
                              'content': 'I kept the watch.',
                              'created_at': '2026-10-15T10:00:00Z',
                            },
                          ]
                        : [],
                  },
          ),
          200,
        ),
      );
      await _chat(tester, socket, history: history);
      socket.receive({'type': 'token', 'text': 'Hold position.'});
      await tester.pump();
      expect(tester.widget<TextField>(find.byType(TextField)).readOnly, isTrue);
      socket.link(false);
      await tester.pump();
      expect(find.text('LINK DOWN'), findsOneWidget);
      expect(
        tester
            .widget<MessageBubble>(find.byType(MessageBubble))
            .message
            .isStreaming,
        isFalse,
      );
      expect(
        tester
            .widget<HudSquareButton>(find.byKey(const Key('composer-send')))
            .onTap,
        isNull,
      );
      restored = true;
      socket.link(true);
      await tester.pump();
      await tester.pump();
      expect(find.text('ONLINE'), findsOneWidget);
      expect(find.textContaining('I kept the watch.'), findsOneWidget);
      expect(
        tester.widget<TextField>(find.byType(TextField)).readOnly,
        isFalse,
      );
      socket.link(false);
      socket.link(true);
      await tester.pump();
      await tester.pump();
      expect(find.textContaining('I kept the watch.'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    },
  );

  journeyTest(
    'revocation clears credentials and prevents late frames or redials reactivating chat',
    (tester) async {
      final socket = JourneySocket();
      var loginRedirects = 0;
      await _chat(tester, socket, revoked: () => loginRedirects++);
      socket.revoke();
      await tester.pump();
      expect(web.window.localStorage.getItem('klukai_token'), isNull);
      expect(loginRedirects, 1);
      expect(find.text('LINK DOWN'), findsOneWidget);
      expect(
        tester
            .widget<HudSquareButton>(find.byKey(const Key('composer-send')))
            .onTap,
        isNull,
      );
      socket.link(true);
      socket.receive({'type': 'token', 'text': 'unauthorized late reply'});
      await tester.pump();
      expect(find.text('LINK DOWN'), findsOneWidget);
      expect(find.textContaining('unauthorized late reply'), findsNothing);
      await tester.pumpWidget(const SizedBox());
    },
  );

  journeyTest(
    'Escape cancels a pending transcript and typed chat remains usable',
    (tester) async {
      final socket = JourneySocket();
      final transcript = Completer<http.Response>();
      await _chat(
        tester,
        socket,
        speech: MockClient((_) => transcript.future),
        bridge: SpeechBridge(start: () async => true, stop: () async => 'clip'),
      );
      await tester.enterText(find.byType(TextField), 'My own words');
      await tester.pump();
      final hold = await tester.startGesture(
        tester.getCenter(find.byType(VoiceButton)),
      );
      await tester.pump();
      await hold.up();
      await tester.pump();
      await tester.sendKeyDownEvent(LogicalKeyboardKey.escape);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.escape);
      transcript.complete(
        http.Response('{"text":"discard this late transcript"}', 200),
      );
      await tester.pump();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'My own words',
      );
      await tester.tap(find.byTooltip('Send'));
      await tester.pump();
      expect(
        socket.sent
            .where((frame) => frame['type'] == 'message')
            .map((frame) => frame['content']),
        ['My own words'],
      );
      expect(find.textContaining('discard this late transcript'), findsNothing);
      await tester.pumpWidget(const SizedBox());
    },
  );

  journeyTest(
    'releasing during permission keeps a late recorder stopped and text usable',
    (tester) async {
      final socket = JourneySocket();
      final permission = Completer<bool>();
      var recorderLive = false;
      await _chat(
        tester,
        socket,
        bridge: SpeechBridge(
          start: () async {
            final allowed = await permission.future;
            recorderLive = allowed;
            return allowed;
          },
          stop: () async {
            recorderLive = false;
            return null;
          },
        ),
      );
      final hold = await tester.startGesture(
        tester.getCenter(find.byType(VoiceButton)),
      );
      await tester.pump();
      await hold.up();
      await tester.pump();
      permission.complete(true);
      await tester.pump();
      expect(
        recorderLive,
        isFalse,
        reason: 'release also cancels late permission',
      );
      await tester.enterText(
        find.byType(TextField),
        'Permission was cancelled',
      );
      await tester.pump();
      await tester.tap(find.byTooltip('Send'));
      await tester.pump();
      expect(socket.sent.last['content'], 'Permission was cancelled');
      await tester.pumpWidget(const SizedBox());
    },
  );

  journeyTest(
    'archive outage is retryable and image provider failure restores the action',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final gallery = _Gallery();
      var requests = 0;
      final provider = MockClient((request) async {
        requests++;
        return request.method == 'POST'
            ? http.Response('{"job_id":"accepted-1"}', 202)
            : http.Response(
                '{"phase":"failed","message":"Image service unavailable. Try later."}',
                200,
              );
      });
      await tester.pumpWidget(
        MaterialApp(
          home: HerPovScreen(
            serverUrl: 'http://fixture.invalid',
            affectionLevel: 0,
            affectionLevelName: 'Cold Assessment',
            memoryService: gallery,
            httpClient: provider,
          ),
        ),
      );
      await tester.pump();
      expect(find.text('Portraits unavailable'), findsOneWidget);
      expect(find.byKey(const Key('gallery-empty')), findsNothing);
      gallery.unavailable = false;
      await tester.tap(find.byKey(const Key('gallery-retry')));
      await tester.pump();
      expect(find.byKey(const Key('gallery-empty')), findsOneWidget);
      await tester.tap(find.text('Find a moment'));
      await tester.pump();
      await tester.pump();
      expect(requests, 2);
      expect(
        find.text('Image service unavailable. Try later.'),
        findsOneWidget,
      );
      expect(find.text('Find a moment'), findsOneWidget);
      expect(find.text('Working…'), findsNothing);
      await tester.pump(const Duration(seconds: 6));
      expect(
        requests,
        2,
        reason: 'terminal provider failure must cancel polling',
      );
      await tester.pumpWidget(const SizedBox());
    },
  );
}
