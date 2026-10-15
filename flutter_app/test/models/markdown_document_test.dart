import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/markdown_document.dart';

void main() {
  test(
    'malformed recognized tokens preserve source instead of losing a block',
    () {
      for (final value in [
        [
          {'type': 'table', 'header': [], 'rows': []},
        ],
        [
          {'type': 'paragraph', 'raw': 'Cannot disappear'},
        ],
        [
          {
            'type': 'list',
            'ordered': true,
            'start': 'broken',
            'items': [
              {'type': 'list_item', 'text': 'Item'},
            ],
          },
        ],
        [
          {
            'type': 'paragraph',
            'tokens': [
              {
                'type': 'table',
                'header': [
                  {'text': 'Nested'},
                ],
                'rows': [],
              },
            ],
          },
        ],
      ]) {
        expect(
          MarkdownToken.document('Complete original', jsonEncode(value)),
          isNull,
        );
      }
    },
  );
}
