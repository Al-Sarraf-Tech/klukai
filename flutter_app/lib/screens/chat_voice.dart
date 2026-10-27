part of 'chat_screen.dart';

/// Microphone permission, epoch cancellation, transcription, and voice playback.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatVoice on _ChatScreenState {
  /// Bound the wait for cloud transcription so the composer can recover.
  static const _sttTimeout = Duration(seconds: 35);

  Future<bool> _startRecorder() async {
    final bridge = widget.speechBridge;
    if (bridge != null) return bridge.start();
    return (await _jsStartRecording().toDart).toDart;
  }

  Future<String?> _stopRecorder() async {
    final bridge = widget.speechBridge;
    if (bridge != null) return bridge.stop();
    final audio = await _jsStopRecording().toDart;
    return audio?.toDart;
  }

  Future<void> _onVoiceDown() async {
    final epoch = _speech.begin();
    if (epoch == null) return;
    if (mounted) _update(() {});
    var started = false;
    try {
      started = await _startRecorder();
    } catch (_) {
      started = false;
    }
    if (!mounted || !_speech.isCurrent(epoch)) {
      // Permission can resolve after release/cancel already attempted a stop.
      // If the bridge only started now, close that late recording as well.
      if (started) {
        try {
          await _stopRecorder();
        } catch (_) {
          debugPrint('Microphone cleanup failed after cancelled permission');
        }
      }
      return;
    }
    if (!started) {
      _update(() => _speech.fail(epoch, 'permission'));
      _showVoiceLinkError(
        '// MICROPHONE UNAVAILABLE — allow microphone access or type.',
      );
      return;
    }
    _update(() => _speech.markRecording(epoch));
  }

  Future<void> _onVoiceUp() async {
    final epoch = _speech.epoch;
    if (_speech.phase == SpeechPhase.requestingPermission) {
      _speech.cancel();
      if (mounted) _update(() {});
      try {
        await _stopRecorder();
      } catch (_) {}
      return;
    }
    if (_speech.phase != SpeechPhase.recording) return;
    _update(() => _speech.markTranscribing(epoch));
    String? audio;
    try {
      audio = await _stopRecorder();
    } catch (_) {
      audio = null;
    }
    if (!mounted || !_speech.isCurrent(epoch)) return;
    if (audio == null || audio.isEmpty) {
      _update(() => _speech.fail(epoch, 'empty'));
      _showVoiceLinkError();
      return;
    }
    await _transcribeAndSend(audio, epoch);
  }

  Future<void> _cancelVoice() async {
    if (_speech.phase == SpeechPhase.idle) return;
    _speech.cancel();
    if (mounted) _update(() {});
    try {
      await _stopRecorder();
    } catch (_) {}
  }

  Future<http.Response> _postStt(String audioBase64) {
    final uri = Uri.parse('${widget.serverUrl}/api/stt');
    final body = jsonEncode({'audio': audioBase64});
    final client = widget.httpClient;
    if (client != null) {
      return client.post(uri, headers: _authHeaders, body: body);
    }
    return http.post(uri, headers: _authHeaders, body: body);
  }

  Future<void> _transcribeAndSend(String audioBase64, int epoch) async {
    var text = '';
    var failureNotice = '// VOICE INPUT UNAVAILABLE — use text for now.';
    try {
      final response = await _postStt(
        audioBase64,
      ).timeout(_ChatVoice._sttTimeout);
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        text = (data['text'] as String? ?? '').trim();
        failureNotice = '// NO SPEECH HEARD — try again or type.';
      } else {
        debugPrint('STT failed: HTTP ${response.statusCode}');
        // Only fixed notices reach the UI; never display a provider body.
        if (response.statusCode == 402) {
          failureNotice = '// VOICE INPUT NEEDS API CREDIT — use text for now.';
        } else if (response.statusCode == 413) {
          failureNotice =
              '// RECORDING TOO LARGE — record a shorter clip or type.';
        } else if (response.statusCode == 429) {
          failureNotice = '// VOICE SERVICE BUSY — try later or type.';
        } else if (response.statusCode == 503) {
          try {
            final data = jsonDecode(response.body);
            if (data is Map && data['error'] == 'Voice service busy') {
              failureNotice = '// VOICE SERVICE BUSY — try later or type.';
            }
          } catch (_) {
            // An unavailable proxy may return HTML instead of the API shape.
          }
        }
      }
    } on TimeoutException {
      failureNotice = '// VOICE INPUT TIMED OUT — try again or type.';
    } catch (_) {
      debugPrint('STT failed: unavailable or invalid response');
    }
    if (!mounted || !_speech.isCurrent(epoch)) return;
    final spoken = SpeechCapture.voiceUtterance(text);
    final kept = SpeechCapture.draftAfterTranscript(
      _textController.text,
      spoken,
    );
    if (kept != _textController.text) _textController.text = kept;
    if (spoken.isEmpty) {
      _update(() => _speech.fail(epoch, 'stt'));
      _showVoiceLinkError(failureNotice);
      return;
    }
    if (!_ws.sendMessage(spoken)) {
      _update(() => _speech.fail(epoch, 'send'));
      _showSendError();
      return;
    }
    _update(() {
      _speech.succeed(epoch);
      _messages.add(
        ChatMessage(
          id: _newLocalMessageId('user'),
          role: 'user',
          content: spoken,
        ),
      );
    });
    _scrollToBottom();
    _startWarmupHint(); // same cold-model wait as a typed message
  }
}
