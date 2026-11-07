@TestOn('browser')
library;

import 'dart:convert';
import 'package:companion_app/screens/chat_screen.dart';
import 'package:companion_app/screens/conversation_search_screen.dart';
import 'package:companion_app/services/voice_settings_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:web/web.dart' as web;
import '../support/fake_browser.dart';
import '../support/her_day_fixtures.dart';
import '../support/journey_socket.dart';

void main() {
  testWidgets(
    'search pushes over chat and preserves draft, socket and incoming message',
    (tester) async {
      try {
        final socket = JourneySocket();
        var searched = 0;
        web.window.localStorage.setItem(
          'klukai_token',
          'synthetic-search-navigation',
        );
        final client = MockClient((request) async {
          if (request.url.path == '/api/messages/search') {
            searched++;
            expect(request.method, 'POST');
            expect(jsonDecode(request.body)['query'], 'rain');
            return http.Response('{"messages":[],"next_cursor":null}', 200);
          }
          if (request.url.path == '/api/affection') {
            return http.Response('{"score":0,"level":0}', 200);
          }
          if (request.url.path == '/api/voice/settings') {
            return http.Response('{"language":"en"}', 200);
          }
          return http.Response('{"messages":[]}', 200);
        });
        await tester.pumpWidget(
          MaterialApp(
            home: ChatScreen(
              serverUrl: 'https://fixture.invalid',
              webSocketService: socket,
              browser: FakeBrowserPlatform(),
              wardrobeService: FakeWardrobeService(),
              historyClient: client,
              httpClient: client,
              voiceSettings: VoiceSettingsService(
                serverUrl: 'https://fixture.invalid',
                client: client,
              ),
            ),
          ),
        );
        await tester.pump();
        await tester.pump();
        await tester.enterText(find.byType(TextField), 'Unsent draft 雨');
        await tester.tap(find.byKey(const Key('chat-menu')));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        await tester.tap(find.byKey(const Key('chat-menu-search')));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        await tester.pump();
        expect(find.byType(ConversationSearchScreen), findsOneWidget);
        expect(find.byType(ChatScreen, skipOffstage: false), findsOneWidget);
        socket.receive({
          'type': 'proactive',
          'message': 'Still here while you searched.',
        });
        await tester.pump();
        await tester.enterText(
          find.byKey(const Key('conversation-query')),
          'rain',
        );
        await tester.pump();
        await tester.tap(find.byKey(const Key('conversation-submit')));
        await tester.pump();
        await tester.pump();
        expect(searched, 1);
        await tester.pageBack();
        await tester.pump();
        await tester.pump(const Duration(seconds: 2));
        await tester.pump();
        expect(find.byType(ConversationSearchScreen), findsNothing);
        expect(
          tester
              .widget<TextField>(
                find.descendant(
                  of: find.byType(ChatScreen),
                  matching: find.byType(TextField),
                ),
              )
              .controller!
              .text,
          'Unsent draft 雨',
        );
        expect(
          find.textContaining('Still here while you searched.'),
          findsOneWidget,
        );
        expect(socket.isConnected, isTrue);
        expect(
          socket.sent.where((frame) => frame['type'] == 'message'),
          isEmpty,
        );
        await tester.pumpWidget(const SizedBox());
        web.window.localStorage.removeItem('klukai_token');
      } catch (error, stack) {
        // ignore: avoid_print
        print('SEARCH_NAVIGATION_FAILURE: $error\n$stack');
        rethrow;
      } finally {
        await tester.pumpWidget(const SizedBox());
        web.window.localStorage.removeItem('klukai_token');
      }
    },
  );
}
