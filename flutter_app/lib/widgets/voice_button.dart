import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../theme/gfl2_colors.dart';

class VoiceButton extends StatefulWidget {
  final VoidCallback? onTapDown;
  final VoidCallback? onTapUp;
  final VoidCallback? onCancel;
  final bool isRecording;
  final bool enabled;

  const VoiceButton({
    super.key,
    this.onTapDown,
    this.onTapUp,
    this.onCancel,
    this.isRecording = false,
    this.enabled = true,
  });

  @override
  State<VoiceButton> createState() => _VoiceButtonState();
}

class _VoiceButtonState extends State<VoiceButton>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;
  final FocusNode _focusNode = FocusNode();
  bool _focused = false;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1000),
    );
    _focusNode.addListener(() {
      if (_focused != _focusNode.hasFocus && mounted) {
        setState(() => _focused = _focusNode.hasFocus);
      }
    });
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
    _focusNode.dispose();
    super.dispose();
  }

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (event.logicalKey == LogicalKeyboardKey.escape) {
      widget.onCancel?.call();
      return KeyEventResult.handled;
    }
    final activate =
        event.logicalKey == LogicalKeyboardKey.enter ||
        event.logicalKey == LogicalKeyboardKey.space;
    if (!activate) return KeyEventResult.ignored;
    if (!widget.enabled) return KeyEventResult.handled;
    if (widget.isRecording) {
      widget.onTapUp?.call();
    } else {
      widget.onTapDown?.call();
    }
    return KeyEventResult.handled;
  }

  @override
  Widget build(BuildContext context) {
    final label = widget.isRecording ? 'Stop voice' : 'Start voice';
    return Semantics(
      button: true,
      enabled: widget.enabled,
      label: label,
      child: Focus(
        focusNode: _focusNode,
        onKeyEvent: _onKey,
        child: GestureDetector(
          onTapDown: widget.enabled
              ? (_) {
                  _focusNode.requestFocus();
                  widget.onTapDown?.call();
                }
              : null,
          onTapUp: widget.enabled ? (_) => widget.onTapUp?.call() : null,
          onTapCancel: widget.enabled ? () => widget.onCancel?.call() : null,
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
                        ? GFL2Colors.danger
                        : widget.enabled
                        ? GFL2Colors.primary.withValues(alpha: 0.15)
                        : GFL2Colors.border,
                    border: Border.all(
                      color: _focused
                          ? GFL2Colors.primary
                          : widget.isRecording
                          ? GFL2Colors.danger
                          : widget.enabled
                          ? GFL2Colors.primary.withValues(alpha: 0.4)
                          : GFL2Colors.border,
                      width: _focused ? 2.5 : 1.5,
                    ),
                  ),
                  child: Icon(
                    widget.isRecording ? Icons.stop : Icons.mic,
                    color: widget.isRecording
                        ? Colors.white
                        : widget.enabled
                        ? GFL2Colors.primary
                        : GFL2Colors.textDim,
                    size: 20,
                  ),
                ),
              );
            },
          ),
        ),
      ),
    );
  }
}
