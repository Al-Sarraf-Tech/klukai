import 'package:web/web.dart' as web;

/// Reads the bearer token the login page stored. Never throws.
String readAuthToken() {
  try {
    return web.window.localStorage.getItem('klukai_token') ?? '';
  } catch (_) {
    return '';
  }
}

/// The server answered 401: the token is dead. Clear it and return to login
/// instead of leaving a screen on a "channel down" / fake-empty state whose
/// Retry can never succeed. Mirrors ChatScreen's _handleAuthExpired.
void handleAuthExpired() {
  try {
    web.window.localStorage.removeItem('klukai_token');
  } catch (_) {}
  web.window.location.href = '/';
}
