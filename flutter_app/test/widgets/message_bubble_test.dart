@TestOn('browser')
library;

// Widget tests for MessageBubble: user vs companion styling, read/sent status
// ticks, streaming cursor, comm tag, and image-vs-text content.
//
// message_bubble.dart uses dart:js_interop + package:web (in tap handlers) and
// imports main.dart, so this MUST run under chrome:
//   flutter test --platform chrome test/widgets/message_bubble_test.dart
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'package:flutter/services.dart';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/models/message.dart';

import '../support/fake_browser.dart';

const _voice = Key('voice-play');

Widget _wrap(Widget child) => MaterialApp(
  home: Scaffold(body: SizedBox(width: 400, child: child)),
);

// 1x1 transparent PNG.
const _pngB64 =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

ChatMessage _msg({
  String role = 'assistant',
  String content = 'Hello, Commander.',
  String status = 'read',
  bool isStreaming = false,
  String? imageData,
}) {
  return ChatMessage(
    id: 'm1',
    role: role,
    content: content,
    status: status,
    isStreaming: isStreaming,
    imageData: imageData,
    createdAt: DateTime(2026, 5, 1, 14, 7),
  );
}

void main() {
  testWidgets(
    'mixed Markdown remains selectable with numbered lists and tables',
    (tester) async {
      final bridge = JSObject();
      bridge['parseMarkdown'] = ((String source) => jsonEncode([
        {
          'type': 'blockquote',
          'raw': '> Hold position.\n',
          'tokens': [
            {
              'type': 'paragraph',
              'text': 'Hold position.',
              'tokens': [
                {'type': 'text', 'text': 'Hold position.'},
              ],
            },
          ],
        },
        {
          'type': 'list',
          'ordered': true,
          'start': 3,
          'items': [
            {
              'type': 'list_item',
              'text': 'Three',
              'tokens': [
                {'type': 'text', 'text': 'Three'},
              ],
            },
            {
              'type': 'list_item',
              'text': 'Four',
              'tokens': [
                {'type': 'text', 'text': 'Four'},
              ],
            },
          ],
        },
        {
          'type': 'table',
          'header': [
            {
              'text': 'Name',
              'tokens': [
                {'type': 'text', 'text': 'Name'},
              ],
            },
          ],
          'rows': [
            [
              {
                'text': 'Klukai',
                'tokens': [
                  {'type': 'text', 'text': 'Klukai'},
                ],
              },
            ],
          ],
          'align': [null],
        },
      ]).toJS).toJS;
      globalContext['pretextBridge'] = bridge;
      addTearDown(() => globalContext.delete('pretextBridge'.toJS));
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(
              content:
                  '> Hold position.\n\n3. Three\n4. Four\n\n| Name |\n| --- |\n| Klukai |',
            ),
          ),
        ),
      );
      final text = tester
          .widgetList<SelectableText>(find.byType(SelectableText))
          .map((w) => w.data ?? w.textSpan!.toPlainText())
          .join(' ');
      expect(tester.takeException(), isNull);
      expect(text, contains('Hold position.'));
      expect(text, contains('Three'));
      expect(text, contains('Four'));
      expect(text, contains('Klukai'));
      expect(find.text('3.'), findsOneWidget);
      expect(find.text('4.'), findsOneWidget);
      expect(find.byType(Table), findsOneWidget);
    },
  );

  group('MessageBubble — companion (assistant) messages', () {
    testWidgets('shows the ▸ KLUKAI tag and a ▶ VOICE control', (tester) async {
      await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
      await tester.pump();
      expect(find.text('▸ KLUKAI'), findsOneWidget);
      // Idle VOICE is shown for non-streaming companion messages.
      expect(find.byKey(_voice), findsOneWidget);
      expect(find.text('VOICE'), findsOneWidget);
      // Companion messages have no read/sent ticks.
      expect(find.byIcon(Icons.done_all), findsNothing);
      expect(find.byIcon(Icons.done), findsNothing);
    });

    testWidgets('renders the message content as selectable text', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(MessageBubble(message: _msg(content: 'Mission ready.'))),
      );
      await tester.pump();
      expect(find.text('Mission ready.'), findsOneWidget);
      expect(find.byType(SelectableText), findsOneWidget);
    });

    testWidgets('renders the formatted HH:MM timestamp', (tester) async {
      await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
      await tester.pump();
      expect(find.text('14:07'), findsOneWidget);
    });
  });

  group('MessageBubble — user messages', () {
    testWidgets('omits the comm tag and the speaker icon', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', content: 'hi'),
          ),
        ),
      );
      await tester.pump();
      expect(find.text('▸ KLUKAI'), findsNothing);
      expect(find.byKey(_voice), findsNothing);
    });

    testWidgets('read status shows double-tick (done_all)', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', status: 'read'),
          ),
        ),
      );
      await tester.pump();
      expect(find.byIcon(Icons.done_all), findsOneWidget);
      expect(find.byIcon(Icons.done), findsNothing);
    });

    testWidgets('non-read status shows single-tick (done)', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', status: 'sent'),
          ),
        ),
      );
      await tester.pump();
      expect(find.byIcon(Icons.done), findsOneWidget);
      expect(find.byIcon(Icons.done_all), findsNothing);
    });
  });

  group('MessageBubble — streaming', () {
    testWidgets('while streaming, hides timestamp footer (no ticks/speaker)', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(message: _msg(isStreaming: true, content: 'typing...')),
        ),
      );
      await tester.pump();
      // VOICE and the timestamp wait until the line is complete.
      expect(find.byKey(_voice), findsNothing);
      expect(find.text('14:07'), findsNothing);
      // Content is still visible.
      expect(find.text('typing...'), findsOneWidget);
    });
  });

  group('MessageBubble — image content', () {
    testWidgets(
      'preserves the caption and complete artwork when imageData present',
      (tester) async {
        await tester.pumpWidget(
          _wrap(
            MessageBubble(
              message: _msg(content: 'see attached', imageData: _pngB64),
            ),
          ),
        );
        await tester.pump();
        expect(find.byType(Image), findsOneWidget);
        // Image captions remain selectable.
        expect(find.text('see attached'), findsOneWidget);
        expect(tester.widget<Image>(find.byType(Image)).fit, BoxFit.contain);
        expect(find.byTooltip('View full artwork'), findsOneWidget);
        expect(find.byTooltip('Download artwork'), findsOneWidget);
        // A labeled download control follows the image.
        expect(find.byIcon(Icons.download), findsOneWidget);
      },
    );
  });

  testWidgets('artwork shows loading while its image decoder is pending', (
    tester,
  ) async {
    await tester.pumpWidget(
      _wrap(MessageBubble(message: _msg(imageData: _pngB64))),
    );
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
  });

  group('MessageBubble — her voice (speaker control)', () {
    const offline = '// VOICE SYNTH OFFLINE — her words stand.';

    testWidgets('a failed TTS request drops the spinner and says so', (
      tester,
    ) async {
      // The GPU host is routinely powered off: /api/tts then 502s (or hangs —
      // the request is bounded by a timeout). Neither may leave the speaker
      // spinning, and neither may fail in silence.
      final gate = Completer<http.Response>();
      final client = MockClient((req) {
        expect(req.url.path, '/api/tts');
        return gate.future;
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(),
            client: client,
            browser: FakeBrowserPlatform(),
          ),
        ),
      );
      await tester.pump();

      await tester.tap(find.byKey(_voice));
      await tester.pump();
      // Request in flight: VOICE is a spinner.
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.text('SYNTH'), findsOneWidget);

      gate.complete(http.Response('voice service unavailable', 502));
      await tester.pump();
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.text('VOICE'), findsOneWidget);
      expect(find.text(offline), findsOneWidget);
      // Let the snackbar run its course.
      await tester.pump(const Duration(seconds: 5));
    });

    testWidgets(
      'reusing a bubble cancels stale synthesis without resetting new playback',
      (tester) async {
        final oldAudio = Completer<http.Response>();
        final newAudio = Completer<http.Response>();
        final browser = FakeBrowserPlatform();
        final client = MockClient(
          (request) => jsonDecode(request.body)['text'] == 'Old reply'
              ? oldAudio.future
              : newAudio.future,
        );
        await tester.pumpWidget(
          _wrap(
            MessageBubble(
              message: ChatMessage(
                id: 'old',
                role: 'assistant',
                content: 'Old reply',
              ),
              client: client,
              browser: browser,
            ),
          ),
        );
        await tester.tap(find.byKey(_voice));
        await tester.pump();
        expect(find.text('SYNTH'), findsOneWidget);
        await tester.pumpWidget(
          _wrap(
            MessageBubble(
              message: ChatMessage(
                id: 'new',
                role: 'assistant',
                content: 'New reply',
              ),
              client: client,
              browser: browser,
            ),
          ),
        );
        expect(find.text('VOICE'), findsOneWidget);
        await tester.tap(find.byKey(_voice));
        await tester.pump();
        oldAudio.complete(http.Response('{"audio":"b2xk"}', 200));
        await tester.pump();
        await tester.pump();
        expect(browser.voices, isEmpty);
        expect(find.text('SYNTH'), findsOneWidget);
        newAudio.complete(http.Response('{"audio":"bmV3"}', 200));
        await tester.pump();
        await tester.pump();
        expect(browser.voices, ['data:audio/wav;base64,bmV3']);
        expect(find.text(offline), findsNothing);
      },
    );

    testWidgets(
      'same text on a different reply still cancels pending synthesis',
      (tester) async {
        final calls = <String>[];
        final browser = FakeBrowserPlatform();
        final client = MockClient((request) async {
          calls.add('${request.method} ${request.url.path}');
          if (request.method == 'POST') {
            return http.Response('{"job_id":"old","status":"queued"}', 202);
          }
          if (request.method == 'DELETE') return http.Response('{}', 200);
          return http.Response('{"audio":"b2xk"}', 200);
        });
        Widget bubble(String id) => _wrap(
          MessageBubble(
            message: ChatMessage(
              id: id,
              role: 'assistant',
              content: 'Same words.',
            ),
            client: client,
            browser: browser,
          ),
        );
        await tester.pumpWidget(bubble('streaming-old'));
        await tester.tap(find.byKey(_voice));
        await tester.pump();
        await tester.pump();
        expect(find.text('SYNTH'), findsOneWidget);
        // Identical words and a canonical-looking ID do not prove promotion.
        await tester.pumpWidget(bubble('00000000-0000-0000-0000-000000000001'));
        await tester.pump();
        expect(calls, contains('DELETE /api/tts/jobs/old'));
        expect(find.text('VOICE'), findsOneWidget);
        await tester.pump(const Duration(seconds: 2));
        expect(browser.voices, isEmpty);
        expect(tester.takeException(), isNull);
      },
    );

    testWidgets('reusing a bubble keeps its previous injected client open', (
      tester,
    ) async {
      final audio = Completer<http.Response>();
      final client = _ClosingClient((_) => audio.future);
      final browser = FakeBrowserPlatform();
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: ChatMessage(
              id: 'old',
              role: 'assistant',
              content: 'Old reply',
            ),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: ChatMessage(
              id: 'new',
              role: 'assistant',
              content: 'New reply',
            ),
            browser: browser,
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      expect(
        client.closed,
        isFalse,
        reason: 'the injector retains client ownership',
      );
      audio.complete(http.Response('{"audio":"b2xk"}', 200));
      await tester.pump();
      expect(browser.voices, isEmpty);
    });

    testWidgets('a good TTS reply plays through the unlocked voice channel', (
      tester,
    ) async {
      final browser = FakeBrowserPlatform();
      final client = MockClient((req) async {
        expect(req.url.path, '/api/tts');
        expect(jsonDecode(req.body)['language'], 'en');
        return http.Response(jsonEncode({'audio': 'UklGRg=='}), 200);
      });
      await tester.pumpWidget(
        _wrap(MessageBubble(message: _msg(), client: client, browser: browser)),
      );
      await tester.pump();

      // Nothing plays until he taps.
      expect(browser.voices, isEmpty);
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(browser.voices, ['data:audio/wav;base64,UklGRg==']);
      expect(find.text(offline), findsNothing);
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    // "Voice synth works but stops after sentences": every sentence was its
    // own request, made only after the previous clip had finished playing.
    testWidgets('a reply of several sentences is one request, one clip', (
      tester,
    ) async {
      const reply =
          'Sit. The stars are clear tonight, Commander. Stay a while? '
          'I kept the coffee warm.';
      final browser = FakeBrowserPlatform();
      final posted = <String>[];
      final client = MockClient((req) async {
        posted.add(jsonDecode(req.body)['text'] as String);
        return http.Response(jsonEncode({'audio': 'UklGRg=='}), 200);
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(content: reply),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(posted, [reply]);
      expect(browser.voices, ['data:audio/wav;base64,UklGRg==']);
      expect(find.text(offline), findsNothing);
    });

    // Two clips of 300 characters: A plays while B is synthesized.
    final clipA = 'A${'a' * 298}.';
    final clipB = 'B${'b' * 298}.';

    /// Room for a 600-character line on one screen.
    void tall(WidgetTester tester) {
      tester.view.physicalSize = const Size(800, 1400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
    }

    testWidgets('a long reply makes the next clip while this one plays', (
      tester,
    ) async {
      tall(tester);
      final browser = _GatedBrowser();
      final posted = <String>[];
      final client = MockClient((req) async {
        final text = jsonDecode(req.body)['text'] as String;
        posted.add(text);
        final audio = base64Encode(utf8.encode(text.substring(0, 1)));
        return http.Response(jsonEncode({'audio': audio}), 200);
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(content: '$clipA $clipB'),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      // A is playing, and B was asked for without waiting for A to end.
      expect(browser.voices, ['data:audio/wav;base64,QQ==']);
      expect(posted, [clipA, clipB]);
      expect(find.text('▮▮'), findsOneWidget);

      browser.plays[0].complete(true);
      await tester.pump();
      await tester.pump();
      // B follows at once: no new request, no spinner in between.
      expect(browser.voices.last, 'data:audio/wav;base64,Qg==');
      expect(posted, hasLength(2));
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.text('▮▮'), findsOneWidget);

      browser.plays[1].complete(true);
      await tester.pump();
      await tester.pump();
      expect(find.text('▶'), findsOneWidget);
      expect(find.text(offline), findsNothing);
    });

    testWidgets('reusing a playing bubble cancels its queued next clip', (
      tester,
    ) async {
      tall(tester);
      final browser = _GatedBrowser();
      final calls = <String>[];
      final client = MockClient((request) async {
        calls.add('${request.method} ${request.url.path}');
        if (request.method == 'DELETE') return http.Response('{}', 200);
        if (request.method == 'GET') {
          return http.Response('{"status":"running"}', 200);
        }
        final text = jsonDecode(request.body)['text'] as String;
        return text.startsWith('B')
            ? http.Response('{"job_id":"old-next","status":"queued"}', 202)
            : http.Response(
                jsonEncode({'audio': text.startsWith('A') ? 'QQ==' : 'Qw=='}),
                200,
              );
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: ChatMessage(
              id: 'old',
              role: 'assistant',
              content: '$clipA $clipB',
            ),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(browser.voices, ['data:audio/wav;base64,QQ==']);
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: ChatMessage(
              id: 'new',
              role: 'assistant',
              content: 'Current reply',
            ),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.pump();
      expect(find.text('VOICE'), findsOneWidget);
      expect(calls, contains('DELETE /api/tts/jobs/old-next'));
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(browser.voices, [
        'data:audio/wav;base64,QQ==',
        'data:audio/wav;base64,Qw==',
      ]);
      browser.plays[0].complete(true);
      await tester.pump();
      await tester.pump();
      expect(find.text('▮▮'), findsOneWidget);
      expect(browser.voices, hasLength(2));
      browser.plays[1].complete(true);
      await tester.pump();
      await tester.pump();
      expect(find.text(offline), findsNothing);
      await tester.pump(const Duration(seconds: 2));
    });

    testWidgets('leaving the screen cancels the clip being made ahead', (
      tester,
    ) async {
      tall(tester);
      final browser = _GatedBrowser();
      final calls = <String>[];
      final client = MockClient((req) async {
        calls.add('${req.method} ${req.url.path}');
        if (req.method == 'POST') {
          final text = jsonDecode(req.body)['text'] as String;
          if (text.startsWith('A')) {
            return http.Response(jsonEncode({'audio': 'QQ=='}), 200);
          }
          return http.Response(
            jsonEncode({'job_id': 'job-b', 'status': 'queued'}),
            202,
          );
        }
        if (req.method == 'DELETE') return http.Response('{}', 200);
        return http.Response(jsonEncode({'status': 'running'}), 200);
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(content: '$clipA $clipB'),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(calls, ['POST /api/tts', 'POST /api/tts']);

      // He leaves while A plays and B is still queued on the server.
      await tester.pumpWidget(_wrap(const SizedBox()));
      await tester.pump();
      expect(calls.last, 'DELETE /api/tts/jobs/job-b');
      browser.plays[0].complete(true);
      await tester.pump();
      expect(browser.voices, hasLength(1)); // B never plays
      await tester.pump(const Duration(seconds: 2));
      expect(tester.takeException(), isNull);
    });

    testWidgets('scrolling her line out of view does not cut her off', (
      tester,
    ) async {
      // The transcript is a lazy ListView: a bubble scrolled far enough
      // away is disposed, and that cancelled the clip made ahead, so a long
      // reply stopped after its first clip while he read older lines.
      tall(tester);
      final browser = _GatedBrowser();
      final posted = <String>[];
      final client = MockClient((req) async {
        final text = jsonDecode(req.body)['text'] as String;
        posted.add(text);
        final audio = base64Encode(utf8.encode(text.substring(0, 1)));
        return http.Response(jsonEncode({'audio': audio}), 200);
      });
      final controller = ScrollController();
      addTearDown(controller.dispose);
      await tester.pumpWidget(
        _wrap(
          ListView.builder(
            controller: controller,
            itemCount: 40,
            itemBuilder: (_, i) => i == 0
                ? MessageBubble(
                    message: _msg(content: '$clipA $clipB'),
                    client: client,
                    browser: browser,
                  )
                : SizedBox(height: 400, child: Text('older line $i')),
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      expect(posted, [clipA, clipB]);
      // A few seconds into clip A (the tap's ink, which holds the bubble
      // too, has faded), he scrolls up to read.
      await tester.pump(const Duration(seconds: 2));

      controller.jumpTo(controller.position.maxScrollExtent);
      await tester.pump();
      browser.plays[0].complete(true);
      await tester.pump();
      await tester.pump();
      expect(browser.voices.last, 'data:audio/wav;base64,Qg=='); // B plays
      browser.plays[1].complete(true);
      await tester.pump();
      await tester.pump();

      // Done speaking, it no longer holds its place: scrolled away, it goes
      // (once the button's ink, which keeps it too, has faded).
      await tester.pump(const Duration(seconds: 2));
      controller.jumpTo(0);
      await tester.pump();
      controller.jumpTo(controller.position.maxScrollExtent);
      await tester.pump();
      await tester.pump();
      expect(find.byType(MessageBubble, skipOffstage: false), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('a clip that fails stops the reply and says so', (
      tester,
    ) async {
      tall(tester);
      final browser = _GatedBrowser();
      final client = MockClient((req) async {
        final text = jsonDecode(req.body)['text'] as String;
        if (text.startsWith('B')) return http.Response('down', 502);
        return http.Response(jsonEncode({'audio': 'QQ=='}), 200);
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(content: '$clipA $clipB'),
            client: client,
            browser: browser,
          ),
        ),
      );
      await tester.tap(find.byKey(_voice));
      await tester.pump();
      await tester.pump();
      // B already failed while A plays; A still finishes.
      expect(find.text(offline), findsNothing);
      browser.plays[0].complete(true);
      await tester.pump();
      await tester.pump();
      expect(browser.voices, hasLength(1));
      expect(find.text('▶'), findsOneWidget);
      expect(find.text(offline), findsOneWidget);
      await tester.pump(const Duration(seconds: 5));
    });
  });

  testWidgets('completed replay is a labeled keyboard-accessible button', (
    tester,
  ) async {
    await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
    expect(find.byTooltip('Replay Klukai voice'), findsOneWidget);
    expect(
      find.descendant(of: find.byKey(_voice), matching: find.byType(InkWell)),
      findsNothing,
    );
    expect(tester.widget<InkWell>(find.byKey(_voice)).onTap, isNotNull);
    expect(tester.getSize(find.byKey(_voice)).height, greaterThanOrEqualTo(44));
  });

  testWidgets('invalid artwork bytes keep text and show an error', (
    tester,
  ) async {
    for (final invalid in ['not base64', 'AAECAwQ=']) {
      await tester.runAsync(() async {
        await tester.pumpWidget(
          _wrap(
            MessageBubble(
              key: ValueKey(invalid),
              message: _msg(content: 'Retained caption', imageData: invalid),
            ),
          ),
        );
        await Future<void>.delayed(const Duration(milliseconds: 100));
      });
      await tester.pump();
      expect(find.text('Retained caption'), findsOneWidget);
      expect(find.text('Artwork unavailable'), findsOneWidget);
      expect(tester.takeException(), isNull);
    }
  });

  testWidgets('replay keyboard activation reports refused browser playback', (
    tester,
  ) async {
    final browser = FakeBrowserPlatform(voiceResult: false);
    final client = MockClient(
      (req) async => http.Response(jsonEncode({'audio': 'UklGRg=='}), 200),
    );
    await tester.pumpWidget(
      _wrap(MessageBubble(message: _msg(), browser: browser, client: client)),
    );
    Focus.of(
      tester.element(
        find.descendant(of: find.byKey(_voice), matching: find.text('VOICE')),
      ),
    ).requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pump();
    await tester.pump();
    expect(tester.takeException(), isNull);
    expect(browser.voices, ['data:audio/wav;base64,UklGRg==']);
    expect(
      find.text('// VOICE SYNTH OFFLINE — her words stand.'),
      findsOneWidget,
    );
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('a voice letter plays its recorded note, only on a tap', (
    tester,
  ) async {
    var plays = 0;
    final notes = Completer<bool>();
    await tester.pumpWidget(
      _wrap(
        MessageBubble(
          message: _msg(content: 'I kept your seat warm on the bike.'),
          browser: FakeBrowserPlatform(),
          client: MockClient((_) async => fail('the note is not re-read')),
          voiceOverride: () {
            plays++;
            return notes.future;
          },
        ),
      ),
    );
    await tester.pump();
    expect(plays, 0);
    await tester.tap(find.byKey(_voice));
    await tester.pump();
    expect(plays, 1);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.tap(find.byKey(_voice)); // busy: not a second playback
    expect(plays, 1);
    notes.complete(true);
    await tester.pump();
    expect(find.text('VOICE'), findsOneWidget);
    expect(
      find.text('// VOICE SYNTH OFFLINE — her words stand.'),
      findsNothing,
    );
  });

  testWidgets('an image-only line has no VOICE', (tester) async {
    await tester.pumpWidget(
      _wrap(
        MessageBubble(
          message: _msg(content: '[IMAGE]', imageData: _pngB64),
        ),
      ),
    );
    await tester.pump();
    expect(find.byKey(_voice), findsNothing);
  });

  group('MessageBubble — alignment', () {
    testWidgets('user message is right-aligned, companion left-aligned', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', content: 'u'),
          ),
        ),
      );
      await tester.pump();
      final userRow = tester.widget<Row>(find.byType(Row).first);
      expect(userRow.mainAxisAlignment, MainAxisAlignment.end);

      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'assistant', content: 'a'),
          ),
        ),
      );
      await tester.pump();
      final botRow = tester.widget<Row>(find.byType(Row).first);
      expect(botRow.mainAxisAlignment, MainAxisAlignment.start);
    });
  });
}

/// Her voice channel, playing each clip until the test says it has ended.
class _GatedBrowser extends FakeBrowserPlatform {
  final plays = <Completer<bool>>[];

  @override
  Future<bool> playVoice(String url) {
    voices.add(url);
    final ended = Completer<bool>();
    plays.add(ended);
    return ended.future;
  }
}

class _ClosingClient extends MockClient {
  _ClosingClient(super.fn);
  bool closed = false;

  @override
  void close() {
    closed = true;
    super.close();
  }
}
