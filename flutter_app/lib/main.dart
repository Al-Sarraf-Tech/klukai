import 'package:flutter/material.dart';
import 'screens/chat_screen.dart';
import 'theme/gfl2_colors.dart';

// Re-exported so existing `import '../main.dart' show GFL2Colors;` keeps working.
export 'theme/gfl2_colors.dart';

void main() {
  runApp(const KlukaiApp());
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
