// Per-device preferences kept in the browser's localStorage, as plain string
// values under the exact keys other pages read (the chat view reads
// `kOpenIn3dPrefKey`; companion.html reads `klukai_companion_prefs`).
//
// Conditional export, the same seam as browser.dart and session_auth.dart:
// the browser build talks to localStorage through package:web; the Dart VM
// (unit and widget tests) gets an in-memory store.
export 'local_prefs_api.dart';
export 'local_prefs_stub.dart'
    if (dart.library.js_interop) 'local_prefs_web.dart';
