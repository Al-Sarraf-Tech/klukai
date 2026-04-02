import 'package:flutter/material.dart';
import 'screens/chat_screen.dart';

void main() {
  runApp(const CompanionApp());
}

class CompanionApp extends StatelessWidget {
  const CompanionApp({super.key});

  @override
  Widget build(BuildContext context) {
    // When served by companion-core, connect to same origin.
    // Fallback to localhost for dev.
    final serverUrl = Uri.base.origin.contains('localhost')
        ? 'http://localhost:8300'
        : Uri.base.origin;

    return MaterialApp(
      title: 'Companion',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0D1117),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFF7C3AED),
          surface: Color(0xFF161B22),
        ),
        fontFamily: 'Inter',
      ),
      home: ChatScreen(serverUrl: serverUrl),
    );
  }
}
