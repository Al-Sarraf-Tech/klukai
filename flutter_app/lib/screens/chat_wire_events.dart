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
        _clearWarmupHint(); // she's replying — drop the fallback waiting notice
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
            _streamingId = _newLocalMessageId('streaming');
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
        final up = (msg['direction'] as String? ?? 'up') == 'up';
        if (mounted) {
          final color = up ? HudColors.violet : HudColors.danger;
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Row(
                children: [
                  Icon(
                    up ? Icons.arrow_upward : Icons.arrow_downward,
                    color: color,
                    size: 16,
                  ),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      'TRUST LEVEL: $levelName',
                      style: HudText.label(
                        size: 11,
                        color: HudColors.text,
                        weight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
              backgroundColor: const Color(0xFF0B1118),
              behavior: SnackBarBehavior.floating,
              shape: CutCornerBorder(
                cut: 8,
                side: BorderSide(color: color.withValues(alpha: 0.5)),
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
              id: _newLocalMessageId('proactive'),
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
        // The server's spoken reply to a push-to-talk turn (from any of his
        // devices). Owner 2026-10-19: her voice plays only when he taps
        // `▶ VOICE`, never by itself — the clip is dropped; her line is in
        // the transcript and its VOICE reads it on demand.
        break;

      case 'voice_letter':
        // A voice note she left while you were away. Show the text like a
        // proactive line; her recorded note plays from that line's `▶ VOICE`
        // (never by itself). Without a note, VOICE reads the text.
        final letter = msg['message'] as String? ?? '';
        final voiceId = msg['voice_id'] as String?;
        if (letter.isNotEmpty) {
          final id = _newLocalMessageId('voice-letter');
          _update(() {
            if (voiceId != null && voiceId.isNotEmpty) {
              _voiceNotes[id] = voiceId;
            }
            _messages.add(
              ChatMessage(id: id, role: 'assistant', content: letter),
            );
          });
          _scrollToBottom();
          _playNotificationSound();
        }

      case 'image':
        final imgData = msg['data'] as String?;
        if (imgData != null) {
          _lockInput('IMAGE INCOMING', timeout: const Duration(seconds: 2));
          _update(() {
            // The picture ends the wait for it: take down "Compiling tactical
            // visualization" now, not on his next message. A reply that is
            // still streaming keeps its own state.
            if (_streamingId == null) {
              _state = _state.copyWith(isTyping: false);
              _thinkingText = null;
            }
            _messages.add(
              ChatMessage(
                id: _newLocalMessageId('image'),
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

      case 'heartbeat':
        // Her live heart rate, once per turn from the core. It shows until
        // the next one (a surge still wins while it lasts) and eases back to
        // her mood's resting rate after 15 quiet minutes. A beat without an
        // integer bpm is malformed and ignored; one out of range is clamped.
        // Her mood is the `mood` frame's to set, not this one's.
        final liveBpm = liveBpmFrom(msg['bpm']);
        if (liveBpm != null) _setLiveHeartbeat(liveBpm);

      case 'heartbeat_spike':
        final rawSpike = msg['bpm'];
        final spikeBpm = rawSpike is int ? rawSpike : 160;
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
              id: _newLocalMessageId('error'),
              role: 'assistant',
              content: errText,
            ),
          );
        });
        _unlockInput();
        _scrollToBottom();
    }
  }

  /// A `heartbeat` frame's reading becomes her live rate for the next
  /// [kLiveHeartbeatTtl], then her mood's resting rate returns. The timer
  /// redraws it on time; [shownHeartRate] also checks the wall clock, in
  /// case a backgrounded tab ran the timer late.
  void _setLiveHeartbeat(int bpm) {
    _liveBpmExpiry?.cancel();
    _update(() {
      _liveBpm = bpm;
      _liveBpmAt = DateTime.now();
    });
    _liveBpmExpiry = Timer(kLiveHeartbeatTtl, () {
      if (!mounted) return;
      _update(() {
        _liveBpm = null;
        _liveBpmAt = null;
      });
    });
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
