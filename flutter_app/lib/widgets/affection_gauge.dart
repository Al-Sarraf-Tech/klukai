import 'package:flutter/material.dart';
import '../main.dart';

class AffectionGauge extends StatefulWidget {
  final int score;
  final int level;
  final String levelName;
  final int? lastDelta;

  const AffectionGauge({
    super.key,
    required this.score,
    required this.level,
    required this.levelName,
    this.lastDelta,
  });

  @override
  State<AffectionGauge> createState() => _AffectionGaugeState();
}

class _AffectionGaugeState extends State<AffectionGauge>
    with SingleTickerProviderStateMixin {
  late AnimationController _deltaController;
  int? _showDelta;

  @override
  void initState() {
    super.initState();
    _deltaController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    );
  }

  @override
  void didUpdateWidget(AffectionGauge oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.lastDelta != null &&
        widget.lastDelta != 0 &&
        widget.lastDelta != oldWidget.lastDelta) {
      _showDelta = widget.lastDelta;
      _deltaController.forward(from: 0.0).then((_) {
        if (mounted) setState(() => _showDelta = null);
      });
    }
  }

  @override
  void dispose() {
    _deltaController.dispose();
    super.dispose();
  }

  double get _fillFraction => (widget.score / 1000.0).clamp(0.0, 1.0);

  Color get _barColor {
    return switch (widget.level) {
      0 => GFL2Colors.textDim,       // Grey — cold
      1 => GFL2Colors.textDim,       // Grey — acknowledged
      2 => GFL2Colors.border,        // Blue-grey — professional
      3 => GFL2Colors.border,        // Blue-grey — guarded interest
      4 => GFL2Colors.primary,       // Cyan — trusted ally
      5 => GFL2Colors.primary,       // Cyan — unguarded
      6 => GFL2Colors.accent,        // Orange — deep devotion
      7 => GFL2Colors.accent,        // Orange — vulnerable
      8 => GFL2Colors.affinity,      // Pink — bonded
      9 => GFL2Colors.affinity,      // Pink — oath fulfilled
      _ => GFL2Colors.textDim,
    };
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        // Label row: TRUST ——————
        Row(
          children: [
            Text(
              'TRUST',
              style: TextStyle(
                color: GFL2Colors.textDim.withValues(alpha: 0.5),
                fontSize: 9,
                fontWeight: FontWeight.w700,
                letterSpacing: 1.5,
                fontFamily: 'monospace',
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Container(
                height: 1,
                color: GFL2Colors.border.withValues(alpha: 0.3),
              ),
            ),
          ],
        ),
        const SizedBox(height: 6),
        // Level name + score + delta
        Row(
          children: [
            Text(
              widget.levelName.toUpperCase(),
              style: TextStyle(
                color: _barColor,
                fontSize: 10,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.8,
                fontFamily: 'monospace',
              ),
            ),
            if (_showDelta != null) ...[
              const SizedBox(width: 6),
              AnimatedBuilder(
                animation: _deltaController,
                builder: (context, child) {
                  return Opacity(
                    opacity: (1.0 - _deltaController.value).clamp(0.0, 1.0),
                    child: Transform.translate(
                      offset: Offset(0, -6 * _deltaController.value),
                      child: Text(
                        _showDelta! > 0 ? '+$_showDelta' : '$_showDelta',
                        style: TextStyle(
                          color: _showDelta! > 0
                              ? GFL2Colors.success
                              : GFL2Colors.danger,
                          fontSize: 9,
                          fontWeight: FontWeight.w700,
                          fontFamily: 'monospace',
                        ),
                      ),
                    ),
                  );
                },
              ),
            ],
            const Spacer(),
            Text(
              '${widget.score}/1000',
              style: TextStyle(
                color: GFL2Colors.textDim.withValues(alpha: 0.6),
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ),
        const SizedBox(height: 4),
        // Progress bar with level segments
        SizedBox(
          height: 4,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(1),
            child: Stack(
              children: [
                // Background with segment dividers
                Container(color: GFL2Colors.border.withValues(alpha: 0.2)),
                // Level dividers at tier thresholds (30, 80, 150, 250, 380, 530, 680, 830, 950) / 1000
                for (final pct in [0.03, 0.08, 0.15, 0.25, 0.38, 0.53, 0.68, 0.83, 0.95])
                  Positioned(
                    left: pct * MediaQuery.of(context).size.width * 0.85,
                    child: Container(
                      width: 1,
                      height: 4,
                      color: GFL2Colors.border.withValues(alpha: 0.4),
                    ),
                  ),
                // Fill
                FractionallySizedBox(
                  alignment: Alignment.centerLeft,
                  widthFactor: _fillFraction,
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 500),
                    decoration: BoxDecoration(
                      color: _barColor,
                      borderRadius: BorderRadius.circular(1),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
