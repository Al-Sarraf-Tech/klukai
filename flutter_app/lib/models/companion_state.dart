class CompanionState {
  final String mood;
  final bool isConnected;
  final bool isTyping;
  final String? currentModel;

  const CompanionState({
    this.mood = 'neutral',
    this.isConnected = false,
    this.isTyping = false,
    this.currentModel,
  });

  CompanionState copyWith({
    String? mood,
    bool? isConnected,
    bool? isTyping,
    String? currentModel,
  }) {
    return CompanionState(
      mood: mood ?? this.mood,
      isConnected: isConnected ?? this.isConnected,
      isTyping: isTyping ?? this.isTyping,
      currentModel: currentModel ?? this.currentModel,
    );
  }
}
