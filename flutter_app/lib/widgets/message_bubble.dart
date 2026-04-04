import 'package:flutter/material.dart';
import '../models/message.dart';
import '../main.dart';

class MessageBubble extends StatelessWidget {
  final ChatMessage message;

  const MessageBubble({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == 'user';
    final isMobile = MediaQuery.of(context).size.width < 600;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      child: Row(
        mainAxisAlignment:
            isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Flexible(
            child: Container(
              constraints: BoxConstraints(
                maxWidth: message.tightBubbleWidth ??
                    MediaQuery.of(context).size.width * (isMobile ? 0.85 : 0.75),
              ),
              decoration: BoxDecoration(
                color: isUser ? GFL2Colors.panel : GFL2Colors.surface,
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(4),
                  topRight: const Radius.circular(4),
                  bottomLeft: Radius.circular(isUser ? 4 : 0),
                  bottomRight: Radius.circular(isUser ? 0 : 4),
                ),
                border: Border(
                  left: isUser
                      ? BorderSide.none
                      : const BorderSide(color: GFL2Colors.primary, width: 2),
                  right: isUser
                      ? const BorderSide(color: GFL2Colors.accent, width: 2)
                      : BorderSide.none,
                ),
              ),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Comm tag for Klukai
                    if (!isUser)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 4),
                        child: Text(
                          'KLUKAI // SST-05',
                          style: TextStyle(
                            color: GFL2Colors.primary.withValues(alpha: 0.45),
                            fontSize: 9,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 1.5,
                            fontFamily: 'monospace',
                          ),
                        ),
                      ),
                    SelectableText(
                      message.content,
                      style: TextStyle(
                        color: GFL2Colors.textPrimary.withValues(alpha: 0.92),
                        fontSize: 14,
                        height: 1.5,
                      ),
                    ),
                    if (message.isStreaming)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 1.5,
                            color: GFL2Colors.primary.withValues(alpha: 0.4),
                          ),
                        ),
                      ),
                    // Timestamp
                    if (!message.isStreaming)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Align(
                          alignment: Alignment.centerRight,
                          child: Text(
                            _formatTime(message.createdAt),
                            style: TextStyle(
                              color: GFL2Colors.textDim.withValues(alpha: 0.4),
                              fontSize: 9,
                              fontFamily: 'monospace',
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  String _formatTime(DateTime dt) {
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}
