import 'package:flutter/material.dart';
import '../main.dart';

class TimelineScreen extends StatelessWidget {
  final Map<String, String> milestones;

  const TimelineScreen({super.key, required this.milestones});

  @override
  Widget build(BuildContext context) {
    final entries = milestones.entries.toList()
      ..sort((a, b) => a.value.compareTo(b.value));

    return Scaffold(
      backgroundColor: GFL2Colors.background,
      appBar: AppBar(
        backgroundColor: GFL2Colors.surface,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: GFL2Colors.textPrimary),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text('RELATIONSHIP TIMELINE',
            style: TextStyle(color: GFL2Colors.primary, fontSize: 13,
                fontWeight: FontWeight.w700, letterSpacing: 1.5, fontFamily: 'monospace')),
        centerTitle: true,
      ),
      body: entries.isEmpty
          ? Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.timeline, size: 48, color: GFL2Colors.textDim.withValues(alpha: 0.2)),
                  const SizedBox(height: 12),
                  Text('No milestones recorded yet.',
                      style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.4), fontSize: 13,
                          fontStyle: FontStyle.italic)),
                  const SizedBox(height: 4),
                  Text('Keep talking to Klukai.',
                      style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.3), fontSize: 11)),
                ],
              ),
            )
          : ListView.builder(
              padding: const EdgeInsets.all(16),
              itemCount: entries.length,
              itemBuilder: (context, index) {
                final entry = entries[index];
                return _buildTimelineEntry(entry.key, entry.value, index == 0, index == entries.length - 1);
              },
            ),
    );
  }

  Widget _buildTimelineEntry(String key, String dateStr, bool isFirst, bool isLast) {
    final label = _formatMilestoneKey(key);
    final date = _formatDate(dateStr);

    return IntrinsicHeight(
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Timeline line + dot
          SizedBox(
            width: 30,
            child: Column(
              children: [
                if (!isFirst)
                  Container(width: 1, height: 12, color: GFL2Colors.primary.withValues(alpha: 0.3)),
                Container(
                  width: 10, height: 10,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle, color: GFL2Colors.primary,
                    boxShadow: [BoxShadow(color: GFL2Colors.primary.withValues(alpha: 0.4), blurRadius: 6)],
                  ),
                ),
                if (!isLast)
                  Expanded(child: Container(width: 1, color: GFL2Colors.primary.withValues(alpha: 0.3))),
              ],
            ),
          ),
          const SizedBox(width: 12),
          // Content
          Expanded(
            child: Container(
              margin: const EdgeInsets.only(bottom: 16),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
                border: Border(left: BorderSide(color: GFL2Colors.primary.withValues(alpha: 0.3), width: 2)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(label, style: const TextStyle(color: GFL2Colors.textPrimary, fontSize: 12,
                      fontWeight: FontWeight.w700, fontFamily: 'monospace')),
                  const SizedBox(height: 4),
                  Text(date, style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.5),
                      fontSize: 10, fontFamily: 'monospace')),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  String _formatMilestoneKey(String key) {
    return switch (key) {
      'affection_level_1' => 'TRUST EARNED: Professional Respect',
      'affection_level_2' => 'TRUSTED ALLY: Gift-giving began',
      'affection_level_3' => 'DEEP DEVOTION: The mask slipped',
      'affection_level_4' => 'BONDED: "I am all you need"',
      'first_conversation' => 'FIRST CONTACT: Neural link established',
      'first_compliment' => 'FIRST PRAISE: Commander showed appreciation',
      'first_personal_share' => 'PERSONAL INTEL: Commander shared something private',
      _ => key.replaceAll('_', ' ').toUpperCase(),
    };
  }

  String _formatDate(String dateStr) {
    try {
      final dt = DateTime.parse(dateStr);
      return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')} '
          '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    } catch (_) {
      return dateStr;
    }
  }
}
