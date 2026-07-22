@TestOn('browser')
library;

// Widget tests for ToolStatusIndicator: tool-name framing + in-progress vs
// done visuals.
//
// Imports main.dart (GFL2Colors) -> package:web, so run under chrome.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/tool_status_indicator.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('ToolStatusIndicator framing', () {
    final framings = <String, String>{
      'web_search': 'INTEL',
      'search_docs': 'INTEL',
      'browse_page': 'RECON',
      'fetch_url': 'RECON',
      'run_code': 'SYSTEMS',
      'code_exec': 'SYSTEMS',
      'read_file': 'DATA', // contains 'file' AND 'read'
      'unknown_tool': 'OPERATION',
    };

    framings.forEach((tool, frame) {
      testWidgets('"$tool" is framed as "$frame"', (tester) async {
        await tester.pumpWidget(
          _wrap(ToolStatusIndicator(toolName: tool, status: 'running')),
        );
        expect(find.text('$frame // $tool'), findsOneWidget);
      });
    });
  });

  group('ToolStatusIndicator status visuals', () {
    testWidgets('in-progress shows a spinner, not a check', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const ToolStatusIndicator(toolName: 'web_search', status: 'running'),
        ),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.byIcon(Icons.check), findsNothing);
    });

    testWidgets('done shows a check, not a spinner', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const ToolStatusIndicator(toolName: 'web_search', status: 'done'),
        ),
      );
      expect(find.byIcon(Icons.check), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    testWidgets('any non-"done" status is treated as in-progress', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          const ToolStatusIndicator(toolName: 'web_search', status: 'queued'),
        ),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });
  });
}
