import 'package:companion_app/services/tts_request.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('her lines are read as English text in her Japanese voice', () {
    // "ja" makes the voice service 500 (no cutlet) — never send it.
    expect(ttsRequestBody('Report.')['language'], 'en');
  });

  test('long lines are capped at 500 characters', () {
    final body = ttsRequestBody('a' * 900);
    expect(body['text']!.length, 500);
    expect(ttsRequestBody('short')['text'], 'short');
  });
}
