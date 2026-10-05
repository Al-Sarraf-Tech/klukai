import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../theme/gfl2_colors.dart';
import 'affection_gauge.dart';
import 'exit_icon.dart';
import 'heartbeat_sensor.dart';
import 'her_day_widgets.dart';
import 'mood_indicator.dart';

/// The chat header: portrait, name, Her Day line, link status, actions,
/// heartbeat and the affection gauge.
///
/// Pulled out of ChatScreen (which is browser-only) so its layout can be
/// widget-tested on the VM at real phone widths. Below [compactBelow] logical
/// pixels of name-column width, the less-used actions (ambient audio, Her POV,
/// subscription) fold into an overflow menu so the row never overflows on a
/// 320-375px phone.
class ChatHeader extends StatelessWidget {
  const ChatHeader({
    super.key,
    required this.connected,
    required this.mood,
    required this.glow,
    required this.bpm,
    required this.heartbeatColor,
    required this.ambientMuted,
    required this.affectionScore,
    required this.affectionLevel,
    required this.affectionLevelName,
    this.lastAffectionDelta,
    this.herDay,
    this.onOpenProfile,
    this.onHerDayTap,
    this.onToggleAmbient,
    this.onCompanion,
    this.onHerPov,
    this.onArchive,
    this.onSubscription,
    this.onLogout,
  });

  final bool connected;
  final String mood;
  final Color glow;
  final int bpm;
  final Color heartbeatColor;
  final bool ambientMuted;
  final int affectionScore;
  final int affectionLevel;
  final String affectionLevelName;
  final int? lastAffectionDelta;
  final HerDay? herDay;
  final VoidCallback? onOpenProfile;
  final VoidCallback? onHerDayTap;
  final VoidCallback? onToggleAmbient;
  final VoidCallback? onCompanion;
  final VoidCallback? onHerPov;
  final VoidCallback? onArchive;
  final VoidCallback? onSubscription;
  final VoidCallback? onLogout;

  /// Name-column width under which secondary actions move into the menu.
  static const double compactBelow = 290;

  static const _iconBox = BoxConstraints(minWidth: 28, minHeight: 28);

  @override
  Widget build(BuildContext context) {
    final linkColor = connected ? GFL2Colors.success : GFL2Colors.danger;
    return Container(
      key: const Key('chat-header'),
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        border: Border(bottom: BorderSide(color: GFL2Colors.border.withValues(alpha: 0.4))),
      ),
      child: Column(
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              GestureDetector(
                key: const Key('chat-header-portrait'),
                onTap: onOpenProfile,
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 600),
                  curve: Curves.easeInOut,
                  width: 52,
                  height: 52,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(4),
                    border: Border.all(color: glow.withValues(alpha: 0.6), width: 1.5),
                    boxShadow: [
                      BoxShadow(color: glow.withValues(alpha: 0.2), blurRadius: 12, spreadRadius: 2),
                    ],
                  ),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(3),
                    child: Image.asset(
                      'assets/klukai_portrait.png',
                      fit: BoxFit.cover,
                      errorBuilder: (_, e, s) => Container(
                        color: GFL2Colors.panel,
                        child: const Center(
                          child: Text('K',
                              style: TextStyle(
                                  color: GFL2Colors.primary, fontSize: 22, fontWeight: FontWeight.w700)),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: LayoutBuilder(
                  builder: (context, constraints) {
                    final compact = constraints.maxWidth < compactBelow;
                    return Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'KLUKAI',
                          style: TextStyle(
                            color: GFL2Colors.textPrimary,
                            fontSize: 16,
                            fontWeight: FontWeight.w800,
                            letterSpacing: 2.0,
                          ),
                        ),
                        Container(
                          width: 40,
                          height: 2,
                          margin: const EdgeInsets.only(top: 2),
                          color: GFL2Colors.accent,
                        ),
                        const SizedBox(height: 4),
                        const Text(
                          'SST-05  //  H.I.D.E. 404',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: GFL2Colors.textMuted,
                            fontSize: 10,
                            letterSpacing: 0.8,
                            fontFamily: 'monospace',
                          ),
                        ),
                        if (herDay != null) ...[
                          const SizedBox(height: 3),
                          HerDayStatusLine(day: herDay!, onTap: onHerDayTap),
                        ],
                        const SizedBox(height: 6),
                        Row(
                          children: [
                            Container(
                              width: 8,
                              height: 8,
                              decoration: BoxDecoration(
                                shape: BoxShape.circle,
                                color: linkColor,
                                boxShadow: [
                                  BoxShadow(
                                    color: linkColor.withValues(alpha: 0.6),
                                    blurRadius: 6,
                                    spreadRadius: 1,
                                  ),
                                ],
                              ),
                            ),
                            const SizedBox(width: 6),
                            Expanded(
                              child: Text(
                                connected ? 'LINK ACTIVE' : 'LINK DOWN',
                                key: const Key('chat-header-link'),
                                maxLines: 1,
                                softWrap: false,
                                overflow: TextOverflow.fade,
                                style: TextStyle(
                                  color: linkColor.withValues(alpha: 0.9),
                                  fontSize: 10,
                                  fontWeight: FontWeight.w700,
                                  letterSpacing: 1.0,
                                  fontFamily: 'monospace',
                                ),
                              ),
                            ),
                            const SizedBox(width: 4),
                            ..._actions(compact),
                          ],
                        ),
                        const SizedBox(height: 4),
                        // Mood on the left, heartbeat on the right. The mood
                        // chip scales down rather than overflow on a 320px phone.
                        Row(
                          children: [
                            Flexible(
                              child: FittedBox(
                                fit: BoxFit.scaleDown,
                                alignment: Alignment.centerLeft,
                                child: MoodIndicator(mood: mood),
                              ),
                            ),
                            const SizedBox(width: 8),
                            HeartbeatSensor(bpm: bpm, color: heartbeatColor),
                          ],
                        ),
                      ],
                    );
                  },
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          AffectionGauge(
            score: affectionScore,
            level: affectionLevel,
            levelName: affectionLevelName,
            lastDelta: lastAffectionDelta,
          ),
        ],
      ),
    );
  }

  List<Widget> _actions(bool compact) {
    final mute = _button(
      key: 'chat-header-ambient',
      icon: ambientMuted ? Icons.music_off : Icons.music_note,
      color: ambientMuted ? GFL2Colors.primary.withValues(alpha: 0.4) : glow.withValues(alpha: 0.8),
      tooltip: ambientMuted ? 'Enable ambient audio' : 'Mute ambient audio',
      onPressed: onToggleAmbient,
    );
    final companion = _button(
      key: 'chat-header-companion',
      icon: Icons.open_in_new,
      color: GFL2Colors.primary.withValues(alpha: 0.85),
      tooltip: 'Companion',
      onPressed: onCompanion,
    );
    final herPov = _button(
      key: 'chat-header-her-pov',
      icon: Icons.auto_awesome,
      color: GFL2Colors.affinity.withValues(alpha: 0.85),
      tooltip: 'Her POV',
      onPressed: onHerPov,
    );
    final archive = _button(
      key: 'chat-header-archive',
      icon: Icons.photo_library_outlined,
      color: GFL2Colors.primary.withValues(alpha: 0.7),
      tooltip: 'Memory Archive',
      onPressed: onArchive,
    );
    final subscription = _button(
      key: 'chat-header-subscription',
      icon: Icons.workspace_premium_outlined,
      color: GFL2Colors.primary.withValues(alpha: 0.7),
      tooltip: 'Subscription',
      onPressed: onSubscription,
    );
    final exit = GestureDetector(
      key: const Key('chat-header-exit'),
      onTap: onLogout,
      child: Tooltip(
        message: 'Disconnect',
        child: ExitIcon(size: 18, color: GFL2Colors.danger.withValues(alpha: 0.85)),
      ),
    );
    if (!compact) return [mute, companion, herPov, archive, subscription, exit];
    return [companion, archive, _overflowMenu(), exit];
  }

  Widget _overflowMenu() {
    return PopupMenuButton<String>(
      key: const Key('chat-header-more'),
      tooltip: 'More',
      padding: EdgeInsets.zero,
      constraints: const BoxConstraints(minWidth: 180),
      color: GFL2Colors.panel,
      icon: Icon(Icons.more_vert, size: 16, color: GFL2Colors.primary.withValues(alpha: 0.7)),
      iconSize: 16,
      style: IconButton.styleFrom(minimumSize: const Size(28, 28), padding: EdgeInsets.zero),
      onSelected: (v) => switch (v) {
        'ambient' => onToggleAmbient?.call(),
        'her_pov' => onHerPov?.call(),
        'subscription' => onSubscription?.call(),
        _ => null,
      },
      itemBuilder: (_) => [
        _menuItem('ambient', ambientMuted ? Icons.music_note : Icons.music_off,
            ambientMuted ? 'Ambient audio on' : 'Ambient audio off'),
        _menuItem('her_pov', Icons.auto_awesome, 'Her POV'),
        _menuItem('subscription', Icons.workspace_premium_outlined, 'Subscription'),
      ],
    );
  }

  PopupMenuItem<String> _menuItem(String value, IconData icon, String label) => PopupMenuItem(
        value: value,
        child: Row(
          children: [
            Icon(icon, size: 16, color: GFL2Colors.primary),
            const SizedBox(width: 10),
            Text(label, style: const TextStyle(color: GFL2Colors.textPrimary, fontSize: 13)),
          ],
        ),
      );

  Widget _button({
    required String key,
    required IconData icon,
    required Color color,
    required String tooltip,
    VoidCallback? onPressed,
  }) {
    return IconButton(
      key: Key(key),
      onPressed: onPressed,
      icon: Icon(icon, color: color, size: 16),
      padding: EdgeInsets.zero,
      constraints: _iconBox,
      tooltip: tooltip,
    );
  }
}
