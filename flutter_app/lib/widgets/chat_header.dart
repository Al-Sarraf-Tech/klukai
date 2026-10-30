import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../theme/hud.dart';
import 'mood_indicator.dart';

/// Which face of the Chat tab is showing: the transcript or her 3D stage.
enum ChatViewMode { chat, threeD }

/// What the chat menu holds: the entry points that lost their header buttons
/// when the tab bar took over Album, Day and Me.
enum ChatMenuAction { companion, threads, herPov, subscription, logout }

/// Highest affinity level; the pips count up to it.
const int kMaxAffinityLevel = 9;

/// Her mood as the HUD prints it (`quietly_pleased` → `PLEASED`).
String moodLabel(String mood) => MoodIndicator(mood: mood).statusText;

/// The Tactical HUD chat header: the top bar with the CHAT | 3D flip, her
/// portrait and readouts (link, affinity, mood, heart), and the WEARING / NOW
/// chips from Her Day.
///
/// Stateless and VM-testable at real phone widths. With [compact] (the
/// keyboard is up) it shrinks to the top bar and one readout line so the
/// transcript keeps its room.
class ChatHeader extends StatelessWidget {
  const ChatHeader({
    super.key,
    required this.connected,
    required this.mood,
    required this.bpm,
    required this.affectionLevel,
    required this.affectionLevelName,
    required this.view,
    required this.onViewChanged,
    this.glow = HudColors.cyan,
    this.spiking = false,
    this.affectionScore = 0,
    this.affectionKnown = true,
    this.lastAffectionDelta,
    this.herDay,
    this.onMenu,
    this.onOpenProfile,
    this.onHerDayTap,
    this.compact = false,
  });

  final bool connected;
  final String mood;
  final int bpm;
  final int affectionLevel;
  final String affectionLevelName;
  final ChatViewMode view;
  final ValueChanged<ChatViewMode> onViewChanged;

  /// Her mood colour, a soft glow around the portrait.
  final Color glow;

  /// A heartbeat surge is in progress: the ECG runs red.
  final bool spiking;
  final int affectionScore;

  /// False when the bond could not be read. The header must not print LV0
  /// as if that were her level.
  final bool affectionKnown;
  final int? lastAffectionDelta;
  final HerDay? herDay;
  final ValueChanged<ChatMenuAction>? onMenu;
  final VoidCallback? onOpenProfile;
  final VoidCallback? onHerDayTap;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    return Padding(
      key: const Key('chat-header'),
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 4),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          HudTopBar(
            title: 'H.I.D.E. 404 // SECURE LINK',
            view: view,
            onViewChanged: onViewChanged,
            onMenu: onMenu,
          ),
          if (compact) _compactLine() else _identity(),
          if (!compact && herDay != null) ...[
            const SizedBox(height: 10),
            HerDayChips(day: herDay!, onTap: onHerDayTap),
          ],
        ],
      ),
    );
  }

  Color get _ecgColor => spiking ? HudColors.danger : HudColors.cyan;

  Widget _linkState({double size = 11}) => Text(
    connected ? 'ONLINE' : 'LINK DOWN',
    key: const Key('chat-header-link'),
    maxLines: 1,
    softWrap: false,
    overflow: TextOverflow.fade,
    style: HudText.label(
      size: size,
      color: connected ? HudColors.violet : HudColors.danger,
      weight: FontWeight.w600,
    ),
  );

  Widget _identity() {
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final portraitWidth = (constraints.maxWidth * 0.28).clamp(
            76.0,
            104.0,
          );
          return Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              HudPortrait(
                width: portraitWidth,
                glow: glow,
                onTap: onOpenProfile,
              ),
              const SizedBox(width: 12),
              Expanded(child: _readouts()),
            ],
          );
        },
      ),
    );
  }

  Widget _readouts() {
    final level = affectionLevel.clamp(0, kMaxAffinityLevel);
    final delta = lastAffectionDelta;
    final deltaText = delta == null || delta == 0
        ? ''
        : ' · ${delta > 0 ? '+' : ''}$delta';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            Flexible(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Text('KLUKAI', style: HudText.display(size: 30)),
              ),
            ),
            const SizedBox(width: 8),
            Flexible(child: _linkState()),
          ],
        ),
        const SizedBox(height: 10),
        Tooltip(
          message: affectionKnown
              ? '$affectionLevelName · $affectionScore$deltaText'
              : 'Affinity unread',
          child: Semantics(
            label: affectionKnown
                ? 'Affinity level $level of $kMaxAffinityLevel, $affectionLevelName'
                : 'Affinity unread',
            excludeSemantics: true,
            child: _ReadoutRow(
              label: 'AFFINITY',
              middle: AffinityPips(level: affectionKnown ? level : 0),
              value: affectionKnown ? 'LV$level' : '—',
              valueKey: const Key('chat-header-level'),
            ),
          ),
        ),
        const SizedBox(height: 8),
        _ReadoutRow(
          label: 'MOOD',
          value: moodLabel(mood),
          valueKey: const Key('chat-header-mood'),
        ),
        const SizedBox(height: 6),
        Semantics(
          label: 'Heart rate $bpm beats per minute',
          excludeSemantics: true,
          child: _ReadoutRow(
            label: 'HEART',
            middle: SizedBox(
              height: 22,
              child: HudEcg(bpm: bpm, color: _ecgColor),
            ),
            value: '$bpm BPM',
            valueKey: const Key('chat-header-bpm'),
          ),
        ),
      ],
    );
  }

  Widget _compactLine() {
    final level = affectionLevel.clamp(0, kMaxAffinityLevel);
    final levelLabel = affectionKnown ? 'LV$level' : '—';
    return Padding(
      padding: const EdgeInsets.only(top: 4, bottom: 2),
      child: LayoutBuilder(
        builder: (context, box) {
          if (box.maxWidth <
              300 * MediaQuery.textScalerOf(context).scale(10) / 10) {
            return Wrap(
              spacing: 12,
              runSpacing: 6,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                Text('KLUKAI', style: HudText.display(size: 18)),
                _linkState(size: 10),
                Text(
                  levelLabel,
                  key: const Key('chat-header-level'),
                  style: HudText.label(
                    size: 10.5,
                    color: HudColors.text,
                    weight: FontWeight.w600,
                  ),
                ),
                SizedBox(
                  width: 60,
                  height: 18,
                  child: HudEcg(bpm: bpm, color: _ecgColor),
                ),
                Text(
                  '$bpm BPM',
                  key: const Key('chat-header-bpm'),
                  style: HudText.label(
                    size: 10.5,
                    color: HudColors.text,
                    weight: FontWeight.w600,
                  ),
                ),
              ],
            );
          }
          return Row(
            children: [
              Text('KLUKAI', style: HudText.display(size: 18)),
              const SizedBox(width: 8),
              Flexible(child: _linkState(size: 10)),
              const SizedBox(width: 8),
              Text(
                levelLabel,
                key: const Key('chat-header-level'),
                style: HudText.label(
                  size: 10.5,
                  color: HudColors.text,
                  weight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: SizedBox(
                  height: 18,
                  child: HudEcg(bpm: bpm, color: _ecgColor),
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '$bpm BPM',
                key: const Key('chat-header-bpm'),
                style: HudText.label(
                  size: 10.5,
                  color: HudColors.text,
                  weight: FontWeight.w600,
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// One `LABEL ……… VALUE` line of the identity readouts.
class _ReadoutRow extends StatelessWidget {
  const _ReadoutRow({
    required this.label,
    required this.value,
    this.middle,
    this.valueKey,
  });
  final String label;
  final String value;
  final Widget? middle;
  final Key? valueKey;

  @override
  Widget build(BuildContext context) {
    final text = Text(
      value,
      key: valueKey,
      maxLines: 1,
      softWrap: false,
      textAlign: TextAlign.right,
      overflow: TextOverflow.ellipsis,
      style: HudText.label(
        size: 10.5,
        color: HudColors.text,
        weight: FontWeight.w600,
      ),
    );
    return Row(
      children: [
        Text(label, style: HudText.label(size: 10.5)),
        const SizedBox(width: 8),
        if (middle != null) ...[
          Expanded(child: middle!),
          const SizedBox(width: 8),
          text,
        ] else
          Expanded(child: text),
      ],
    );
  }
}

/// Nine violet pips, [level] of them lit. Shrinks with the row on a narrow
/// phone rather than overflow.
class AffinityPips extends StatelessWidget {
  const AffinityPips({super.key, required this.level});
  final int level;

  @override
  Widget build(BuildContext context) => Align(
    alignment: Alignment.centerRight,
    child: ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: kMaxAffinityLevel * 11.0),
      child: Row(
        key: const Key('affinity-pips'),
        children: [
          for (var i = 0; i < kMaxAffinityLevel; i++)
            Expanded(
              child: Container(
                key: Key(i < level ? 'pip-on' : 'pip-off'),
                height: 6,
                margin: const EdgeInsets.only(right: 2),
                color: i < level ? HudColors.violet : HudColors.pipOff,
              ),
            ),
        ],
      ),
    ),
  );
}

/// Her portrait in the cut-corner cyan frame; tapping opens her dossier.
class HudPortrait extends StatelessWidget {
  const HudPortrait({
    super.key,
    this.width = 104,
    this.glow = HudColors.cyan,
    this.onTap,
  });
  final double width;
  final Color glow;
  final VoidCallback? onTap;

  static const _shape = CutCornerBorder(cut: 14);

  @override
  Widget build(BuildContext context) {
    final height = width * 124 / 104;
    return Semantics(
      button: onTap != null,
      label: 'Klukai. Open her dossier',
      child: GestureDetector(
        key: const Key('chat-header-portrait'),
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 600),
          width: width,
          height: height,
          decoration: ShapeDecoration(
            color: const Color(0xFF0D1B26),
            shape: _shape,
            shadows: [
              BoxShadow(color: glow.withValues(alpha: 0.22), blurRadius: 14),
            ],
          ),
          foregroundDecoration: const ShapeDecoration(
            shape: CutCornerBorder(
              cut: 14,
              side: BorderSide(color: HudColors.cyan),
            ),
          ),
          child: ClipPath(
            clipper: const ShapeBorderClipper(shape: _shape),
            child: Image.asset(
              'assets/klukai_portrait.png',
              fit: BoxFit.cover,
              alignment: const Alignment(0, -0.64),
              errorBuilder: (_, e, s) => Center(
                child: Text(
                  'K',
                  style: HudText.display(size: 34, color: HudColors.cyan),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// WEARING (today's outfit) and NOW (her current block of the day). Tapping
/// either opens the Her Day sheet.
class HerDayChips extends StatelessWidget {
  const HerDayChips({super.key, required this.day, this.onTap});
  final HerDay day;
  final VoidCallback? onTap;

  /// Where she is now: the status line, else the current schedule block.
  static String nowLine(HerDay day) {
    final label = day.status?.label ?? '';
    if (label.isNotEmpty) return label;
    for (final block in day.schedule) {
      if (block.current) return block.line;
    }
    return '';
  }

  @override
  Widget build(BuildContext context) {
    final outfit = day.outfit?.name ?? '';
    final now = nowLine(day);
    return Row(
      children: [
        Expanded(
          child: HudInfoChip(
            key: const Key('chat-chip-wearing'),
            label: 'WEARING',
            value: outfit.isEmpty ? '—' : outfit,
            onTap: onTap,
          ),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: HudInfoChip(
            key: const Key('chat-chip-now'),
            label: 'NOW',
            value: now.isEmpty ? '—' : now,
            onTap: onTap,
          ),
        ),
      ],
    );
  }
}

/// A small cut-corner tile: a spaced label over a one-line value.
class HudInfoChip extends StatelessWidget {
  const HudInfoChip({
    super.key,
    required this.label,
    required this.value,
    this.onTap,
  });
  final String label;
  final String value;
  final VoidCallback? onTap;

  static const _shape = CutCornerBorder(
    cut: 8,
    cutBottomRight: false,
    side: BorderSide(color: Color(0x594FD1FF)),
  );

  @override
  Widget build(BuildContext context) => Semantics(
    button: onTap != null,
    label: '$label: $value',
    excludeSemantics: true,
    child: Material(
      type: MaterialType.transparency,
      child: InkWell(
        onTap: onTap,
        customBorder: _shape,
        child: Ink(
          decoration: ShapeDecoration(
            color: HudColors.cyanA(0.05),
            shape: _shape,
          ),
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 44),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(8, 6, 8, 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(label, style: HudText.label(size: 10)),
                  const SizedBox(height: 2),
                  Text(
                    value,
                    maxLines: 1,
                    softWrap: false,
                    overflow: TextOverflow.ellipsis,
                    style: HudText.body(size: 13, weight: FontWeight.w600),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

/// The HUD's top line: a spaced mono title, the CHAT | 3D flip and the menu.
class HudTopBar extends StatelessWidget {
  const HudTopBar({
    super.key,
    required this.title,
    required this.view,
    required this.onViewChanged,
    this.onMenu,
  });

  final String title;
  final ChatViewMode view;
  final ValueChanged<ChatViewMode> onViewChanged;
  final ValueChanged<ChatMenuAction>? onMenu;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Expanded(
        // Shrinks rather than clip on a narrow phone.
        child: FittedBox(
          fit: BoxFit.scaleDown,
          alignment: Alignment.centerLeft,
          child: Text(
            title,
            key: const Key('hud-top-title'),
            maxLines: 1,
            softWrap: false,
            style: HudText.label(size: 11, color: HudColors.cyan),
          ),
        ),
      ),
      const SizedBox(width: 8),
      ViewFlip(view: view, onChanged: onViewChanged),
      if (onMenu != null) ChatMenuButton(onSelected: onMenu!),
    ],
  );
}

/// The CHAT | 3D switch: a slim visual inside 44px touch targets.
class ViewFlip extends StatelessWidget {
  const ViewFlip({super.key, required this.view, required this.onChanged});
  final ChatViewMode view;
  final ValueChanged<ChatViewMode> onChanged;

  @override
  Widget build(BuildContext context) => Semantics(
    label: 'View',
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _segment(ChatViewMode.chat, 'CHAT', const Key('view-flip-chat')),
        _segment(ChatViewMode.threeD, '3D', const Key('view-flip-3d')),
      ],
    ),
  );

  Widget _segment(ChatViewMode mode, String label, Key key) {
    final on = mode == view;
    final first = mode == ChatViewMode.chat;
    return Semantics(
      button: true,
      selected: on,
      label: label == '3D' ? '3D view' : 'Chat view',
      excludeSemantics: true,
      child: InkWell(
        key: key,
        onTap: on ? null : () => onChanged(mode),
        child: SizedBox(
          height: 44,
          child: Center(
            child: Container(
              height: 26,
              constraints: const BoxConstraints(minWidth: 40),
              padding: const EdgeInsets.symmetric(horizontal: 8),
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: on ? HudColors.cyanA(0.18) : Colors.transparent,
                border: Border(
                  top: BorderSide(color: HudColors.cyanA(0.45)),
                  bottom: BorderSide(color: HudColors.cyanA(0.45)),
                  left: first
                      ? BorderSide(color: HudColors.cyanA(0.45))
                      : BorderSide.none,
                  right: BorderSide(color: HudColors.cyanA(0.45)),
                ),
              ),
              child: Text(
                label,
                style: HudText.label(
                  size: 10,
                  color: on ? HudColors.text : HudColors.textMuted,
                  weight: on ? FontWeight.w600 : FontWeight.w400,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// The small menu for what has no tab of its own.
class ChatMenuButton extends StatelessWidget {
  const ChatMenuButton({super.key, required this.onSelected});
  final ValueChanged<ChatMenuAction> onSelected;

  @override
  Widget build(BuildContext context) => PopupMenuButton<ChatMenuAction>(
    key: const Key('chat-menu'),
    tooltip: 'More',
    color: const Color(0xFF0B1118),
    shape: CutCornerBorder(
      cut: 8,
      side: BorderSide(color: HudColors.cyanA(0.45)),
    ),
    constraints: const BoxConstraints(minWidth: 200),
    style: IconButton.styleFrom(
      minimumSize: const Size(44, 44),
      fixedSize: const Size(44, 44),
    ),
    icon: const Icon(Icons.more_vert, size: 20, color: HudColors.cyan),
    onSelected: onSelected,
    itemBuilder: (_) => [
      _item(ChatMenuAction.companion, Icons.open_in_new, 'Companion stage'),
      _item(ChatMenuAction.threads, Icons.forum_outlined, 'Threads'),
      _item(ChatMenuAction.herPov, Icons.auto_awesome, 'Her POV'),
      _item(
        ChatMenuAction.subscription,
        Icons.workspace_premium_outlined,
        'Subscription',
      ),
      _item(
        ChatMenuAction.logout,
        Icons.logout,
        'Disconnect',
        color: HudColors.danger,
      ),
    ],
  );

  PopupMenuItem<ChatMenuAction> _item(
    ChatMenuAction value,
    IconData icon,
    String label, {
    Color color = HudColors.cyan,
  }) => PopupMenuItem(
    key: Key('chat-menu-${value.name}'),
    value: value,
    height: 44,
    child: Row(
      children: [
        Icon(icon, size: 18, color: color),
        const SizedBox(width: 12),
        Text(label.toUpperCase(), style: HudText.label(size: 11, color: color)),
      ],
    ),
  );
}

/// Her heartbeat as a scrolling ECG trace, one beat every 60/[bpm] seconds.
/// Still (a flat trace with two beats) when the platform asks for reduced
/// motion.
class HudEcg extends StatefulWidget {
  const HudEcg({super.key, required this.bpm, this.color = HudColors.cyan});
  final int bpm;
  final Color color;

  @override
  State<HudEcg> createState() => _HudEcgState();
}

class _HudEcgState extends State<HudEcg> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: _beat(),
  );

  Duration _beat() =>
      Duration(milliseconds: (60000 / math.max(1, widget.bpm)).round());

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  @override
  void didUpdateWidget(HudEcg oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.bpm != widget.bpm) {
      _controller.duration = _beat();
      if (_controller.isAnimating) {
        _controller
          ..stop()
          ..repeat();
      }
    }
  }

  void _sync() {
    final still = MediaQuery.maybeDisableAnimationsOf(context) ?? false;
    if (still) {
      _controller.stop();
    } else if (!_controller.isAnimating) {
      _controller.repeat();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RepaintBoundary(
    child: CustomPaint(
      size: Size.infinite,
      painter: _EcgPainter(
        repaint: _controller,
        progress: () => _controller.value,
        color: widget.color,
      ),
    ),
  );
}

class _EcgPainter extends CustomPainter {
  _EcgPainter({
    required Listenable repaint,
    required this.progress,
    required this.color,
  }) : super(repaint: repaint);

  final double Function() progress;
  final Color color;

  // One beat, as (x fraction, y as a fraction of the amplitude; up is -).
  static const _beat = [
    (0.0, 0.0),
    (0.32, 0.0),
    (0.38, -0.85),
    (0.45, 0.55),
    (0.50, 0.0),
    (0.66, 0.0),
    (0.72, -0.18),
    (0.78, 0.0),
    (1.0, 0.0),
  ];

  @override
  void paint(Canvas canvas, Size size) {
    if (size.isEmpty) return;
    final beatWidth = size.width / 2;
    final mid = size.height * 0.62;
    final amp = size.height * 0.6;
    final offset = progress() * beatWidth;
    final path = Path();
    var started = false;
    for (var b = -1; b <= 2; b++) {
      final x0 = b * beatWidth - offset;
      for (final (fx, fy) in _beat) {
        final p = Offset(x0 + fx * beatWidth, mid + fy * amp);
        if (!started) {
          path.moveTo(p.dx, p.dy);
          started = true;
        } else {
          path.lineTo(p.dx, p.dy);
        }
      }
    }
    canvas.save();
    canvas.clipRect(Offset.zero & size);
    canvas.drawPath(
      path,
      Paint()
        ..color = color
        ..strokeWidth = 1.4
        ..style = PaintingStyle.stroke
        ..strokeJoin = StrokeJoin.round,
    );
    canvas.restore();
  }

  @override
  bool shouldRepaint(_EcgPainter oldDelegate) => oldDelegate.color != color;
}
