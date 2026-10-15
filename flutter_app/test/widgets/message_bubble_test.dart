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
    testWidgets('shows the KLUKAI comm tag and a voice (speaker) control', (
      tester,
    ) async {
      await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
      await tester.pump();
      expect(find.text('KLUKAI // SST-05'), findsOneWidget);
      // Idle speaker icon is shown for non-streaming companion messages.
      expect(find.byIcon(Icons.volume_up_outlined), findsOneWidget);
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
      expect(find.text('KLUKAI // SST-05'), findsNothing);
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);
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
      // Footer (with speaker + timestamp) is suppressed during streaming.
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);
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

      await tester.tap(find.byIcon(Icons.volume_up_outlined));
      await tester.pump();
      // Request in flight: the speaker is a spinner.
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);

      gate.complete(http.Response('voice service unavailable', 502));
      await tester.pump();
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.byIcon(Icons.volume_up_outlined), findsOneWidget);
      expect(find.text(offline), findsOneWidget);
      // Let the snackbar run its course.
      await tester.pump(const Duration(seconds: 5));
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

      await tester.tap(find.byIcon(Icons.volume_up_outlined));
      await tester.pump();
      await tester.pump();
      expect(browser.voices, ['data:audio/wav;base64,UklGRg==']);
      expect(find.text(offline), findsNothing);
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });
  });

  testWidgets('completed replay is a labeled keyboard-accessible button', (
    tester,
  ) async {
    await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
    expect(find.byTooltip('Replay Klukai voice'), findsOneWidget);
    expect(find.byType(IconButton), findsOneWidget);
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
    final button = find.byWidgetPredicate(
      (w) => w is IconButton && w.tooltip == 'Replay Klukai voice',
    );
    Focus.of(
      tester.element(
        find.descendant(of: button, matching: find.byType(Icon)).first,
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
