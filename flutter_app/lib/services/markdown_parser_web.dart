import 'dart:js_interop';

@JS('pretextBridge.parseMarkdown')
external String? _parseMarkdown(String source);
String? parseMarkdown(String source) {
  try {
    return _parseMarkdown(source);
  } catch (_) {
    return null;
  }
}
