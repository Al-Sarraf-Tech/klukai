/// The voice service reads at most this many characters of one line.
const int kTtsMaxChars = 500;

/// Body for POST /api/tts — her voice reading one of her lines.
///
/// `language` is the language of the TEXT, not of her voice. Her voice is
/// always the Japanese Ai Nonaka reference clip; her lines are English, so
/// they are read as "en". "ja" asks XTTS to parse English as Japanese and
/// needs `cutlet`, which the voice image doesn't ship — it 500s.
Map<String, String> ttsRequestBody(String text) => {
      'text': clipForTts(text),
      'language': 'en',
    };

/// Clips a line that is too long to read to its last sentence end within
/// [limit] (". ", "! ", "? ", or the same before a newline), so she stops at a
/// full stop instead of mid-word. If the last sentence end comes before a
/// third of the limit — one short sentence and then a wall of text — the clip
/// would drop almost everything she said, so it falls back to the hard cut.
/// Text within the limit is returned untouched.
String clipForTts(String text, {int limit = kTtsMaxChars}) {
  if (text.length <= limit) return text;
  // One extra character so a sentence ending exactly at the limit still
  // counts (its trailing space is the character just past it).
  final window = text.substring(0, limit + 1);
  var cut = -1;
  for (final boundary in const ['. ', '! ', '? ', '.\n', '!\n', '?\n']) {
    final i = window.lastIndexOf(boundary);
    if (i > cut) cut = i;
  }
  if (cut < limit ~/ 3) return text.substring(0, limit);
  return text.substring(0, cut + 1); // keep the punctuation, drop the space
}
