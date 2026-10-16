part of 'chat_screen.dart';

/// Notification and queued voice playback, including authenticated voice notes.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatPlayback on _ChatScreenState {
  void _playNotificationSound() {
    if (_soundMuted) return;
    try {
      final audio = web.HTMLAudioElement()
        ..src = 'audio/comm_beep.wav'
        ..volume = 0.3;
      // play() rejects without a prior gesture (iOS, Safari autoplay rules);
      // a missed beep is fine, an unhandled rejection is console noise.
      audio.play().toDart.catchError((Object _) => null);
    } catch (_) {}
  }

  /// Her voice goes through the shared, gesture-unlocked channel (web/js/
  /// voice.js): a fresh Audio element per WebSocket frame is refused on iOS,
  /// and two frames close together would otherwise talk over each other.
  void _playAudio(String base64Audio) {
    _browser.playVoice('data:audio/wav;base64,$base64Audio').then((ok) {
      if (!ok) debugPrint('Voice playback refused (no gesture yet?)');
    });
  }

  Future<void> _fetchAndPlayVoiceNote(String voiceId) async {
    // Image.network(headers:) is ignored on web, and the same applies to audio
    // src — so fetch the bytes with the bearer token and play them inline.
    try {
      final uri = Uri.parse(
        '${widget.serverUrl}/api/voice-notes/$voiceId/audio',
      );
      final resp = await http
          .get(uri, headers: _authHeaders)
          .timeout(const Duration(seconds: 20));
      if (resp.statusCode == 200 && resp.bodyBytes.isNotEmpty) {
        _playAudio(base64Encode(resp.bodyBytes));
      } else {
        debugPrint('Voice note fetch failed: HTTP ${resp.statusCode}');
      }
    } catch (e) {
      debugPrint('Voice note fetch failed: $e');
    }
  }
}
