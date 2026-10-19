import 'package:flutter/material.dart';
import '../theme/hud.dart';

class DateDivider extends StatelessWidget {
  final DateTime date;

  const DateDivider({super.key, required this.date});

  String _label() {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final d = DateTime(date.year, date.month, date.day);
    final diff = today.difference(d).inDays;
    if (diff == 0) return 'Today';
    if (diff == 1) return 'Yesterday';
    const months = [
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ];
    return '${months[date.month - 1]} ${date.day}';
  }

  @override
  Widget build(BuildContext context) {
    final rule = Expanded(
      child: Container(height: 1, color: HudColors.cyanA(0.18)),
    );
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      child: Center(
        child: Row(
          children: [
            rule,
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Text(
                _label(),
                style: HudText.label(size: 10, color: HudColors.textMuted),
              ),
            ),
            rule,
          ],
        ),
      ),
    );
  }
}
