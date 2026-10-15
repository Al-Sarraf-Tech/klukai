import 'package:flutter/material.dart';
import '../models/message.dart';
import 'message_bubble.dart';

/// Compatibility for older imports. Conversation rendering is always native.
@Deprecated('Use MessageBubble for streaming and completed messages')
class CanvasMessageBubble extends StatelessWidget {
  const CanvasMessageBubble({super.key, required this.message});
  final ChatMessage message;
  @override
  Widget build(BuildContext context) => MessageBubble(message: message);
}
