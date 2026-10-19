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
    try {
      final uri = Uri.parse('${widget.serverUrl}/api/messages?limit=50');
      final response = await _getChat(uri);
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final messages = (data['messages'] as List)
            .map((m) => ChatMessage.fromJson(m))
            .toList();
        if (!mounted || _authExpired) return;
        _mergeHistory(messages);
        _scrollToBottom(instant: true);
      } else if (response.statusCode == 401) {
        // Expired token. Don't wait for the WebSocket's 4001 close — under
        // uvicorn that close never reaches the client, and the app would sit
        // soft-locked on LINK DOWN with an empty transcript. Back to login.
        if (mounted) _handleAuthExpired();
      } else if (mounted) {
        // Don't render an empty conversation when the server actually errored —
        // history is sacred; offer a retry so it can't be mistaken for "wiped".
        _showHistoryError();
      }
    } catch (e) {
      debugPrint('Failed to load history: $e');
      if (mounted) _showHistoryError();
    }
  }

  /// Merge fetched history into [_messages] by id — idempotent, so the
  /// initial load, a RETRY, and a reconnect refetch can all share it without
  /// ever duplicating a message. Locally-echoed messages (synthetic ids like
  /// `user-…`/`streaming-…`) are matched by role+content so the server copy
  /// of a message we already rendered isn't shown twice. New messages are
  /// inserted in timestamp order, preserving the live conversation flow.
  void _mergeHistory(List<ChatMessage> fetched) {
    bool isLocalEchoOf(ChatMessage m) => _messages.any(
      (x) =>
          x.id != m.id &&
          x.role == m.role &&
          x.content == m.content &&
          (x.id.startsWith('user-') ||
              x.id.startsWith('streaming-') ||
              x.id.startsWith('proactive-') ||
              x.id.startsWith('image-')),
    );
    final known = _messages.map((m) => m.id).toSet();
    final additions =
        fetched
            .where((m) => !known.contains(m.id) && !isLocalEchoOf(m))
            .toList()
          ..sort((a, b) => a.createdAt.compareTo(b.createdAt));
    if (additions.isEmpty) return;
    _update(() {
      for (final m in additions) {
        var idx = _messages.length;
        while (idx > 0 && _messages[idx - 1].createdAt.isAfter(m.createdAt)) {
          idx--;
        }
        _messages.insert(idx, m);
      }
    });
  }

  void _showHistoryError() {
    if (!mounted || _authExpired) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          "COULDN'T LOAD HISTORY",
          style: HudText.label(size: 11, color: HudColors.text),
        ),
        backgroundColor: const Color(0xFF0B1118),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 6),
        shape: CutCornerBorder(
          cut: 8,
          side: BorderSide(color: HudColors.danger.withValues(alpha: 0.5)),
        ),
        action: SnackBarAction(
          label: 'RETRY',
          textColor: HudColors.cyan,
          onPressed: _loadHistory,
        ),
      ),
    );
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
