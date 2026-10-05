import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../theme/gfl2_colors.dart';

const _mono = 'monospace';

/// The one compact line under her name in the chat header:
/// `⌖ Hangar · tuning the suspension   [Speed Star]`.
///
/// Built to survive a 390px-wide phone: the label takes the remaining width
/// and ellipsizes, and the outfit chip has a hard maximum width.
class HerDayStatusLine extends StatelessWidget {
  final HerDay day;
  final VoidCallback? onTap;

  /// The chip never grows past this, so a long outfit name can't squeeze
  /// the label off the line.
  static const double chipMaxWidth = 110;

  const HerDayStatusLine({super.key, required this.day, this.onTap});

  IconData get _icon => switch (day.status?.override) {
        'gaming' => Icons.sports_esports_outlined,
        'mission' => Icons.gps_fixed,
        _ => Icons.place_outlined,
      };

  @override
  Widget build(BuildContext context) {
    final label = day.status?.label ?? '';
    final outfit = day.outfit;
    return GestureDetector(
      key: const Key('her-day-status'),
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Row(
        children: [
          Icon(_icon, size: 11, color: GFL2Colors.primary.withValues(alpha: 0.7)),
          const SizedBox(width: 4),
          Expanded(
            child: Text(
              label,
              key: const Key('her-day-label'),
              maxLines: 1,
              softWrap: false,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: GFL2Colors.textPrimary.withValues(alpha: 0.75),
                fontSize: 10,
                fontFamily: _mono,
                letterSpacing: 0.3,
              ),
            ),
          ),
          if (outfit != null) ...[
            const SizedBox(width: 6),
            OutfitChip(name: outfit.name, requested: outfit.requestedByCommander),
          ],
        ],
      ),
    );
  }
}

/// Small bordered outfit tag. Pink when he asked for it, cyan when she chose.
class OutfitChip extends StatelessWidget {
  final String name;
  final bool requested;

  const OutfitChip({super.key, required this.name, this.requested = false});

  @override
  Widget build(BuildContext context) {
    final color = requested ? GFL2Colors.affinity : GFL2Colors.primary;
    return ConstrainedBox(
      key: const Key('her-day-outfit-chip'),
      constraints: const BoxConstraints(maxWidth: HerDayStatusLine.chipMaxWidth),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(2),
          border: Border.all(color: color.withValues(alpha: 0.45), width: 0.8),
        ),
        child: Text(
          name,
          maxLines: 1,
          softWrap: false,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            color: color.withValues(alpha: 0.9),
            fontSize: 9,
            fontWeight: FontWeight.w700,
            fontFamily: _mono,
            letterSpacing: 0.4,
          ),
        ),
      ),
    );
  }
}

/// Her day as a strip of blocks, `0500–0800  Range · drills`, with the
/// current block highlighted.
class ScheduleStrip extends StatelessWidget {
  final List<ScheduleBlock> blocks;

  const ScheduleStrip({super.key, required this.blocks});

  @override
  Widget build(BuildContext context) {
    if (blocks.isEmpty) {
      return Text('No schedule filed.',
          style: TextStyle(
              color: GFL2Colors.textDim.withValues(alpha: 0.6), fontSize: 11, fontFamily: _mono));
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var i = 0; i < blocks.length; i++) _row(i, blocks[i]),
      ],
    );
  }

  Widget _row(int i, ScheduleBlock b) {
    final current = b.current;
    return Container(
      key: Key(current ? 'schedule-current' : 'schedule-block-$i'),
      margin: const EdgeInsets.only(bottom: 3),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
      decoration: BoxDecoration(
        color: current ? GFL2Colors.primary.withValues(alpha: 0.10) : Colors.transparent,
        border: Border(
          left: BorderSide(
            color: current ? GFL2Colors.primary : GFL2Colors.border.withValues(alpha: 0.4),
            width: current ? 2 : 1,
          ),
        ),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 82,
            child: Text(
              b.timeRange,
              style: TextStyle(
                color: current ? GFL2Colors.primary : GFL2Colors.textDim.withValues(alpha: 0.7),
                fontSize: 10,
                fontWeight: current ? FontWeight.w700 : FontWeight.w400,
                fontFamily: _mono,
              ),
            ),
          ),
          Expanded(
            child: Text(
              b.line,
              style: TextStyle(
                color: current
                    ? GFL2Colors.textPrimary
                    : GFL2Colors.textPrimary.withValues(alpha: 0.65),
                fontSize: 11,
                fontWeight: current ? FontWeight.w600 : FontWeight.w400,
              ),
            ),
          ),
          if (current)
            const Padding(
              padding: EdgeInsets.only(left: 6),
              child: Text('NOW',
                  style: TextStyle(
                      color: GFL2Colors.accent,
                      fontSize: 9,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 1,
                      fontFamily: _mono)),
            ),
        ],
      ),
    );
  }
}

/// Bottom sheet behind the header line: status, outfit, weather, schedule.
class HerDaySheet extends StatelessWidget {
  final HerDay day;
  final VoidCallback? onOpenDossier;

  const HerDaySheet({super.key, required this.day, this.onOpenDossier});

  @override
  Widget build(BuildContext context) {
    final outfit = day.outfit;
    final weather = day.weather;
    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              day.date.isEmpty ? 'TODAY' : 'TODAY // ${day.date}',
              style: TextStyle(
                  color: GFL2Colors.primary.withValues(alpha: 0.7),
                  fontSize: 10,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.5,
                  fontFamily: _mono),
            ),
            const SizedBox(height: 8),
            if ((day.status?.label ?? '').isNotEmpty)
              Text(day.status!.label,
                  style: const TextStyle(
                      color: GFL2Colors.textPrimary, fontSize: 13, fontWeight: FontWeight.w600)),
            if (outfit != null) ...[
              const SizedBox(height: 4),
              Text(
                outfit.sourceLine.isEmpty ? outfit.name : '${outfit.name} — ${outfit.sourceLine}',
                style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.9), fontSize: 11),
              ),
            ],
            if (weather != null && weather.label.isNotEmpty) ...[
              const SizedBox(height: 2),
              Text(weather.label,
                  style: TextStyle(
                      color: GFL2Colors.textDim.withValues(alpha: 0.7),
                      fontSize: 10,
                      fontFamily: _mono)),
            ],
            const SizedBox(height: 12),
            ScheduleStrip(blocks: day.schedule),
            if (onOpenDossier != null) ...[
              const SizedBox(height: 10),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  key: const Key('her-day-open-dossier'),
                  onPressed: onOpenDossier,
                  icon: const Icon(Icons.badge_outlined, size: 14, color: GFL2Colors.primary),
                  label: const Text('DOSSIER',
                      style: TextStyle(
                          color: GFL2Colors.primary,
                          fontSize: 11,
                          letterSpacing: 1.2,
                          fontFamily: _mono)),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
