@TestOn('browser')
library;

import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:companion_app/widgets/message_content.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Widget _content(String source, {double scale = 1}) => MaterialApp(
  home: Scaffold(
    body: MediaQuery(
      data: MediaQueryData(textScaler: TextScaler.linear(scale)),
      child: MessageContent(source: source),
    ),
  ),
);

String _text(WidgetTester tester) => tester
    .widgetList<SelectableText>(find.byType(SelectableText))
    .map((widget) => widget.data ?? widget.textSpan!.toPlainText())
    .join('\n');

void _testContent(String name, WidgetTesterCallback test) {
  testWidgets(name, (tester) async {
    try {
      await test(tester);
    } catch (error) {
      // Chrome's runner can omit Flutter's exception details from stdout.
      // ignore: avoid_print
      print('CONTENT_FAILURE: $error');
      rethrow;
    }
  });
}

void main() {
  late List<String> parsed;
  setUp(() {
    parsed = [];
    final bridge = JSObject();
    bridge['parseMarkdown'] = ((String source) {
      parsed.add(source);
      return jsonEncode([
        {
          'type': 'paragraph',
          'text': source,
          'tokens': [
            {'type': 'text', 'text': source},
          ],
        },
      ]).toJS;
    }).toJS;
    globalContext['pretextBridge'] = bridge;
  });
  tearDown(() => globalContext.delete('pretextBridge'.toJS));

  _testContent('unchanged content parses once across rebuild and text reflow', (
    tester,
  ) async {
    await tester.pumpWidget(_content('Standing by, Commander.'));
    await tester.pumpWidget(_content('Standing by, Commander.', scale: 2));
    await tester.binding.setSurfaceSize(const Size(320, 700));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pump();
    await tester.pumpWidget(_content('Standing by, Commander.', scale: 2));

    expect(_text(tester), 'Standing by, Commander.');
    expect(parsed, ['Standing by, Commander.']);
    expect(tester.takeException(), isNull);
  });

  _testContent('stream append and canonical replacement each parse new text', (
    tester,
  ) async {
    await tester.pumpWidget(_content('Orders'));
    await tester.pumpWidget(_content('Orders received.'));
    expect(_text(tester), 'Orders received.');
    await tester.pumpWidget(_content('Understood, Commander.'));
    await tester.pumpWidget(_content('Understood, Commander.'));

    expect(_text(tester), 'Understood, Commander.');
    expect(parsed, ['Orders', 'Orders received.', 'Understood, Commander.']);
  });

  _testContent('unusable tokens retain source without retrying every rebuild', (
    tester,
  ) async {
    final bridge = JSObject();
    bridge['parseMarkdown'] = ((String source) {
      parsed.add(source);
      return '[{"type":"unknown"}]'.toJS;
    }).toJS;
    globalContext['pretextBridge'] = bridge;

    await tester.pumpWidget(_content('**unfinished'));
    await tester.pumpWidget(_content('**unfinished', scale: 2));
    expect(_text(tester), '**unfinished');
    expect(parsed, ['**unfinished']);

    await tester.pumpWidget(_content('Complete source.'));
    expect(_text(tester), 'Complete source.');
    expect(parsed, ['**unfinished', 'Complete source.']);
  });

  _testContent('parsed content belongs to the mounted message', (tester) async {
    await tester.pumpWidget(_content('Standing by.'));
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(_content('Standing by.'));

    expect(_text(tester), 'Standing by.');
    expect(parsed, ['Standing by.', 'Standing by.']);
  });

  _testContent('twenty transcript rebuilds do not reparse eight stable lines', (
    tester,
  ) async {
    for (var frame = 0; frame <= 20; frame++) {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Column(
              children: [
                for (var line = 0; line < 8; line++)
                  MessageContent(
                    key: ValueKey(line),
                    source: 'Transcript line $line',
                  ),
              ],
            ),
          ),
        ),
      );
    }

    expect(find.byType(SelectableText), findsNWidgets(8));
    expect(_text(tester), contains('Transcript line 7'));
    expect(parsed, hasLength(8));
  });
}
