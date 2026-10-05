import 'package:flutter/painting.dart';

/// GFL2-inspired color constants.
///
/// Lives in its own library (re-exported by main.dart) so widgets that only
/// need the palette don't drag in the app shell, and with it package:web.
/// That keeps them testable on the Dart VM as well as under chrome.
class GFL2Colors {
  static const background = Color(0xFF12151E);
  static const surface = Color(0xFF1A1F2E);
  static const panel = Color(0xFF252B3B);
  static const border = Color(0xFF3A4256);
  static const primary = Color(0xFF4FC3F7);     // Cyan-blue
  static const accent = Color(0xFFE8923E);      // Orange
  static const affinity = Color(0xFFE88CA5);    // Pink
  static const success = Color(0xFF4ADE80);     // Green
  static const danger = Color(0xFFEF4444);      // Red
  static const textPrimary = Color(0xFFD4DDE6); // Light silver
  static const textDim = Color(0xFF6B7D8D);     // Muted
}
