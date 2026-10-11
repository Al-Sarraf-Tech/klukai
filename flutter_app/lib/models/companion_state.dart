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
    final locked = isInputLocked ?? this.isInputLocked;
    return CompanionState(
      mood: mood ?? this.mood,
      isConnected: isConnected ?? this.isConnected,
      isTyping: isTyping ?? this.isTyping,
      currentModel: currentModel ?? this.currentModel,
      affectionScore: affectionScore ?? this.affectionScore,
      affectionLevel: affectionLevel ?? this.affectionLevel,
      affectionLevelName: affectionLevelName ?? this.affectionLevelName,
      isInputLocked: locked,
      // The reason belongs to the lock: it survives unrelated updates (a mood
      // or typing frame mid-reply used to drop it, flickering the composer
      // hint to "// STANDBY...") and is released together with the lock.
      inputLockReason: locked ? (inputLockReason ?? this.inputLockReason) : null,
    );
  }
}
