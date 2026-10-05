// Browser integration seam: safe-area insets, page resume, iOS standalone
// detection, opening URLs, and voice playback.
//
// Conditional export: the real implementation talks to the DOM through
// package:web; the Dart VM (unit and widget tests) gets an inert stub. Widgets
// take a [BrowserPlatform] so tests can inject a fake.
export 'browser_api.dart';
export 'browser_stub.dart' if (dart.library.js_interop) 'browser_web.dart';
