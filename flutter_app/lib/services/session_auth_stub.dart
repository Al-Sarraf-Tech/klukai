/// VM fallback: there is no localStorage outside the browser.
String readAuthToken() => '';

/// VM fallback: nowhere to redirect to. Screens take an injectable
/// `onAuthExpired` so tests can observe the bounce instead.
void handleAuthExpired() {}
