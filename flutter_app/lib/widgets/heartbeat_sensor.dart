import 'dart:math';
import 'package:flutter/material.dart';

/// Animated heartbeat sensor showing Klukai's BPM based on mood.
/// Displays a pulsing heart icon + BPM number + animated ECG-style line.
class HeartbeatSensor extends StatefulWidget {
  final int bpm;
  final Color color;

  const HeartbeatSensor({super.key, required this.bpm, required this.color});

  @override
  State<HeartbeatSensor> createState() => _HeartbeatSensorState();
}

class _HeartbeatSensorState extends State<HeartbeatSensor>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _pulseAnimation;

  @override
  void initState() {
    super.initState();
    _setupAnimation();
  }

  @override
  void didUpdateWidget(HeartbeatSensor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.bpm != widget.bpm) {
      _controller.stop();
      _setupAnimation();
    }
  }

  void _setupAnimation() {
    final duration = Duration(milliseconds: (60000 / widget.bpm).round());
    _controller = AnimationController(vsync: this, duration: duration)
      ..repeat();
    _pulseAnimation = TweenSequence<double>([
      TweenSequenceItem(tween: Tween(begin: 1.0, end: 1.3), weight: 15),
      TweenSequenceItem(tween: Tween(begin: 1.3, end: 1.0), weight: 15),
      TweenSequenceItem(tween: Tween(begin: 1.0, end: 1.15), weight: 10),
      TweenSequenceItem(tween: Tween(begin: 1.15, end: 1.0), weight: 10),
      TweenSequenceItem(tween: Tween(begin: 1.0, end: 1.0), weight: 50),
    ]).animate(CurvedAnimation(parent: _controller, curve: Curves.easeInOut));
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        // Pulsing heart
        AnimatedBuilder(
          animation: _pulseAnimation,
          builder: (context, child) {
            return Transform.scale(
              scale: _pulseAnimation.value,
              child: Icon(
                Icons.favorite,
                color: widget.color.withValues(alpha: 0.8),
                size: 10,
              ),
            );
          },
        ),
        const SizedBox(width: 4),
        // BPM number
        Text(
          '${widget.bpm}',
          style: TextStyle(
            color: widget.color.withValues(alpha: 0.6),
            fontSize: 9,
            fontWeight: FontWeight.w700,
            fontFamily: 'monospace',
          ),
        ),
        const SizedBox(width: 2),
        Text(
          'BPM',
          style: TextStyle(
            color: widget.color.withValues(alpha: 0.3),
            fontSize: 7,
            fontWeight: FontWeight.w600,
            fontFamily: 'monospace',
            letterSpacing: 0.5,
          ),
        ),
        const SizedBox(width: 4),
        // Animated ECG line
        SizedBox(
          width: 40,
          height: 12,
          child: AnimatedBuilder(
            animation: _controller,
            builder: (context, child) {
              return CustomPaint(
                painter: _ECGPainter(
                  progress: _controller.value,
                  color: widget.color.withValues(alpha: 0.5),
                  bpm: widget.bpm,
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _ECGPainter extends CustomPainter {
  final double progress;
  final Color color;
  final int bpm;

  _ECGPainter({required this.progress, required this.color, required this.bpm});

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 1.0
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round;

    final path = Path();
    final midY = size.height / 2;
    final spikeHeight = min(size.height * 0.8, (bpm / 180.0) * size.height);

    // Draw ECG waveform scrolling left
    final offset = progress * size.width;
    path.moveTo(0, midY);

    for (double x = 0; x < size.width; x += 1) {
      final nx = (x + offset) % size.width;
      final t = nx / size.width;
      double y = midY;

      // QRS complex spike pattern
      if (t > 0.3 && t < 0.35) {
        y = midY + spikeHeight * 0.3;
      } else if (t > 0.35 && t < 0.4) {
        y = midY - spikeHeight;
      } else if (t > 0.4 && t < 0.45) {
        y = midY + spikeHeight * 0.4;
      } else if (t > 0.45 && t < 0.5) {
        y = midY;
      } else if (t > 0.6 && t < 0.7) {
        // T-wave
        y = midY - spikeHeight * 0.2;
      }

      if (x == 0) {
        path.moveTo(x, y);
      } else {
        path.lineTo(x, y);
      }
    }

    canvas.drawPath(path, paint);
  }

  @override
  bool shouldRepaint(_ECGPainter oldDelegate) =>
      oldDelegate.progress != progress || oldDelegate.color != color;
}
