part of 'chat_screen.dart';

/// Authenticated screen navigation and ordered service-worker logout.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatNavigation on _ChatScreenState {
  void _openProfile() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ProfileScreen(
          serverUrl: widget.serverUrl,
          affectionScore: _state.affectionScore,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
          wardrobeService: _wardrobe,
          onAuthExpired: _handleAuthExpired,
        ),
      ),
    ).then((_) => _loadHerDay()); // a wardrobe change in the dossier shows here
  }

  Future<void> _logout() async {
    _ws.dispose();
    // End the session on the server too; bounded, so it never holds up logout.
    await endServerSession(widget.serverUrl, readAuthToken());
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    // Unregister the Flutter service worker BEFORE navigating, so login.html
    // is fetched from the server instead of being served by a stale SW cache.
    await _unregisterServiceWorker();
    web.window.location.href = '/';
  }

  void _openHerPov() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => HerPovScreen(
          serverUrl: widget.serverUrl,
          affectionLevel: _state.affectionLevel,
          affectionLevelName: _state.affectionLevelName,
        ),
      ),
    );
  }

  /// Her threads (letters and running conversations). Lived behind the
  /// archive button; now in the chat menu.
  void _openThreads() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ThreadScreen(
          serverUrl: widget.serverUrl,
          onAuthExpired: _handleAuthExpired,
        ),
      ),
    );
  }

  void _openSubscription() {
    String? token;
    try {
      token = web.window.localStorage.getItem('klukai_token');
    } catch (_) {}
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => SubscriptionScreen(
          serverUrl: widget.serverUrl,
          authToken: token,
          onAuthExpired: _handleAuthExpired,
        ),
      ),
    );
  }
}
