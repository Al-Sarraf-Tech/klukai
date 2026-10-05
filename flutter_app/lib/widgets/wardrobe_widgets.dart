import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../theme/gfl2_colors.dart';

const _mono = 'monospace';

/// TODAY card: the outfit she has on, its blurb, and why.
class TodayOutfitCard extends StatelessWidget {
  final OutfitInfo outfit;

  const TodayOutfitCard({super.key, required this.outfit});

  @override
  Widget build(BuildContext context) {
    final line = outfit.sourceLine;
    final accent = outfit.requestedByCommander ? GFL2Colors.affinity : GFL2Colors.primary;
    return Container(
      key: const Key('today-outfit-card'),
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.07),
        borderRadius: BorderRadius.circular(4),
        border: Border(left: BorderSide(color: accent, width: 2)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('TODAY',
              style: TextStyle(
                  color: accent.withValues(alpha: 0.8),
                  fontSize: 9,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 1.5,
                  fontFamily: _mono)),
          const SizedBox(height: 4),
          Text(outfit.name.toUpperCase(),
              style: const TextStyle(
                  color: GFL2Colors.textPrimary,
                  fontSize: 13,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 1,
                  fontFamily: _mono)),
          if (outfit.blurb.isNotEmpty) ...[
            const SizedBox(height: 3),
            Text(outfit.blurb,
                style: TextStyle(
                    color: GFL2Colors.textPrimary.withValues(alpha: 0.7), fontSize: 11, height: 1.4)),
          ],
          if (line.isNotEmpty) ...[
            const SizedBox(height: 5),
            Text(
              line,
              key: const Key('today-reason'),
              style: TextStyle(
                  color: accent.withValues(alpha: 0.9),
                  fontSize: 11,
                  fontStyle: FontStyle.italic),
            ),
          ],
        ],
      ),
    );
  }
}

/// The wardrobe, grouped CANON / EVERYDAY KIT. Locked items show a lock and
/// the level that opens them. Tapping an unlocked item calls [onSelect]; a
/// locked one calls [onLockedTap] (she answers without asking the server).
class WardrobeGrid extends StatelessWidget {
  final Wardrobe wardrobe;
  final ValueChanged<WardrobeItem>? onSelect;
  final ValueChanged<WardrobeItem>? onLockedTap;

  const WardrobeGrid({super.key, required this.wardrobe, this.onSelect, this.onLockedTap});

  @override
  Widget build(BuildContext context) {
    final todayId = wardrobe.today?.id;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (wardrobe.canon.isNotEmpty) ..._group('CANON', wardrobe.canon, todayId),
        if (wardrobe.everyday.isNotEmpty) ..._group('EVERYDAY KIT', wardrobe.everyday, todayId),
      ],
    );
  }

  List<Widget> _group(String title, List<WardrobeItem> items, String? todayId) => [
        Padding(
          padding: const EdgeInsets.only(top: 10, bottom: 6),
          child: Text(title,
              key: Key('wardrobe-group-$title'),
              style: TextStyle(
                  color: GFL2Colors.textDim.withValues(alpha: 0.7),
                  fontSize: 9,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.4,
                  fontFamily: _mono)),
        ),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [for (final item in items) _tile(item, item.current || item.id == todayId)],
        ),
      ];

  Widget _tile(WardrobeItem item, bool selected) {
    final unlocked = item.unlocked;
    return GestureDetector(
      key: Key('wardrobe-item-${item.id}'),
      onTap: unlocked
          ? (onSelect == null ? null : () => onSelect!(item))
          : (onLockedTap == null ? null : () => onLockedTap!(item)),
      child: Opacity(
        opacity: unlocked ? 1.0 : 0.45,
        child: Container(
          constraints: const BoxConstraints(maxWidth: 170),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            color: selected ? GFL2Colors.primary.withValues(alpha: 0.15) : GFL2Colors.background,
            borderRadius: BorderRadius.circular(4),
            border: Border.all(
              color: selected ? GFL2Colors.primary : GFL2Colors.border.withValues(alpha: 0.3),
              width: selected ? 1.5 : 1,
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (!unlocked)
                    Padding(
                      padding: const EdgeInsets.only(right: 4),
                      child: Icon(Icons.lock_outline,
                          key: Key('wardrobe-lock-${item.id}'),
                          size: 10,
                          color: GFL2Colors.textDim.withValues(alpha: 0.8)),
                    ),
                  Flexible(
                    child: Text(item.name,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: selected ? GFL2Colors.primary : GFL2Colors.textPrimary,
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                          fontFamily: _mono,
                        )),
                  ),
                  if (!unlocked)
                    Padding(
                      padding: const EdgeInsets.only(left: 6),
                      child: Text('LV ${item.unlockLevel}',
                          style: const TextStyle(
                              color: GFL2Colors.accent,
                              fontSize: 9,
                              fontWeight: FontWeight.w700,
                              fontFamily: _mono)),
                    ),
                ],
              ),
              if (item.blurb.isNotEmpty)
                Text(item.blurb,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.6), fontSize: 9)),
            ],
          ),
        ),
      ),
    );
  }
}

/// WORN THIS MONTH: one compact row per day, newest first.
class WornLogList extends StatelessWidget {
  final List<WardrobeLogEntry> entries;

  const WornLogList({super.key, required this.entries});

  @override
  Widget build(BuildContext context) {
    if (entries.isEmpty) {
      return Text('No record yet.',
          style: TextStyle(
              color: GFL2Colors.textDim.withValues(alpha: 0.6), fontSize: 11, fontFamily: _mono));
    }
    return Column(
      key: const Key('worn-log'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final e in entries)
          Padding(
            padding: const EdgeInsets.only(bottom: 3),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 52,
                  child: Text(e.shortDay,
                      style: TextStyle(
                          color: GFL2Colors.textDim.withValues(alpha: 0.7),
                          fontSize: 10,
                          fontFamily: _mono)),
                ),
                Expanded(
                  child: Text.rich(
                    TextSpan(children: [
                      TextSpan(
                          text: e.name,
                          style: const TextStyle(
                              color: GFL2Colors.textPrimary,
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                              fontFamily: _mono)),
                      if (e.requested)
                        const TextSpan(
                            text: '  YOUR REQUEST',
                            style: TextStyle(
                                color: GFL2Colors.affinity,
                                fontSize: 9,
                                fontWeight: FontWeight.w700,
                                fontFamily: _mono))
                      else if (e.reason.isNotEmpty)
                        TextSpan(
                            text: '  ${e.reason}',
                            style: TextStyle(
                                color: GFL2Colors.textDim.withValues(alpha: 0.7), fontSize: 10)),
                    ]),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }
}
