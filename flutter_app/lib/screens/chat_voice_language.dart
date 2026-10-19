part of 'chat_screen.dart';

/// Her voice language (the composer's EN | JP), loaded from and saved to
/// /api/voice/settings. English until the server says otherwise; a missing
/// endpoint stays English. Shares the screen's single State owner.
extension _ChatVoiceLanguage on _ChatScreenState {
  Future<void> _loadVoiceLanguage() async {
    try {
      final language = await _voiceSettings.load();
      if (!mounted || _voiceLanguageSaving || _authExpired) return;
      _update(() => _voiceLanguage = language);
    } on VoiceSettingsAuthExpired {
      if (mounted) _handleAuthExpired();
    } catch (e) {
      debugPrint('Voice settings unavailable: $e'); // keep the current choice
    }
  }

  /// Optimistic: the switch moves at once and goes back, with a notice, if
  /// the server doesn't take it.
  Future<void> _setVoiceLanguage(VoiceLanguage next) async {
    if (next == _voiceLanguage || _voiceLanguageSaving) return;
    final previous = _voiceLanguage;
    _update(() {
      _voiceLanguage = next;
      _voiceLanguageSaving = true;
    });
    try {
      final saved = await _voiceSettings.save(next);
      if (!mounted) return;
      _update(() {
        _voiceLanguage = saved;
        _voiceLanguageSaving = false;
      });
    } on VoiceSettingsAuthExpired {
      if (!mounted) return;
      _update(() {
        _voiceLanguage = previous;
        _voiceLanguageSaving = false;
      });
      _handleAuthExpired();
    } catch (e) {
      debugPrint('Voice settings save failed: $e');
      if (!mounted) return;
      _update(() {
        _voiceLanguage = previous;
        _voiceLanguageSaving = false;
      });
      _showHudNotice(
        '// VOICE LANGUAGE NOT SAVED — still ${previous.label}.',
        key: const Key('voice-language-error'),
      );
    }
  }
}
