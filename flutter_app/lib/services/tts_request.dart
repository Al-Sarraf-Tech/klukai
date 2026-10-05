/// Body for POST /api/tts — her voice reading one of her lines.
///
/// `language` is the language of the TEXT, not of her voice. Her voice is
/// always the Japanese Ai Nonaka reference clip; her lines are English, so
/// they are read as "en". "ja" asks XTTS to parse English as Japanese and
/// needs `cutlet`, which the voice image doesn't ship — it 500s.
Map<String, String> ttsRequestBody(String text) => {
      'text': text.length > 500 ? text.substring(0, 500) : text,
      'language': 'en',
    };
