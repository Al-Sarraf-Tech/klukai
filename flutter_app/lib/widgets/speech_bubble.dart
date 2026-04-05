import 'dart:async';
import 'package:flutter/material.dart';
import '../main.dart';

class SpeechBubble extends StatefulWidget {
  final String text;
  final bool isStreaming;
  final VoidCallback? onDismiss;

  const SpeechBubble({
    super.key,
    required this.text,
    this.isStreaming = false,
    this.onDismiss,
  });

  @override
  State<SpeechBubble> createState() => _SpeechBubbleState();
}

class _SpeechBubbleState extends State<SpeechBubble>
    with SingleTickerProviderStateMixin {
  late AnimationController _fadeController;
  Timer? _fadeTimer;

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 500),
      value: 1.0,
    );
    _scheduleFade();
  }

  @override
  void didUpdateWidget(SpeechBubble old) {
    super.didUpdateWidget(old);
    if (widget.text != old.text || widget.isStreaming != old.isStreaming) {
      _fadeController.value = 1.0;
      _scheduleFade();
    }
  }

  void _scheduleFade() {
    _fadeTimer?.cancel();
    if (!widget.isStreaming && widget.text.isNotEmpty) {
      _fadeTimer = Timer(const Duration(seconds: 5), () {
        if (mounted) {
          _fadeController.reverse().then((_) {
            widget.onDismiss?.call();
          });
        }
      });
    }
  }

  @override
  void dispose() {
    _fadeTimer?.cancel();
    _fadeController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.text.isEmpty) return const SizedBox.shrink();

    final display = widget.text.length > 100
        ? '${widget.text.substring(0, 100)}...'
        : widget.text;

    return GestureDetector(
      onTap: () {
        _fadeController.reverse().then((_) {
          widget.onDismiss?.call();
        });
      },
      child: FadeTransition(
        opacity: _fadeController,
        child: Container(
          constraints: const BoxConstraints(maxWidth: 240),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: GFL2Colors.surface,
            borderRadius: const BorderRadius.only(
              topLeft: Radius.circular(12),
              topRight: Radius.circular(12),
              bottomRight: Radius.circular(12),
              bottomLeft: Radius.circular(4),
            ),
            border: Border.all(
              color: GFL2Colors.primary.withValues(alpha: 0.4),
            ),
            boxShadow: [
              BoxShadow(
                color: GFL2Colors.primary.withValues(alpha: 0.1),
                blurRadius: 8,
              ),
            ],
          ),
          child: Text(
            display,
            style: const TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 12,
              height: 1.4,
              fontFamily: 'monospace',
            ),
          ),
        ),
      ),
    );
  }
}
