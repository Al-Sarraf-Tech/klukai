class CompanionState {
  final String mood;
  final bool isConnected;
  final bool isTyping;
  final String? currentModel;
  final int affectionScore;
  final int affectionLevel;
  final String affectionLevelName;
  final bool isInputLocked;
  final String? inputLockReason;

  const CompanionState({
    this.mood = 'composed',
    this.isConnected = false,
    this.isTyping = false,
    this.currentModel,
    this.affectionScore = 0,
    this.affectionLevel = 0,
    this.affectionLevelName = 'Cold Assessment',
    this.isInputLocked = false,
    this.inputLockReason,
  });

  CompanionState copyWith({
    String? mood,
    bool? isConnected,
    bool? isTyping,
    String? currentModel,
    int? affectionScore,
    int? affectionLevel,
    String? affectionLevelName,
    bool? isInputLocked,
    String? inputLockReason,
  }) {
    return CompanionState(
      mood: mood ?? this.mood,
      isConnected: isConnected ?? this.isConnected,
      isTyping: isTyping ?? this.isTyping,
      currentModel: currentModel ?? this.currentModel,
      affectionScore: affectionScore ?? this.affectionScore,
      affectionLevel: affectionLevel ?? this.affectionLevel,
      affectionLevelName: affectionLevelName ?? this.affectionLevelName,
      isInputLocked: isInputLocked ?? this.isInputLocked,
      inputLockReason: inputLockReason,
    );
  }
}
