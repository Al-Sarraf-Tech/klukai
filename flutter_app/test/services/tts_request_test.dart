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

  test('the request body carries the sentence-clipped text', () {
    const sentence = 'The hangar is quiet tonight. ';
    final text = ttsRequestBody(sentence * 30)['text']!;
    expect(text, endsWith('tonight.'));
    expect(text.length, lessThanOrEqualTo(kTtsMaxChars));
  });

  group('clipForTts', () {
    test('text within the limit is untouched', () {
      expect(clipForTts('Report.'), 'Report.');
      expect(clipForTts('a' * kTtsMaxChars).length, kTtsMaxChars);
    });

    test('a long line stops at its last sentence end, not mid-word', () {
      const sentence = 'The hangar is quiet tonight. '; // 29 chars
      final text = sentence * 30; // 870 chars
      final clipped = clipForTts(text);
      expect(clipped.length, lessThanOrEqualTo(kTtsMaxChars));
      expect(clipped, endsWith('tonight.'));
      expect(text.startsWith(clipped), isTrue);
      // The sentence after the cut would have crossed the limit.
      expect(clipped.length + sentence.length, greaterThan(kTtsMaxChars));
    });

    test('"!" and "?" end a sentence too, before a space or a newline', () {
      expect(clipForTts('Hold position! Now move out quietly please.', limit: 30),
          'Hold position!');
      expect(clipForTts('Are you ready?\nMove out on my mark.', limit: 25), 'Are you ready?');
      expect(clipForTts('Hold here.\nI will scout ahead and report back.', limit: 25),
          'Hold here.');
    });

    test('a sentence ending exactly at the limit is kept whole', () {
      expect(clipForTts('Confirmed. Then more words follow here.', limit: 10), 'Confirmed.');
    });

    test('falls back to the hard cut when the only sentence end comes too early', () {
      // One short sentence then a wall of text: clipping at "Yes." would drop
      // nearly everything she said, so cut hard at the limit instead.
      final clipped = clipForTts('Yes. ${'a' * 30}', limit: 20);
      expect(clipped.length, 20);
      expect(clipped, 'Yes. ${'a' * 15}');
    });

    test('no sentence end at all: plain hard cut', () {
      expect(clipForTts('a' * 900).length, kTtsMaxChars);
    });
  });
}
