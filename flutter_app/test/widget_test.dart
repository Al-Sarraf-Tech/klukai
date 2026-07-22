@TestOn('browser')
library;

// App-level smoke tests.
//
// The previous template test referenced a non-existent `MyApp`/counter and no
// longer compiled. The real app root is `KlukaiApp`, whose home is `ChatScreen`
// — and ChatScreen.initState() immediately opens a WebSocket and schedules
// self-reconnecting Timers, so pumping the full app cannot `pumpAndSettle` and
// would require a live backend. We therefore assert the stable, backend-free
// contract instead: the app constructs and the GFL2 design palette is intact.
//
// Runs under chrome (KlukaiApp transitively imports package:web):
//   flutter test --platform chrome test/widget_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/main.dart';

void main() {
  group('KlukaiApp', () {
    test('constructs without throwing', () {
      expect(() => const KlukaiApp(), returnsNormally);
    });
  });

  group('GFL2Colors palette', () {
    test('exposes fully-opaque core brand colors', () {
      // All palette entries should be opaque (alpha == 0xFF).
      for (final c in <Color>[
        GFL2Colors.background,
        GFL2Colors.surface,
        GFL2Colors.panel,
        GFL2Colors.border,
        GFL2Colors.primary,
        GFL2Colors.accent,
        GFL2Colors.affinity,
        GFL2Colors.success,
        GFL2Colors.danger,
        GFL2Colors.textPrimary,
        GFL2Colors.textDim,
      ]) {
        expect(
          c.toARGB32() >> 24 & 0xFF,
          0xFF,
          reason: 'palette colors must be opaque',
        );
      }
    });

    test('primary is the expected cyan and accent the expected orange', () {
      expect(GFL2Colors.primary, const Color(0xFF4FC3F7));
      expect(GFL2Colors.accent, const Color(0xFFE8923E));
      expect(GFL2Colors.affinity, const Color(0xFFE88CA5));
    });

    test('semantic success/danger are distinct', () {
      expect(GFL2Colors.success, isNot(equals(GFL2Colors.danger)));
    });
  });
}
