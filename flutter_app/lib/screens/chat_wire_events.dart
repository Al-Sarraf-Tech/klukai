part of 'chat_screen.dart';

/// Inbound wire events and reply finalization, preserving protocol cases.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatWireEvents on _ChatScreenState {
  void _handleWSMessage(Map<String, dynamic> msg) {
    if (!mounted || _authExpired) return;
    final type = msg['type'] as String?;

    switch (type) {
      case 'token':
        final text = msg['text'] as String? ?? '';
        _clearWarmupHint(); // she's replying — drop any "waking up" notice
        // The first token always (re)arms the lock for the whole reply. A
        // shorter lock may be active (proactive 1.5s, image 2s, heartbeat 5s);
        // left alone it would lapse mid-reply, freeing the composer while she
        // is still talking. Later tokens only re-arm if the lock has lapsed.
        if (_streamingId == null || !_state.isInputLocked) {
          _lockInput(
            'RECEIVING TRANSMISSION',
            timeout: const Duration(seconds: 60),
          );
        }
        _update(() {
          _streamingBuffer += text;
          if (_streamingId == null) {
            _thinkingText = null;
            _streamingId = 'streaming-${DateTime.now().millisecondsSinceEpoch}';
            _messages.add(
              ChatMessage(
                id: _streamingId!,
                role: 'assistant',
                content: _streamingBuffer,
                isStreaming: true,
              ),
            );
          } else {
            final idx = _messages.indexWhere((m) => m.id == _streamingId);
            if (idx >= 0) {
              _messages[idx] = _messages[idx].copyWith(
                content: _streamingBuffer,
              );
            }
          }
        });
        _scrollToBottom(instant: true);

      case 'done':
        final model = msg['model'] as String?;
        // done.text is her reply after the server's narration fix-ups (e.g.
        // <think> blocks stripped). Showing it instead of the raw stream also
        // keeps the next history merge from adding a second copy of the line.
        final finalText = msg['text'];
        _update(() {
          _finalizeStreamingBubble(
            content: finalText is String && finalText.isNotEmpty
                ? finalText
                : null,
            model: model,
          );
          _state = _state.copyWith(currentModel: model);
        });
        _clearWarmupHint();
        _unlockInput();
        _playNotificationSound();

      case 'read_receipt':
        // Read receipts handled — messages default to 'read' status
        break;

      case 'mood':
        _update(() {
          _state = _state.copyWith(mood: msg['mood'] as String? ?? 'composed');
        });
        try {
          _jsSetAmbientMood(msg['mood'] as String? ?? 'composed');
        } catch (_) {}

      case 'thinking':
        _clearWarmupHint(); // a real status arrived; don't overwrite it later
        // Older frames carry `text`, current ones `content`. An empty frame is
        // the backend clearing its status line — it must take the spinner
        // down, not leave a permanent PROCESSING behind.
        final status = msg['text'] as String? ?? msg['content'] as String?;
        _update(() {
          if (status == null || status.isEmpty) {
            _state = _state.copyWith(isTyping: false);
            _thinkingText = null;
          } else {
            _state = _state.copyWith(isTyping: true);
            _thinkingText = status;
          }
        });

      case 'tool_use':
        final toolName = msg['tool'] as String? ?? 'unknown';
        final toolStatus = msg['status'] as String? ?? 'calling';
        _update(() {
          if (toolStatus == 'calling') {
            _activeTools.add({'tool': toolName, 'status': 'calling'});
          } else {
            for (var i = _activeTools.length - 1; i >= 0; i--) {
              if (_activeTools[i]['tool'] == toolName &&
                  _activeTools[i]['status'] == 'calling') {
                _activeTools[i] = {'tool': toolName, 'status': 'done'};
                break;
              }
            }
          }
        });

      case 'affection':
        _update(() {
          _lastAffectionDelta = msg['delta'] as int? ?? 0;
          _state = _state.copyWith(
            affectionScore: msg['score'] as int? ?? _state.affectionScore,
            affectionLevel: msg['level'] as int? ?? _state.affectionLevel,
            affectionLevelName:
                msg['level_name'] as String? ?? _state.affectionLevelName,
          );
        });

      case 'affection_level_change':
        final levelName = msg['level_name'] as String? ?? '';
        final direction = msg['direction'] as String? ?? 'up';
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Row(
                children: [
                  Icon(
                    direction == 'up'
                        ? Icons.arrow_upward
                        : Icons.arrow_downward,
                    color: direction == 'up'
                        ? GFL2Colors.success
                        : GFL2Colors.danger,
                    size: 16,
                  ),
                  const SizedBox(width: 8),
                  Text(
                    'TRUST LEVEL: $levelName',
                    style: const TextStyle(
                      color: GFL2Colors.textPrimary,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.8,
                      fontFamily: 'monospace',
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
              backgroundColor: GFL2Colors.surface,
              behavior: SnackBarBehavior.floating,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(4),
                side: BorderSide(
                  color: direction == 'up'
                      ? GFL2Colors.primary.withValues(alpha: 0.4)
                      : GFL2Colors.danger.withValues(alpha: 0.4),
                ),
              ),
              duration: const Duration(seconds: 4),
            ),
          );
        }

      case 'proactive':
        final message = msg['message'] as String? ?? '';
        _lockInput(
          'INCOMING TRANSMISSION',
          timeout: const Duration(milliseconds: 1500),
        );
        _update(() {
          _messages.add(
            ChatMessage(
              id: 'proactive-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: message,
            ),
          );
        });
        _scrollToBottom();
        _playNotificationSound();
        // Auto-unlock after brief pause
        Timer(const Duration(milliseconds: 1500), () {
          if (mounted && _state.inputLockReason == 'INCOMING TRANSMISSION') {
            _unlockInput();
          }
        });

      case 'voice_audio':
        final audioData = msg['audio'] as String?;
        if (audioData != null) {
          _playAudio(audioData);
        }

      case 'voice_letter':
        // A voice note she left while you were away. Show the text like a
        // proactive line, then fetch + play the audio (fail-soft: text stands
        // on its own if the audio can't be fetched).
        final letter = msg['message'] as String? ?? '';
        final voiceId = msg['voice_id'] as String?;
        if (letter.isNotEmpty) {
          _update(() {
            _messages.add(
              ChatMessage(
                id: 'voice-letter-${DateTime.now().millisecondsSinceEpoch}',
                role: 'assistant',
                content: letter,
              ),
            );
          });
          _scrollToBottom();
          _playNotificationSound();
        }
        if (voiceId != null && voiceId.isNotEmpty) {
          _fetchAndPlayVoiceNote(voiceId);
        }

      case 'image':
        final imgData = msg['data'] as String?;
        if (imgData != null) {
          _lockInput('IMAGE INCOMING', timeout: const Duration(seconds: 2));
          _update(() {
            _messages.add(
              ChatMessage(
                id: 'image-${DateTime.now().millisecondsSinceEpoch}',
                role: 'assistant',
                content: '[IMAGE]',
                imageData: imgData,
              ),
            );
          });
          // Retry scroll 3 times to catch image decode layout shifts
          _scrollToBottom(retries: 3);
          _playNotificationSound();
          Timer(const Duration(seconds: 2), () {
            if (mounted && _state.inputLockReason == 'IMAGE INCOMING') {
              _unlockInput();
            }
          });
        }

      case 'outfit':
        // She changed because he asked in chat. Show it now, then refetch so
        // the status line and schedule agree with the server.
        final raw = msg['outfit'];
        if (raw is Map<String, dynamic> && _herDay != null) {
          _update(
            () => _herDay = _herDay!.withOutfit(OutfitInfo.fromJson(raw)),
          );
        }
        _loadHerDay();

      case 'heartbeat_spike':
        final spikeBpm = msg['bpm'] as int? ?? 160;
        _lockInput('HEARTBEAT SURGE', timeout: const Duration(seconds: 5));
        _update(() {
          _heartbeatSpikeOverride = spikeBpm;
        });
        // Decay back to normal BPM after 5 seconds
        _spikeDecayTimer?.cancel();
        _spikeDecayTimer = Timer(const Duration(seconds: 5), () {
          if (mounted) {
            _update(() {
              _heartbeatSpikeOverride = null;
            });
            if (_state.inputLockReason == 'HEARTBEAT SURGE') _unlockInput();
          }
        });

      case 'error':
        // Backend hit a snag (handler crash / stream failure). Clear all the
        // in-flight indicators — including the warmup hint — so the UI never
        // sticks on "WAKING UP", and show her message as a normal bubble. A
        // reply cut off mid-stream keeps what arrived, frozen: no cursor
        // blinking forever on a bubble nothing will ever finish.
        _clearWarmupHint();
        final errText =
            msg['message'] as String? ??
            '...Something glitched on my end, Commander. Try again.';
        _update(() {
          _finalizeStreamingBubble();
          _messages.add(
            ChatMessage(
              id: 'error-${DateTime.now().millisecondsSinceEpoch}',
              role: 'assistant',
              content: errText,
            ),
          );
        });
        _unlockInput();
        _scrollToBottom();
    }
  }

  /// Ends the in-progress reply where it stands: the streaming bubble (if any)
  /// stops blinking and keeps what streamed — or takes [content], the server's
  /// final text — and every per-reply indicator (buffer, status line, tool
  /// list, typing) is reset. Shared by `done`, `error` and a dropped link.
  /// Must be called inside setState. Returns the finalized bubble's index so
  /// the caller can prepare its layout, or null when nothing was streaming.
  int? _finalizeStreamingBubble({String? content, String? model}) {
    int? idx;
    final id = _streamingId;
    if (id != null) {
      final i = _messages.indexWhere((m) => m.id == id);
      if (i >= 0) {
        _messages[i] = _messages[i].copyWith(
          isStreaming: false,
          content: content,
          model: model,
        );
        idx = i;
      }
    }
    _streamingBuffer = '';
    _streamingId = null;
    _thinkingText = null;
    _activeTools.clear();
    _state = _state.copyWith(isTyping: false);
    return idx;
  }
}
