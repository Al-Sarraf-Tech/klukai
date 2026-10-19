/// Tactical HUD design system (owner pick 2026-10-19): the H.I.D.E. 404 look.
///
/// Gunmetal ground, a cyan primary and a violet secondary (her canon palette),
/// cut-corner panels, Rajdhani for display and JetBrains Mono for labels.
/// Every screen of the new UI builds on these tokens and widgets.
library;

import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

class HudColors {
  HudColors._();
  static const ground = Color(0xFF06080C);
  static const cyan = Color(0xFF4FD1FF);
  static const violet = Color(0xFF9B7BFF);
  static const text = Color(0xFFD8F3FF);
  static const textDim = Color(0xFF8FB7C9);
  static const textMuted = Color(0xFF5E7F90);
  static const pipOff = Color(0xFF1F2A38);
  static const danger = Color(0xFFFF8FA3);
  static const online = Color(0xFF7EE0A6);
  static Color cyanA(double a) => cyan.withValues(alpha: a);
  static Color violetA(double a) => violet.withValues(alpha: a);
}

class HudText {
  HudText._();
  static TextStyle display({double size = 30, Color color = HudColors.text}) =>
      GoogleFonts.rajdhani(
        fontSize: size,
        fontWeight: FontWeight.w700,
        letterSpacing: size * 0.08,
        height: 1,
        color: color,
      );
  static TextStyle body({
    double size = 15,
    FontWeight weight = FontWeight.w500,
    Color color = HudColors.text,
  }) => GoogleFonts.rajdhani(
    fontSize: size,
    fontWeight: weight,
    height: 1.3,
    color: color,
  );
  static TextStyle label({
    double size = 10,
    Color color = HudColors.textDim,
    FontWeight weight = FontWeight.w400,
  }) => GoogleFonts.jetBrainsMono(
    fontSize: size,
    letterSpacing: size * 0.14,
    fontWeight: weight,
    color: color,
  );
}

/// A rectangle with two opposite corners cut at 45 degrees (top-left and
/// bottom-right by default), the HUD's panel silhouette.
class CutCornerBorder extends OutlinedBorder {
  const CutCornerBorder({
    this.cut = 10,
    this.cutTopLeft = true,
    this.cutTopRight = false,
    this.cutBottomRight = true,
    this.cutBottomLeft = false,
    super.side,
  });

  final double cut;
  final bool cutTopLeft, cutTopRight, cutBottomRight, cutBottomLeft;

  Path _path(Rect r) {
    final c = cut.clamp(0, r.shortestSide / 2).toDouble();
    return Path()
      ..moveTo(r.left + (cutTopLeft ? c : 0), r.top)
      ..lineTo(r.right - (cutTopRight ? c : 0), r.top)
      ..lineTo(r.right, r.top + (cutTopRight ? c : 0))
      ..lineTo(r.right, r.bottom - (cutBottomRight ? c : 0))
      ..lineTo(r.right - (cutBottomRight ? c : 0), r.bottom)
      ..lineTo(r.left + (cutBottomLeft ? c : 0), r.bottom)
      ..lineTo(r.left, r.bottom - (cutBottomLeft ? c : 0))
      ..lineTo(r.left, r.top + (cutTopLeft ? c : 0))
      ..close();
  }

  @override
  Path getOuterPath(Rect rect, {TextDirection? textDirection}) => _path(rect);

  @override
  Path getInnerPath(Rect rect, {TextDirection? textDirection}) =>
      _path(rect.deflate(side.strokeInset));

  @override
  void paint(Canvas canvas, Rect rect, {TextDirection? textDirection}) {
    if (side.style == BorderStyle.none) return;
    canvas.drawPath(_path(rect.deflate(side.width / 2)), side.toPaint());
  }

  @override
  CutCornerBorder copyWith({BorderSide? side}) => CutCornerBorder(
    cut: cut,
    cutTopLeft: cutTopLeft,
    cutTopRight: cutTopRight,
    cutBottomRight: cutBottomRight,
    cutBottomLeft: cutBottomLeft,
    side: side ?? this.side,
  );

  @override
  ShapeBorder scale(double t) => CutCornerBorder(
    cut: cut * t,
    cutTopLeft: cutTopLeft,
    cutTopRight: cutTopRight,
    cutBottomRight: cutBottomRight,
    cutBottomLeft: cutBottomLeft,
    side: side.scale(t),
  );
}

/// The faint cyan grid and scanlines behind every HUD screen.
class HudBackground extends StatelessWidget {
  const HudBackground({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => ColoredBox(
    color: HudColors.ground,
    child: CustomPaint(
      painter: const _GridPainter(),
      foregroundPainter: const _ScanPainter(),
      child: child,
    ),
  );
}

class _GridPainter extends CustomPainter {
  const _GridPainter();
  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = HudColors.cyanA(0.06)
      ..strokeWidth = 1;
    for (double x = 0; x < size.width; x += 26) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), p);
    }
    for (double y = 0; y < size.height; y += 26) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), p);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

class _ScanPainter extends CustomPainter {
  const _ScanPainter();
  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()..color = Colors.white.withValues(alpha: 0.025);
    for (double y = 0; y < size.height; y += 3) {
      canvas.drawRect(Rect.fromLTWH(0, y, size.width, 1), p);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

/// A cut-corner panel with a thin cyan (or violet) edge.
class HudPanel extends StatelessWidget {
  const HudPanel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(12),
    this.accent = HudColors.cyan,
    this.fill,
    this.edgeAlpha = 0.35,
    this.cut = 10,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final Color accent;
  final Color? fill;
  final double edgeAlpha;
  final double cut;

  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: ShapeDecoration(
      color: fill ?? accent.withValues(alpha: 0.05),
      shape: CutCornerBorder(
        cut: cut,
        side: BorderSide(color: accent.withValues(alpha: edgeAlpha)),
      ),
    ),
    child: Padding(padding: padding, child: child),
  );
}

/// Small spaced monospace label (section titles, readout keys).
class HudLabel extends StatelessWidget {
  const HudLabel(
    this.text, {
    super.key,
    this.color = HudColors.textDim,
    this.size = 10,
  });
  final String text;
  final Color color;
  final double size;
  @override
  Widget build(BuildContext context) => Text(
    text.toUpperCase(),
    style: HudText.label(size: size, color: color),
  );
}

/// A filter chip: a real button, cyan-filled when selected.
class HudChip extends StatelessWidget {
  const HudChip({
    super.key,
    required this.label,
    required this.selected,
    this.onTap,
  });
  final String label;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Semantics(
    button: true,
    selected: selected,
    child: InkWell(
      onTap: onTap,
      child: Container(
        constraints: const BoxConstraints(minHeight: 36),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        decoration: BoxDecoration(
          color: selected ? HudColors.cyan : Colors.transparent,
          border: Border.all(
            color: selected ? HudColors.cyan : HudColors.cyanA(0.35),
          ),
        ),
        child: Text(
          label.toUpperCase(),
          style: HudText.label(
            color: selected ? HudColors.ground : HudColors.textDim,
            weight: selected ? FontWeight.w600 : FontWeight.w400,
          ),
        ),
      ),
    ),
  );
}

/// A small outlined badge (LV 4, NEW YEAR, EQUIPPED…).
class HudBadge extends StatelessWidget {
  const HudBadge(
    this.text, {
    super.key,
    this.color = HudColors.cyan,
    this.filled = false,
  });
  final String text;
  final Color color;
  final bool filled;
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
    decoration: BoxDecoration(
      color: filled ? color : Colors.transparent,
      border: Border.all(color: filled ? color : color.withValues(alpha: 0.6)),
    ),
    child: Text(
      text.toUpperCase(),
      style: HudText.label(
        size: 9.5,
        color: filled ? HudColors.ground : color,
        weight: filled ? FontWeight.w600 : FontWeight.w400,
      ),
    ),
  );
}

/// A two-or-more option segmented switch (EN | JP, CHAT | 3D).
class HudSegmented<T> extends StatelessWidget {
  const HudSegmented({
    super.key,
    required this.options,
    required this.value,
    required this.onChanged,
    this.accent = HudColors.violet,
    this.height = 44,
    this.semanticLabel,
  });

  final Map<T, String> options;
  final T value;
  final ValueChanged<T>? onChanged;
  final Color accent;
  final double height;
  final String? semanticLabel;

  @override
  Widget build(BuildContext context) => Semantics(
    label: semanticLabel,
    child: Container(
      height: height,
      decoration: BoxDecoration(
        border: Border.all(color: accent.withValues(alpha: 0.6)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final entry in options.entries)
            Semantics(
              button: true,
              selected: entry.key == value,
              label: entry.value,
              child: InkWell(
                onTap: onChanged == null ? null : () => onChanged!(entry.key),
                child: Container(
                  constraints: const BoxConstraints(minWidth: 40),
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  alignment: Alignment.center,
                  color: entry.key == value ? accent : Colors.transparent,
                  child: Text(
                    entry.value,
                    style: HudText.label(
                      size: 11,
                      color: entry.key == value
                          ? HudColors.ground
                          : accent.withValues(alpha: 0.8),
                      weight: entry.key == value
                          ? FontWeight.w600
                          : FontWeight.w400,
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    ),
  );
}

/// The five destinations of the bottom tab bar.
enum HudTab { chat, album, day, kit, me }

extension HudTabInfo on HudTab {
  String get label => switch (this) {
    HudTab.chat => 'Chat',
    HudTab.album => 'Album',
    HudTab.day => 'Day',
    HudTab.kit => 'Kit',
    HudTab.me => 'Me',
  };
  IconData get icon => switch (this) {
    HudTab.chat => Icons.chat_bubble_outline,
    HudTab.album => Icons.photo_outlined,
    HudTab.day => Icons.schedule,
    HudTab.kit => Icons.checkroom_outlined,
    HudTab.me => Icons.person_outline,
  };
}

/// Bottom tab bar at thumb height, above the home indicator.
class HudTabBar extends StatelessWidget {
  const HudTabBar({super.key, required this.current, required this.onSelect});
  final HudTab current;
  final ValueChanged<HudTab> onSelect;

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.paddingOf(context).bottom;
    return Container(
      padding: EdgeInsets.only(top: 8, bottom: bottom > 0 ? bottom : 10),
      decoration: BoxDecoration(
        color: HudColors.ground.withValues(alpha: 0.96),
        border: Border(top: BorderSide(color: HudColors.cyanA(0.3))),
      ),
      child: Row(
        children: [
          for (final tab in HudTab.values)
            Expanded(
              child: Semantics(
                button: true,
                selected: tab == current,
                label: tab.label,
                child: InkWell(
                  key: ValueKey('hud-tab-${tab.name}'),
                  onTap: () => onSelect(tab),
                  child: SizedBox(
                    height: 52,
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(
                          tab.icon,
                          size: 22,
                          color: tab == current
                              ? HudColors.cyan
                              : HudColors.textMuted,
                          shadows: tab == current
                              ? [Shadow(color: HudColors.cyan, blurRadius: 8)]
                              : null,
                        ),
                        const SizedBox(height: 4),
                        ExcludeSemantics(
                          child: Text(
                            tab.label.toUpperCase(),
                            style: HudText.label(
                              size: 9.5,
                              color: tab == current
                                  ? HudColors.cyan
                                  : HudColors.textMuted,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
