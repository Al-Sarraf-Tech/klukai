// HUD tab text colors meet WCAG AA (4.5:1) on every surface they sit on:
// the ground, a cyan panel tint, the violet EQUIPPED tint and the stripes of
// locked kit. VM-runnable.
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/theme/hud.dart';
import 'package:companion_app/widgets/hud_parts.dart';

import 'contrast_test.dart' show contrast, over;

void main() {
  final surfaces = {
    'ground': HudColors.ground,
    'cyan panel (5%)': over(HudColors.cyan, 0.05, HudColors.ground),
    'active block (9%)': over(HudColors.cyan, 0.09, HudColors.ground),
    'equipped (10% violet)': over(HudColors.violet, 0.10, HudColors.ground),
    'stripes (7%)': over(HudColors.cyan, 0.07, HudColors.ground),
    'violet stripes (7%)': over(HudColors.violet, 0.07, HudColors.ground),
  };
  const texts = {
    'text': HudColors.text,
    'textDim': HudColors.textDim,
    'kHudDimText': kHudDimText,
    'cyan': HudColors.cyan,
    'violet': HudColors.violet,
    'danger': kHudDanger,
  };

  for (final MapEntry(key: name, value: bg) in surfaces.entries) {
    test('HUD text colors on $name (>= 4.5:1)', () {
      for (final MapEntry(key: fgName, value: fg) in texts.entries) {
        expect(
          contrast(fg, bg),
          greaterThanOrEqualTo(4.5),
          reason: '$fgName on $name',
        );
      }
    });
  }

  test('filled chips and badges: ground on cyan / violet', () {
    expect(contrast(HudColors.ground, HudColors.cyan), greaterThan(4.5));
    expect(contrast(HudColors.ground, HudColors.violet), greaterThan(4.5));
  });

  test('why kHudDimText exists: textMuted falls short on a panel', () {
    expect(
      contrast(
        HudColors.textMuted,
        over(HudColors.cyan, 0.05, HudColors.ground),
      ),
      lessThan(4.5),
    );
  });
}
