import 'package:flutter/material.dart';
import 'screens/chat_screen.dart';

void main() {
  runApp(const KlukaiApp());
}

/// GFL2-inspired color constants.
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

class KlukaiApp extends StatelessWidget {
  const KlukaiApp({super.key});

  @override
  Widget build(BuildContext context) {
    final serverUrl = Uri.base.origin.contains('localhost')
        ? 'http://localhost:8300'
        : Uri.base.origin;

    return MaterialApp(
      title: 'Klukai',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: GFL2Colors.background,
        colorScheme: const ColorScheme.dark(
          primary: GFL2Colors.primary,
          secondary: GFL2Colors.accent,
          surface: GFL2Colors.surface,
          onSurface: GFL2Colors.textPrimary,
        ),
        fontFamily: 'Inter',
      ),
      home: ChatScreen(serverUrl: serverUrl),
    );
  }
}
