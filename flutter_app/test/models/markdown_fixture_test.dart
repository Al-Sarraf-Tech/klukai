import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/markdown_document.dart';
import '../support/conversation_fixture.dart';

void main() {
  test('real checked-in marked tokens produce a native rich document', () {
    final parsed = MarkdownToken.document(
      conversationSource,
      conversationTokens,
    );
    expect(parsed, isNotNull);
    expect(
      parsed!.map((t) => t.type),
      containsAll(['heading', 'blockquote', 'list', 'table']),
    );
  });
}
