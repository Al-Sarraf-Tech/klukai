/// Server cursors retain the exact timestamp (including sub-millisecond digits).
class MessageCursor {
  const MessageCursor({required this.createdAt, required this.id});
  final String createdAt;
  final String id;
  factory MessageCursor.fromJson(Map<String, dynamic> json) => MessageCursor(
    createdAt: json['created_at'] as String,
    id: json['id'] as String,
  );
  Map<String, dynamic> toJson() => {'created_at': createdAt, 'id': id};
}

class ConversationSearchHit {
  const ConversationSearchHit({
    required this.id,
    required this.role,
    required this.excerpt,
    required this.createdAt,
  });
  final String id, role, excerpt;
  final DateTime createdAt;
  factory ConversationSearchHit.fromJson(Map<String, dynamic> json) =>
      ConversationSearchHit(
        id: json['id'] as String,
        role: json['role'] as String,
        excerpt: json['excerpt'] as String,
        createdAt: DateTime.parse(json['created_at'] as String),
      );
}

class ConversationSourceMessage {
  const ConversationSourceMessage({
    required this.id,
    required this.role,
    required this.content,
    required this.createdAt,
  });
  final String id, role, content;
  final DateTime createdAt;
  factory ConversationSourceMessage.fromJson(Map<String, dynamic> json) =>
      ConversationSourceMessage(
        id: json['id'] as String,
        role: json['role'] as String,
        content: json['content'] as String,
        createdAt: DateTime.parse(json['created_at'] as String),
      );
}

MessageCursor? _cursor(dynamic value) => value == null
    ? null
    : MessageCursor.fromJson(value as Map<String, dynamic>);

class ConversationSearchPage {
  const ConversationSearchPage({required this.messages, this.nextCursor});
  final List<ConversationSearchHit> messages;
  final MessageCursor? nextCursor;
  factory ConversationSearchPage.fromJson(Map<String, dynamic> json) =>
      ConversationSearchPage(
        messages: (json['messages'] as List)
            .map(
              (m) => ConversationSearchHit.fromJson(m as Map<String, dynamic>),
            )
            .toList(),
        nextCursor: _cursor(json['next_cursor']),
      );
}

class ConversationContextPage {
  const ConversationContextPage({
    required this.anchorId,
    required this.messages,
    this.olderCursor,
    this.newerCursor,
  });
  final String anchorId;
  final List<ConversationSourceMessage> messages;
  final MessageCursor? olderCursor, newerCursor;
  factory ConversationContextPage.fromJson(Map<String, dynamic> json) =>
      ConversationContextPage(
        anchorId: json['anchor_id'] as String,
        messages: (json['messages'] as List)
            .map(
              (m) =>
                  ConversationSourceMessage.fromJson(m as Map<String, dynamic>),
            )
            .toList(),
        olderCursor: _cursor(json['older_cursor']),
        newerCursor: _cursor(json['newer_cursor']),
      );
}
