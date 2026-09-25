/// The ten-year thread: the messages Klukai sent during the Mephisto years.
class ThreadEntry {
  final String stamp;
  final String text;
  final DateTime? readAt;

  const ThreadEntry({required this.stamp, required this.text, this.readAt});

  bool get isRead => readAt != null;

  ThreadEntry markedRead(DateTime at) =>
      ThreadEntry(stamp: stamp, text: text, readAt: readAt ?? at);

  factory ThreadEntry.fromJson(Map<String, dynamic> json) => ThreadEntry(
        stamp: json['stamp'] as String? ?? '',
        text: json['text'] as String? ?? '',
        readAt: DateTime.tryParse(json['read_at'] as String? ?? '')?.toLocal(),
      );
}

class ThreadReply {
  final String stamp;
  final String text;

  const ThreadReply({required this.stamp, required this.text});

  factory ThreadReply.fromJson(Map<String, dynamic> json) => ThreadReply(
        stamp: json['stamp'] as String? ?? '',
        text: json['text'] as String? ?? '',
      );
}

class ThreadView {
  final bool sealed;

  /// Her line while the thread is sealed.
  final String? message;
  final List<ThreadEntry> entries;

  /// Entries that exist but she isn't ready to show yet.
  final int heldBack;

  /// The Commander's only reply, shown at the end of the thread.
  final ThreadReply? reply;

  const ThreadView({
    required this.sealed,
    this.message,
    this.entries = const [],
    this.heldBack = 0,
    this.reply,
  });

  factory ThreadView.fromJson(Map<String, dynamic> json) {
    final reply = json['reply'];
    return ThreadView(
      sealed: json['status'] != 'open',
      message: json['message'] as String?,
      entries: (json['entries'] as List? ?? const [])
          .map((e) => ThreadEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      heldBack: json['held_back'] as int? ?? 0,
      reply: reply is Map<String, dynamic> ? ThreadReply.fromJson(reply) : null,
    );
  }
}
