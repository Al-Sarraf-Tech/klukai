class ChatMessage {
  final String id;

  /// Stable identity for this displayed reply, even when a local echo adopts
  /// its persisted row id. Repeated text on another reply has its own identity.
  final String presentationId;
  final String role; // 'user' or 'assistant'
  final String content;
  final String mood;
  final String? model;
  final DateTime createdAt;
  final bool isStreaming;
  final int? pretextHandle;
  final double? tightBubbleWidth;
  final String? imageData;
  final String status; // 'sending', 'sent', 'read'

  ChatMessage({
    required this.id,
    String? presentationId,
    required this.role,
    required this.content,
    this.mood = 'neutral',
    this.model,
    DateTime? createdAt,
    this.isStreaming = false,
    this.pretextHandle,
    this.tightBubbleWidth,
    this.imageData,
    this.status = 'read',
  }) : presentationId = presentationId ?? id,
       createdAt = createdAt ?? DateTime.now();

  ChatMessage copyWith({
    String? presentationId,
    String? content,
    String? mood,
    String? model,
    bool? isStreaming,
    int? pretextHandle,
    double? tightBubbleWidth,
    String? imageData,
    String? status,
  }) {
    return ChatMessage(
      id: id,
      presentationId: presentationId ?? this.presentationId,
      role: role,
      content: content ?? this.content,
      mood: mood ?? this.mood,
      model: model ?? this.model,
      createdAt: createdAt,
      isStreaming: isStreaming ?? this.isStreaming,
      pretextHandle: pretextHandle ?? this.pretextHandle,
      tightBubbleWidth: tightBubbleWidth ?? this.tightBubbleWidth,
      imageData: imageData ?? this.imageData,
      status: status ?? this.status,
    );
  }

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    return ChatMessage(
      id: json['id'] ?? '',
      role: json['role'] ?? 'assistant',
      content: json['content'] ?? '',
      mood: json['mood'] ?? 'neutral',
      model: json['model'],
      // The server stamps history in UTC ("...Z"). Convert at the edge so the
      // bubble clock, the date dividers and dorm mode all read wall-clock time
      // (ThreadEntry.fromJson does the same).
      createdAt: json['created_at'] != null
          ? DateTime.parse(json['created_at']).toLocal()
          : DateTime.now(),
      status: json['status'] as String? ?? 'read',
    );
  }
}
