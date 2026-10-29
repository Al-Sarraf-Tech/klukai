part of 'chat_screen.dart';

/// Authenticated history merge, affection loading, and WebSocket connection rules.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatConnection on _ChatScreenState {
  Map<String, String> get _authHeaders {
    final token = _authToken ?? _getToken() ?? '';
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  Future<http.Response> _getChat(Uri uri) => widget.historyClient != null
      ? widget.historyClient!.get(uri, headers: _authHeaders)
      : http.get(uri, headers: _authHeaders);

  Future<void> _loadHistory() async {
    if (_authExpired) return;
    final generation = ++_historyGeneration;
    _update(() {
      _historyLoading = true;
      _historyFailed = false;
    });
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/messages?limit=50');
      final response = await _getChat(uri);
      if (!mounted || _authExpired) return;
      // Revocation is authoritative even when this request was superseded.
      if (response.statusCode == 401) {
        _handleAuthExpired();
        return;
      }
      if (generation != _historyGeneration) return;
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final messages = (data['messages'] as List)
            .map((m) => ChatMessage.fromJson(m))
            .toList();
        _mergeHistory(messages);
        if (_pinnedToBottom) _scrollToBottom(instant: true);
        _scheduleStageWarmUp();
      } else {
        _showHistoryError();
      }
    } catch (e) {
      debugPrint('Failed to load history: $e');
      if (mounted && !_authExpired && generation == _historyGeneration) {
        _showHistoryError();
      }
    } finally {
      if (mounted && !_authExpired && generation == _historyGeneration) {
        _update(() => _historyLoading = false);
      }
    }
  }

  /// Merge by canonical row id. A nearby matching local echo may become one
  /// server row, but repeated text elsewhere in history remains distinct.
  /// Echoes retain their arrival position to preserve playback state; tied
  /// UUIDs or small clock differences need not match their visual order.
  void _mergeHistory(List<ChatMessage> fetched) {
    if (fetched.isEmpty) return;
    final known = {..._messages.map((m) => m.id), ..._older.map((m) => m.id)};
    final unmatched = {
      for (var i = 0; i < _messages.length; i++)
        if (_isLocalId(_messages[i].id) && !_messages[i].isStreaming) i,
    };
    // Reconcile newest rows first; repeated words from a previous day must
    // never consume an echo of what he just said. Ambiguous older rows stay.
    final ordered = [...fetched]..sort((a, b) => _compareHistory(b, a));
    _update(() {
      final additions = <ChatMessage>[];
      for (final message in ordered) {
        if (!known.add(message.id)) continue;
        int? match;
        var nearest = const Duration(minutes: 5);
        for (final i in unmatched) {
          final echo = _messages[i];
          if (echo.role != message.role || echo.content != message.content) {
            continue;
          }
          final distance = echo.createdAt.difference(message.createdAt).abs();
          if (distance <= nearest) {
            match = i;
            nearest = distance;
          }
        }
        if (match != null) {
          unmatched.remove(match);
          final echo = _messages[match];
          // Keep local media/layout, playback identity and the bubble's place
          // while adopting the server id. Live streams are never candidates.
          _messages[match] = message.copyWith(
            presentationId: echo.presentationId,
            imageData: echo.imageData,
            pretextHandle: echo.pretextHandle,
            tightBubbleWidth: echo.tightBubbleWidth,
          );
          final note = _voiceNotes.remove(echo.id);
          if (note != null) _voiceNotes[message.id] = note;
        } else {
          additions.add(message);
        }
      }
      additions.sort(_compareHistory);
      for (final m in additions) {
        var idx = _messages.length;
        while (idx > 0 && _compareHistory(_messages[idx - 1], m) > 0) {
          idx--;
        }
        _messages.insert(idx, m);
      }
    });
  }

  /// Lines of one history page; a shorter page means the start was reached.
  static const int _historyPage = 50;

  /// Match the server's (created_at, id) cursor order. Dart's sort is not
  /// stable, so comparing only timestamps can select a cursor inside a tie.
  static int _compareHistory(ChatMessage a, ChatMessage b) {
    final time = a.createdAt.compareTo(b.createdAt);
    return time != 0 ? time : a.id.compareTo(b.id);
  }

  /// The page of history before the oldest line on screen, fetched when he
  /// scrolls near the top. Pages grow upward above the transcript's anchor,
  /// so what he is reading stays where it is.
  Future<void> _loadOlder({bool retry = false}) async {
    if (_loadingOlder ||
        _olderExhausted ||
        _authExpired ||
        (_olderFailed && !retry)) {
      return;
    }
    // Promoted echoes keep their screen positions, so select the canonical
    // minimum independently of display order before advancing the cursor.
    ChatMessage? oldest;
    for (final message in _older.followedBy(_messages)) {
      if (!_isHistoryCursorId(message.id)) continue;
      if (oldest == null || _compareHistory(message, oldest) < 0) {
        oldest = message;
      }
    }
    if (oldest == null) return;
    _update(() {
      _loadingOlder = true;
      _olderFailed = false;
    });
    try {
      final before = oldest.createdAt.toUtc().toIso8601String();
      final response = await _getChat(
        Uri.parse('${widget.serverUrl}/api/messages').replace(
          queryParameters: {
            'limit': '$_historyPage',
            'before': before,
            'before_id': oldest.id,
          },
        ),
      );
      if (!mounted || _authExpired) return;
      if (response.statusCode == 200) {
        final page = (jsonDecode(response.body)['messages'] as List)
            .map((m) => ChatMessage.fromJson(m))
            .toList();
        final known = {
          ..._messages.map((m) => m.id),
          ..._older.map((m) => m.id),
        };
        final fresh = page.where((m) => !known.contains(m.id)).toList()
          ..sort(_compareHistory);
        _update(() {
          _older.insertAll(0, fresh);
          if (page.length < _historyPage || fresh.isEmpty) {
            _olderExhausted = true;
          }
        });
      } else if (response.statusCode == 401) {
        _handleAuthExpired();
      } else {
        _update(() => _olderFailed = true);
      }
    } catch (e) {
      debugPrint('Failed to load older history: $e');
      if (mounted && !_authExpired) _update(() => _olderFailed = true);
    } finally {
      if (mounted) _update(() => _loadingOlder = false);
    }
  }

  /// Errors are local-only, not echoes awaiting a persisted copy. Keep cursor
  /// eligibility separate so excluding errors never makes them merge candidates.
  static bool _isHistoryCursorId(String id) =>
      id.isNotEmpty && !id.startsWith('error-') && !_isLocalId(id);

  /// Ids the client made up for a line before the server's copy arrived.
  static bool _isLocalId(String id) =>
      id.startsWith('user-') ||
      id.startsWith('streaming-') ||
      id.startsWith('proactive-') ||
      id.startsWith('voice-letter-') ||
      id.startsWith('image-');

  void _showHistoryError() {
    if (!mounted || _authExpired) return;
    _update(() => _historyFailed = true);
  }

  Future<void> _loadAffection() async {
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/affection');
      final response = await _getChat(uri);
      if (!mounted || _authExpired) return;
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        _update(() {
          _state = _state.copyWith(
            affectionScore: data['score'] as int? ?? 0,
            affectionLevel: data['level'] as int? ?? 0,
            affectionLevelName:
                data['level_name'] as String? ?? 'Cold Assessment',
          );
        });
      } else if (response.statusCode == 401) {
        _handleAuthExpired(); // same soft-lock as _loadHistory; see there
      }
    } catch (e) {
      debugPrint('Failed to load affection: $e');
    }
  }

  String? _getToken() {
    try {
      return web.window.localStorage.getItem('klukai_token');
    } catch (_) {
      return null;
    }
  }

  void _connectWS() {
    _authToken = _getToken();
    if (_authToken == null || _authToken!.isEmpty) {
      // No token — redirect to login page
      web.window.location.href = '/';
      return;
    }
    final wsUrl = '${widget.serverUrl.replaceFirst('http', 'ws')}/ws';
    _ws.connect(wsUrl, token: _authToken);
    _ws.connectionState.listen((connected) {
      if (!mounted || _authExpired) return;
      final wasConnected = _state.isConnected;
      _update(() {
        _state = _state.copyWith(isConnected: connected);
        // The link died (or was redialed) mid-reply: no more tokens are coming
        // for that bubble. Freeze it where it stands so the next reply gets a
        // bubble of its own instead of being appended to a dead one, and free
        // the composer instead of leaving it locked for the 60s safety timer.
        if (!connected) _finalizeStreamingBubble();
      });
      if (!connected) {
        _clearWarmupHint();
        _unlockInput();
      }
      if (connected && !wasConnected) {
        // (Re)connected — refetch recent history and merge by id so anything
        // missed while the link was down appears, without duplicates.
        _loadHistory();
        _loadHerDay();
      }
    });
    _ws.messages.listen(_handleWSMessage);
    _ws.authFailure.listen((_) => _handleAuthExpired());
  }

  void _handleAuthExpired() {
    if (_authExpired) return;
    _authExpired = true;
    _cancelVoice();
    _clearWarmupHint();
    _inputLockTimer?.cancel();
    _ws.disconnect();
    if (mounted) {
      _update(() {
        _finalizeStreamingBubble();
        _state = _state.copyWith(isConnected: false, isInputLocked: false);
      });
    }
    // Revoke locally before redirecting; late frames cannot reactivate chat.
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    final redirect = widget.onAuthExpired;
    if (redirect != null) {
      redirect();
    } else {
      web.window.location.href = '/';
    }
  }
}
