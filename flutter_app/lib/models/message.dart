class ChatMessage {
  final String id;
  final String role; // 'user' or 'assistant'
  final String content;
  final String mood;
  final String? model;
  final DateTime createdAt;
  final bool isStreaming;
  final int? pretextHandle;
  final double? tightBubbleWidth;

  ChatMessage({
    required this.id,
    required this.role,
    required this.content,
    this.mood = 'neutral',
    this.model,
    DateTime? createdAt,
    this.isStreaming = false,
    this.pretextHandle,
    this.tightBubbleWidth,
  }) : createdAt = createdAt ?? DateTime.now();

  ChatMessage copyWith({
    String? content,
    String? mood,
    String? model,
    bool? isStreaming,
    int? pretextHandle,
    double? tightBubbleWidth,
  }) {
    return ChatMessage(
      id: id,
      role: role,
      content: content ?? this.content,
      mood: mood ?? this.mood,
      model: model ?? this.model,
      createdAt: createdAt,
      isStreaming: isStreaming ?? this.isStreaming,
      pretextHandle: pretextHandle ?? this.pretextHandle,
      tightBubbleWidth: tightBubbleWidth ?? this.tightBubbleWidth,
    );
  }

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    return ChatMessage(
      id: json['id'] ?? '',
      role: json['role'] ?? 'assistant',
      content: json['content'] ?? '',
      mood: json['mood'] ?? 'neutral',
      model: json['model'],
      createdAt: json['created_at'] != null
          ? DateTime.parse(json['created_at'])
          : DateTime.now(),
    );
  }
}
