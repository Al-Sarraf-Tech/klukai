import 'package:flutter/material.dart';

/// Emergency exit sign icon — running person through doorway.
/// Painted via CustomPainter for crisp rendering at any size.
class ExitIcon extends StatelessWidget {
  final double size;
  final Color color;

  const ExitIcon({
    super.key,
    this.size = 20,
    this.color = const Color(0xFFEF4444),
  });

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      size: Size(size, size),
      painter: _ExitIconPainter(color: color),
    );
  }
}

class _ExitIconPainter extends CustomPainter {
  final Color color;

  _ExitIconPainter({required this.color});

  @override
  void paint(Canvas canvas, Size size) {
    final s = size.width;
    final fill = Paint()
      ..color = color
      ..style = PaintingStyle.fill;

    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = s * 0.09
      ..strokeCap = StrokeCap.round;

    // Layout: [person 0.05-0.50] [arrow 0.50-0.65] [door 0.65-0.95]
    // Person runs RIGHT → arrow points RIGHT → door on RIGHT

    // ── Door frame (right side) ──
    final doorStroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = s * 0.07;

    // U-shaped door frame: open on the left side (person enters from left)
    canvas.drawLine(Offset(s * 0.70, s * 0.10), Offset(s * 0.70, s * 0.90), doorStroke); // left jamb
    canvas.drawLine(Offset(s * 0.70, s * 0.10), Offset(s * 0.95, s * 0.10), doorStroke); // lintel
    canvas.drawLine(Offset(s * 0.95, s * 0.10), Offset(s * 0.95, s * 0.90), doorStroke); // right jamb
    canvas.drawLine(Offset(s * 0.70, s * 0.90), Offset(s * 0.95, s * 0.90), doorStroke); // threshold

    // ── Arrow pointing right into the door ──
    final arrow = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = s * 0.07
      ..strokeCap = StrokeCap.round;

    canvas.drawLine(Offset(s * 0.52, s * 0.50), Offset(s * 0.72, s * 0.50), arrow);
    canvas.drawLine(Offset(s * 0.64, s * 0.38), Offset(s * 0.72, s * 0.50), arrow);
    canvas.drawLine(Offset(s * 0.64, s * 0.62), Offset(s * 0.72, s * 0.50), arrow);

    // ── Running person (facing RIGHT, strong forward lean) ──
    // Head — positioned forward (right) to show momentum
    canvas.drawCircle(Offset(s * 0.38, s * 0.18), s * 0.09, fill);

    // Torso — strong diagonal lean toward door (upper-left to lower-right)
    canvas.drawLine(Offset(s * 0.35, s * 0.27), Offset(s * 0.28, s * 0.55), stroke);

    // Arms — front arm reaches RIGHT toward door, back arm trails LEFT
    canvas.drawLine(Offset(s * 0.33, s * 0.36), Offset(s * 0.48, s * 0.32), stroke); // front arm → door
    canvas.drawLine(Offset(s * 0.33, s * 0.36), Offset(s * 0.18, s * 0.42), stroke); // back arm ← trailing

    // Legs — front leg extends RIGHT, back leg pushes off LEFT
    canvas.drawLine(Offset(s * 0.30, s * 0.55), Offset(s * 0.44, s * 0.78), stroke); // front leg → door
    canvas.drawLine(Offset(s * 0.30, s * 0.55), Offset(s * 0.14, s * 0.75), stroke); // back leg ← push
  }

  @override
  bool shouldRepaint(covariant _ExitIconPainter oldDelegate) {
    return oldDelegate.color != color;
  }
}
