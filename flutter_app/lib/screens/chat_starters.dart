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
    const shape = CutCornerBorder(
      cut: 8,
      side: BorderSide(color: Color(0x594FD1FF)),
    );
    return Semantics(
      button: true,
      label: text,
      excludeSemantics: true,
      child: Material(
        type: MaterialType.transparency,
        child: InkWell(
          customBorder: shape,
          onTap: () {
            _textController.text = text;
            _sendMessage();
          },
          child: Ink(
            decoration: ShapeDecoration(
              color: HudColors.cyanA(0.05),
              shape: shape,
            ),
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 44),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 14),
                child: Center(
                  widthFactor: 1,
                  child: Text(
                    text,
                    style: HudText.body(size: 14, color: HudColors.textDim),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
