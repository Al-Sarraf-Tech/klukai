part of 'chat_screen.dart';

/// History status belongs to the transcript, not a transient toast. A failed
/// refresh never hides messages already on screen or changes the draft.
extension _ChatHistoryState on _ChatScreenState {
  /// Status shares the transcript's available height. Long error copy at large
  /// text sizes scrolls here instead of pushing the composer under the keyboard.
  Widget _buildTranscriptArea() => LayoutBuilder(
    builder: (context, box) => Column(
      children: [
        if (_historyLoading || _historyFailed)
          ConstrainedBox(
            constraints: BoxConstraints(
              maxHeight: box.maxHeight * (_messages.isEmpty ? 1 : 0.5),
            ),
            child: SingleChildScrollView(child: _buildHistoryStatus()),
          ),
        Expanded(
          child: Stack(
            children: [
              Positioned.fill(child: _buildMessageList()),
              if (_showScrollFAB)
                Positioned(
                  bottom: 12,
                  right: 16,
                  child: HudSquareButton(
                    key: const Key('chat-latest'),
                    tooltip: 'Latest',
                    onTap: () => _scrollToBottom(),
                    child: const Icon(Icons.keyboard_arrow_down),
                  ),
                ),
            ],
          ),
        ),
      ],
    ),
  );

  Widget _buildHistoryStatus() => Semantics(
    key: const Key('chat-history-status'),
    liveRegion: true,
    child: _historyNotice(
      label: _historyFailed
          ? (_messages.isEmpty
                ? "Couldn't load your conversation. Try again."
                : "Couldn't refresh. Your loaded messages are still here.")
          : (_messages.isEmpty
                ? 'Loading your conversation…'
                : 'Refreshing your conversation…'),
      loading: _historyLoading,
      retryKey: const Key('chat-history-retry'),
      onRetry: _historyFailed ? _loadHistory : null,
    ),
  );

  Widget _historyNotice({
    required String label,
    required Key retryKey,
    required VoidCallback? onRetry,
    bool loading = false,
  }) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
    child: Row(
      children: [
        if (loading)
          const SizedBox(
            width: 14,
            height: 14,
            child: CircularProgressIndicator(strokeWidth: 1.5),
          )
        else
          const Icon(
            Icons.cloud_off_outlined,
            size: 18,
            color: HudColors.textDim,
          ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            label,
            style: HudText.body(size: 13, color: HudColors.textDim),
          ),
        ),
        if (onRetry != null)
          TextButton(
            key: retryKey,
            onPressed: onRetry,
            style: TextButton.styleFrom(
              foregroundColor: HudColors.cyan,
              minimumSize: const Size(64, 48),
            ),
            child: const Text('Retry'),
          ),
      ],
    ),
  );
}
