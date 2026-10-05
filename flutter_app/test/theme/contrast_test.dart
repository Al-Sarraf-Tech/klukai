// Dark-mode contrast: text colors meet WCAG AA on every surface. VM-runnable.
import 'dart:math' as math;

import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/theme/gfl2_colors.dart';

double _channel(double c) => c <= 0.03928 ? c / 12.92 : math.pow((c + 0.055) / 1.055, 2.4).toDouble();

double _luminance(Color c) => 0.2126 * _channel(c.r) + 0.7152 * _channel(c.g) + 0.0722 * _channel(c.b);

double contrast(Color a, Color b) {
  final la = _luminance(a), lb = _luminance(b);
  return (math.max(la, lb) + 0.05) / (math.min(la, lb) + 0.05);
}

/// [fg] at [alpha] over [bg], as the eye sees it.
Color over(Color fg, double alpha, Color bg) => Color.lerp(bg, fg, alpha)!;

void main() {
  const surfaces = {
    'background': GFL2Colors.background,
    'surface': GFL2Colors.surface,
    'panel': GFL2Colors.panel,
  };

  for (final MapEntry(key: name, value: bg) in surfaces.entries) {
    test('readable text colors on $name (>= 4.5:1)', () {
      for (final fg in [GFL2Colors.textPrimary, GFL2Colors.textMuted, GFL2Colors.primary,
          GFL2Colors.accent, GFL2Colors.affinity]) {
        expect(contrast(fg, bg), greaterThanOrEqualTo(4.5), reason: '$fg on $name');
      }
    });
  }

  test('why textMuted exists: dimmed textDim fails AA on surface', () {
    expect(contrast(over(GFL2Colors.textDim, 0.6, GFL2Colors.surface), GFL2Colors.surface),
        lessThan(3));
    expect(contrast(GFL2Colors.textMuted, GFL2Colors.surface), greaterThan(4.5));
  });
}
