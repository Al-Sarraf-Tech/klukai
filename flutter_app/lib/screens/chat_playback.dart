part of 'chat_screen.dart';

/// The notification beep and voice letters' recorded notes (tap to play).
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

  /// A voice letter's recorded note, fetched with the bearer token and played
  /// through the shared, gesture-unlocked channel (web/js/voice.js). Runs only
  /// from that letter's `▶ VOICE`; resolves true once it was heard.
  Future<bool> _fetchAndPlayVoiceNote(String voiceId) async {
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
        return _browser.playVoice(
          'data:audio/wav;base64,${base64Encode(resp.bodyBytes)}',
        );
      }
      debugPrint('Voice note fetch failed: HTTP ${resp.statusCode}');
    } catch (e) {
      debugPrint('Voice note fetch failed: $e');
    }
    return false;
  }
}
