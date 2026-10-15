@TestOn('browser')
library;

import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:web/web.dart' as web;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/message.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/widgets/message_content.dart';
import '../support/fake_browser.dart';
import '../support/conversation_fixture.dart';
import '../support/artwork_fixtures.dart';

@JS('eval')
external JSAny? evaluate(String source);

void parser(String? tokens) {
  final bridge = JSObject();
  bridge['isReady'] = (() => true.toJS).toJS;
  bridge['parseMarkdown'] = ((String source) => tokens?.toJS).toJS;
  globalContext['pretextBridge'] = bridge;
}

Widget wrap(Widget child, {double scale = 1}) => MaterialApp(
  home: Scaffold(
    body: Builder(
      builder: (context) => MediaQuery(
        data: MediaQuery.of(
          context,
        ).copyWith(textScaler: TextScaler.linear(scale)),
        child: SingleChildScrollView(child: child),
      ),
    ),
  ),
);
String selectable(WidgetTester tester) => tester
    .widgetList<SelectableText>(find.byType(SelectableText))
    .map((w) => w.data ?? w.textSpan!.toPlainText())
    .join('\n');
void fixtureTest(String name, WidgetTesterCallback callback) {
  testWidgets(name, (tester) async {
    try {
      await callback(tester);
    } catch (error) {
      // ignore: avoid_print
      print('FIXTURE_FAILURE: $error');
      rethrow;
    }
  });
}

Future<void> settleArtwork(WidgetTester tester) async {
  for (var i = 0; i < 30; i++) {
    final frames = tester.widgetList<RawImage>(find.byType(RawImage));
    if (frames.isNotEmpty && frames.every((frame) => frame.image != null)) {
      return;
    }
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
    await tester.pump();
  }
  expect(
    tester
        .widgetList<RawImage>(find.byType(RawImage))
        .every((frame) => frame.image != null),
    isTrue,
    reason: 'Fixture image must decode before visual assertions',
  );
}

void main() {
  tearDown(() => globalContext.delete('pretextBridge'.toJS));
  fixtureTest(
    'unsupported tokens and broken parser preserve whole selectable source',
    (tester) async {
      for (final tokens in [
        null,
        'not json',
        '[{"type":"unknown","raw":"lost"}]',
        '[{"type":"html","raw":"<script>bad</script>"}]',
        '[{"type":"image","href":"https://example.invalid/p.png"}]',
      ]) {
        parser(tokens);
        const source =
            '<script>bad</script>\n![image](https://example.invalid/p.png)\n**unfinished';
        await tester.pumpWidget(wrap(const MessageContent(source: source)));
        expect(selectable(tester), source);
        expect(find.byType(Image), findsNothing);
        expect(find.byType(TextButton), findsNothing);
      }
      final bridge = JSObject();
      JSString brokenParser(String source) {
        throw StateError('parser failed');
      }

      bridge['parseMarkdown'] = brokenParser.toJS;
      globalContext['pretextBridge'] = bridge;
      await tester.pumpWidget(
        wrap(const MessageContent(source: 'Full fallback')),
      );
      expect(selectable(tester), 'Full fallback');
    },
  );
  fixtureTest('links expose source and only http/https have keyboard actions', (
    tester,
  ) async {
    parser(conversationTokens);
    final browser = FakeBrowserPlatform();
    await tester.pumpWidget(
      wrap(MessageContent(source: conversationSource, browser: browser)),
    );
    final action = find.text(
      'Open link: https://example.invalid/briefing?q=ready',
    );
    expect(action, findsOneWidget);
    expect(selectable(tester), contains('Link blocked: javascript:alert(1)'));
    final button = find
        .ancestor(of: action, matching: find.byType(TextButton))
        .first;
    await tester.ensureVisible(button);
    Focus.of(tester.element(action)).requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pump();
    expect(tester.takeException(), isNull);
    expect(browser.opened, [
      ('https://example.invalid/briefing?q=ready', true),
    ]);
  });
  for (final width in [320.0, 390.0, 1280.0]) {
    fixtureTest('mixed native content reflows at $width px and 200% text', (
      tester,
    ) async {
      await tester.binding.setSurfaceSize(Size(width, 1000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      parser(conversationTokens);
      final message = ChatMessage(
        id: 'fixture',
        role: 'assistant',
        content: conversationSource,
        tightBubbleWidth: 20,
        createdAt: DateTime(2026, 10, 14, 12),
      );
      await tester.pumpWidget(wrap(MessageBubble(message: message), scale: 2));
      await tester.pump();
      for (final token in [
        '日本語',
        'Commander',
        'quietly',
        'old',
        'H.I.D.E.',
        'Hold position.',
        'Stay with me.',
        'Three',
        'Four',
        'Nested alpha',
        'Nested beta',
        'status = "ready"',
        'Name',
        'Status',
        'Klukai',
        'Briefing',
        'bad',
        'Ready',
      ]) {
        expect(selectable(tester), contains(token));
      }
      expect(tester.takeException(), isNull);
      expect(find.byType(Table), findsOneWidget);
      final bounds = tester.getRect(find.byType(MessageContent));
      expect(bounds.left, greaterThanOrEqualTo(0));
      expect(bounds.right, lessThanOrEqualTo(width));
      expect(find.byTooltip('Replay Klukai voice'), findsOneWidget);
      await tester.binding.setSurfaceSize(
        Size(width == 320 ? 1280 : 320, 1000),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
    });
  }
  fixtureTest(
    'download keyboard action preserves original bytes and truthful MIME',
    (tester) async {
      evaluate(
        'window.__savedAnchorClick = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function() { window.__fixtureDownload = {name: this.download, href: this.href}; };',
      );
      addTearDown(
        () => evaluate(
          'HTMLAnchorElement.prototype.click = window.__savedAnchorClick; delete window.__savedAnchorClick; delete window.__fixtureDownload;',
        ),
      );
      for (final fixture in [
        (artworkFixtures['landscape']!, 'image/png', '.png'),
        (jpegArtworkFixture, 'image/jpeg', '.jpg'),
      ]) {
        await tester.pumpWidget(
          wrap(
            MessageBubble(
              key: ValueKey(fixture.$2),
              message: ChatMessage(
                id: 'download',
                role: 'assistant',
                content: '',
                imageData: fixture.$1,
              ),
            ),
          ),
        );
        await settleArtwork(tester);
        Focus.of(tester.element(find.byIcon(Icons.download))).requestFocus();
        await tester.pump();
        await tester.sendKeyEvent(LogicalKeyboardKey.enter);
        await tester.pump();
        final download = globalContext['__fixtureDownload'] as JSObject;
        expect((download['name'] as JSString).toDart, endsWith(fixture.$3));
        final href = download['href'] as JSString;
        await tester.runAsync(() async {
          final response = await web.window.fetch(href).toDart;
          final blob = await response.blob().toDart;
          expect(blob.type, fixture.$2);
          final bytes = (await blob.arrayBuffer().toDart).toDart.asUint8List();
          expect(bytes, base64Decode(fixture.$1));
        });
        await tester.pump(const Duration(seconds: 1));
        expect(tester.takeException(), isNull);
      }
    },
  );
  for (final name in artworkFixtures.keys) {
    fixtureTest('$name artwork retains corners and has full-view controls', (
      tester,
    ) async {
      final semantics = tester.ensureSemantics();
      await tester.binding.setSurfaceSize(const Size(390, 850));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.runAsync(() async {
        await tester.pumpWidget(
          wrap(
            RepaintBoundary(
              key: const ValueKey('artwork-capture'),
              child: MessageBubble(
                message: ChatMessage(
                  id: name,
                  role: 'assistant',
                  content: 'All four corners',
                  imageData: artworkFixtures[name],
                ),
              ),
            ),
          ),
        );
        await Future<void>.delayed(const Duration(milliseconds: 100));
      });
      await tester.pump();
      await settleArtwork(tester);
      final image = tester.widget<Image>(find.byType(Image));
      expect(image.fit, BoxFit.contain);
      expect(
        (image.image as MemoryImage).bytes,
        base64Decode(artworkFixtures[name]!),
      );
      final boundary = tester.renderObject<RenderRepaintBoundary>(
        find.byKey(const ValueKey('artwork-capture')),
      );
      await tester.runAsync(() async {
        final screenshot = await boundary.toImage(pixelRatio: 1);
        final png = (await screenshot.toByteData(
          format: ui.ImageByteFormat.png,
        ))!;
        // ignore: avoid_print
        print(
          'FIXTURE_PNG:$name:390x850:${base64Encode(png.buffer.asUint8List())}',
        );
        final pixels = (await screenshot.toByteData(
          format: ui.ImageByteFormat.rawRgba,
        ))!.buffer.asUint8List();
        for (final color in [
          [255, 0, 0],
          [0, 128, 0],
          [0, 0, 255],
          [255, 255, 0],
        ]) {
          var visible = false;
          for (var i = 0; i < pixels.length; i += 4) {
            if (pixels[i] == color[0] &&
                pixels[i + 1] == color[1] &&
                pixels[i + 2] == color[2]) {
              visible = true;
              break;
            }
          }
          expect(
            visible,
            isTrue,
            reason: '$name corner marker $color must be painted',
          );
        }
        screenshot.dispose();
      });
      final captureError = tester.takeException();
      expect(captureError, isNull);
      await tester.ensureVisible(find.byIcon(Icons.open_in_full));
      Focus.of(tester.element(find.byIcon(Icons.open_in_full))).requestFocus();
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pumpAndSettle();
      expect(find.byType(InteractiveViewer), findsOneWidget);
      expect(find.byTooltip('Zoom in'), findsOneWidget);
      expect(find.byTooltip('Fit full artwork'), findsOneWidget);
      Focus.of(tester.element(find.byIcon(Icons.zoom_in))).requestFocus();
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      expect(
        tester
            .widget<InteractiveViewer>(find.byType(InteractiveViewer))
            .transformationController!
            .value
            .getMaxScaleOnAxis(),
        1.5,
      );
      await tester.tap(find.byIcon(Icons.fit_screen));
      await tester.pump();
      expect(
        tester
            .widget<InteractiveViewer>(find.byType(InteractiveViewer))
            .transformationController!
            .value
            .getMaxScaleOnAxis(),
        1,
      );
      expect(
        tester
            .widgetList<Image>(find.byType(Image))
            .every((image) => image.fit == BoxFit.contain),
        isTrue,
      );
      expect(
        tester.getSemantics(find.byIcon(Icons.close)).tooltip,
        'Close full artwork',
      );
      Focus.of(tester.element(find.byIcon(Icons.close))).requestFocus();
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pumpAndSettle();
      expect(find.byType(InteractiveViewer), findsNothing);
      semantics.dispose();
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump(const Duration(seconds: 2));
      expect(tester.takeException(), isNull);
    });
  }
}
