import 'package:flutter/material.dart';

class VoiceButton extends StatefulWidget {
  final VoidCallback? onTapDown;
  final VoidCallback? onTapUp;
  final bool isRecording;
  final bool enabled;

  const VoiceButton({
    super.key,
    this.onTapDown,
    this.onTapUp,
    this.isRecording = false,
    this.enabled = true,
  });

  @override
  State<VoiceButton> createState() => _VoiceButtonState();
}

class _VoiceButtonState extends State<VoiceButton>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1000),
    );
  }

  @override
  void didUpdateWidget(VoiceButton oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.isRecording && !_pulseController.isAnimating) {
      _pulseController.repeat(reverse: true);
    } else if (!widget.isRecording && _pulseController.isAnimating) {
      _pulseController.stop();
      _pulseController.reset();
    }
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTapDown: widget.enabled ? (_) => widget.onTapDown?.call() : null,
      onTapUp: widget.enabled ? (_) => widget.onTapUp?.call() : null,
      onTapCancel: widget.enabled ? () => widget.onTapUp?.call() : null,
      child: AnimatedBuilder(
        animation: _pulseController,
        builder: (context, child) {
          final scale = widget.isRecording
              ? 1.0 + (_pulseController.value * 0.15)
              : 1.0;
          return Transform.scale(
            scale: scale,
            child: Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: widget.isRecording
                    ? const Color(0xFFEF4444)
                    : widget.enabled
                        ? const Color(0xFF7C3AED)
                        : const Color(0xFF374151),
              ),
              child: Icon(
                widget.isRecording ? Icons.stop : Icons.mic,
                color: Colors.white,
                size: 22,
              ),
            ),
          );
        },
      ),
    );
  }
}
