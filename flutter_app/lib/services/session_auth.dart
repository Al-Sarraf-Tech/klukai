// The Commander's bearer token (`klukai_token` in localStorage).
//
// Conditional import: the browser build reads localStorage through
// package:web; the Dart VM (unit/widget tests) gets an empty token, so code
// that only needs the header can be tested without a browser.
//
// Also home to `handleAuthExpired()`: the one shared "token is dead, back to
// login" bounce for screens that have no injected `onAuthExpired`.
export 'session_auth_stub.dart'
    if (dart.library.js_interop) 'session_auth_web.dart';
