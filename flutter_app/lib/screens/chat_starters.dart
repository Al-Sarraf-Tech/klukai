part of 'chat_screen.dart';

/// Character-specific conversation starters and their presentation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ChatStarters on _ChatScreenState {
  List<String> _getStarters() {
    final level = _state.affectionLevel;
    final hour = DateTime.now().hour;
    final isMorning = hour >= 6 && hour < 12;
    final isEvening = hour >= 18 || hour < 6;

    if (level <= 2) {
      return [
        'Mission briefing',
        'Status report',
        if (isMorning) "What's the plan today?" else "How's the squad?",
        'Tell me about yourself',
      ];
    } else if (level <= 4) {
      return [
        if (isMorning)
          'Good morning'
        else if (isEvening)
          'Good evening'
        else
          "How's your day?",
        'Tell me about Belka',
        "How's the squad?",
        "Let's go for a ride",
      ];
    } else if (level <= 6) {
      return [
        if (isMorning)
          'Good morning, Klukai'
        else if (isEvening)
          "Can't sleep?"
        else
          'I was thinking about you',
        'Tell me about Mechty',
        "What's on your mind?",
        "I missed you",
      ];
    } else {
      return [
        if (isMorning)
          'Good morning, beautiful'
        else if (isEvening)
          'Come sit with me'
        else
          'I love you',
        "What are you thinking about?",
        "Tell me a memory",
        "I'm here",
      ];
    }
  }

  Widget _starterChip(String text) {
    return GestureDetector(
      onTap: () {
        _textController.text = text;
        _sendMessage();
      },
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: GFL2Colors.surface,
          borderRadius: BorderRadius.circular(4),
          border: Border.all(color: GFL2Colors.primary.withValues(alpha: 0.3)),
        ),
        child: Text(
          text,
          style: TextStyle(
            color: GFL2Colors.primary.withValues(alpha: 0.7),
            fontSize: 12,
            fontFamily: 'monospace',
          ),
        ),
      ),
    );
  }
}
