/// Building blocks shared by the HUD tab screens (Album, Day, Kit, Me), on top
/// of the design system in theme/hud.dart: the screen header, 44 px touch
/// targets, toggles, setting rows, buttons, collapsible sections and the
/// loading / error / empty states.
library;

import 'package:flutter/material.dart';

import '../theme/hud.dart';

/// Dimmed copy that still reads at 4.5:1 on a HUD panel (textMuted does not
/// once a panel tint sits under it). Locked kit, later timeline blocks.
const kHudDimText = Color(0xFF6D8796);

/// The danger accent for SIGN OUT and error copy.
const kHudDanger = HudColors.danger;

/// Header of a HUD tab: `KICKER // LINE ........ TRAILING`, then the big title
/// with its small violet subtitle and optional actions on the right.
class HudScreenHeader extends StatelessWidget {
  const HudScreenHeader({
    super.key,
    required this.kicker,
    required this.title,
    this.subtitle,
    this.trailing,
    this.trailingKey,
    this.trailingColor = HudColors.violet,
    this.actions = const [],
  });

  final String kicker;
  final String title;
  final String? subtitle;
  final String? trailing;
  final Key? trailingKey;
  final Color trailingColor;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      HudSplitRow(
        left: Text(
          kicker.toUpperCase(),
          style: HudText.label(size: 11, color: HudColors.cyan),
        ),
        right: trailing == null
            ? null
            : Text(
                trailing!.toUpperCase(),
                key: trailingKey,
                textAlign: TextAlign.end,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: HudText.label(size: 11, color: trailingColor),
              ),
      ),
      const SizedBox(height: 10),
      Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: Wrap(
              crossAxisAlignment: WrapCrossAlignment.end,
              spacing: 8,
              runSpacing: 4,
              children: [
                Semantics(
                  header: true,
                  child: Text(title.toUpperCase(), style: HudText.display()),
                ),
                if (subtitle != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 2),
                    child: Text(
                      subtitle!.toUpperCase(),
                      style: HudText.label(
                        size: 11,
                        color: HudColors.violet,
                      ).copyWith(letterSpacing: 2.2),
                    ),
                  ),
              ],
            ),
          ),
          ...actions,
        ],
      ),
    ],
  );
}

/// A label on the left and a readout pinned to the right edge. The right
/// side keeps its natural width up to [rightShare] of the row (then wraps);
/// the left side takes the rest.
class HudSplitRow extends StatelessWidget {
  const HudSplitRow({
    super.key,
    required this.left,
    this.right,
    this.rightShare = 0.45,
  });

  final Widget left;
  final Widget? right;
  final double rightShare;

  @override
  Widget build(BuildContext context) {
    final r = right;
    if (r == null) return left;
    return LayoutBuilder(
      builder: (context, constraints) => Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(child: left),
          const SizedBox(width: 8),
          ConstrainedBox(
            constraints: BoxConstraints(
              maxWidth: constraints.maxWidth * rightShare,
            ),
            child: r,
          ),
        ],
      ),
    );
  }
}

/// [HudChip] with a 44 px tall touch target; the chip keeps its look.
class HudTapChip extends StatelessWidget {
  const HudTapChip({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => GestureDetector(
    behavior: HitTestBehavior.opaque,
    onTap: onTap,
    child: ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 44),
      child: Center(
        widthFactor: 1,
        child: HudChip(label: label, selected: selected, onTap: onTap),
      ),
    ),
  );
}

/// A wrapping row of filter chips, one selected.
class HudChipRow<T> extends StatelessWidget {
  const HudChipRow({
    super.key,
    required this.options,
    required this.value,
    required this.onChanged,
    this.keyPrefix = 'chip',
  });

  final Map<T, String> options;
  final T value;
  final ValueChanged<T> onChanged;
  final String keyPrefix;

  @override
  Widget build(BuildContext context) => Wrap(
    spacing: 6,
    children: [
      for (final entry in options.entries)
        HudTapChip(
          key: ValueKey('$keyPrefix-${entry.key}'),
          label: entry.value,
          selected: entry.key == value,
          onTap: () => onChanged(entry.key),
        ),
    ],
  );
}

/// Section title above a group of rows (VOICE, STAGE…).
class HudSectionLabel extends StatelessWidget {
  const HudSectionLabel(this.text, {super.key});
  final String text;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 6, bottom: 6),
    child: Semantics(
      header: true,
      child: Text(
        text.toUpperCase(),
        style: HudText.label(
          size: 10,
          color: HudColors.cyan,
        ).copyWith(letterSpacing: 1.8),
      ),
    ),
  );
}

/// The square on/off switch. Visual only: the row around it is the button.
class HudToggle extends StatelessWidget {
  const HudToggle({super.key, required this.value, this.enabled = true});
  final bool value;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final on = value && enabled;
    return Container(
      width: 46,
      height: 24,
      decoration: BoxDecoration(
        border: Border.all(
          color: on ? HudColors.cyanA(0.5) : const Color(0xFF2A3A46),
        ),
      ),
      child: AnimatedAlign(
        duration: const Duration(milliseconds: 140),
        alignment: on ? Alignment.centerRight : Alignment.centerLeft,
        child: Container(
          width: 16,
          height: 16,
          margin: const EdgeInsets.symmetric(horizontal: 3),
          color: on ? HudColors.cyan : const Color(0xFF3A4B58),
        ),
      ),
    );
  }
}

/// A setting as a cut-corner panel row: title, subtitle, and a control.
/// With [toggled] the whole row is one switch for assistive tech.
class HudSettingRow extends StatelessWidget {
  const HudSettingRow({
    super.key,
    required this.title,
    this.subtitle,
    this.subtitleWidget,
    required this.trailing,
    this.onTap,
    this.toggled,
    this.enabled = true,
  });

  final String title;

  /// The subtitle as read aloud; shown unless [subtitleWidget] is given.
  final String? subtitle;

  /// A richer visible subtitle (inline icons), e.g. [HudInlineText].
  final Widget? subtitleWidget;
  final Widget trailing;
  final VoidCallback? onTap;
  final bool? toggled;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final content = HudPanel(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 44),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text(
                    title,
                    style: HudText.body(
                      size: 16,
                      weight: FontWeight.w600,
                      color: enabled ? HudColors.text : HudColors.textDim,
                    ),
                  ),
                  if (subtitleWidget != null) ...[
                    const SizedBox(height: 1),
                    subtitleWidget!,
                  ] else if (subtitle != null) ...[
                    const SizedBox(height: 1),
                    Text(
                      subtitle!,
                      style: HudText.body(size: 12.5, color: HudColors.textDim),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(width: 12),
            trailing,
          ],
        ),
      ),
    );
    // A plain row (its trailing control is the button) keeps its children's
    // semantics; a switch row or a link row is one node.
    if (toggled == null && onTap == null) return content;
    return Semantics(
      container: true,
      button: toggled == null,
      toggled: toggled,
      enabled: enabled,
      label: subtitle == null ? title : '$title. $subtitle',
      excludeSemantics: true,
      onTap: enabled ? onTap : null,
      child: InkWell(onTap: enabled ? onTap : null, child: content),
    );
  }
}

/// Body text with inline icons standing in for symbols the display fonts
/// lack (▶, ⇄): pass strings and [IconData] in order.
class HudInlineText extends StatelessWidget {
  const HudInlineText(
    this.parts, {
    super.key,
    this.size = 12.5,
    this.color = HudColors.textDim,
  });

  final List<Object> parts;
  final double size;
  final Color color;

  @override
  Widget build(BuildContext context) => Text.rich(
    TextSpan(
      style: HudText.body(size: size, color: color),
      children: [
        for (final p in parts)
          if (p is IconData)
            WidgetSpan(
              alignment: PlaceholderAlignment.middle,
              child: Icon(p, size: size + 2, color: color),
            )
          else
            TextSpan(text: '$p'),
      ],
    ),
  );
}

/// A full-width cut-corner button, at least 44 px tall.
class HudActionButton extends StatelessWidget {
  const HudActionButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.danger = false,
    this.icon,
    this.busy = false,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool danger;
  final IconData? icon;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final color = danger ? kHudDanger : HudColors.cyan;
    final enabled = onPressed != null && !busy;
    return Semantics(
      button: true,
      enabled: enabled,
      label: label,
      excludeSemantics: true,
      child: Material(
        color: color.withValues(alpha: danger ? 0.06 : 0.08),
        shape: CutCornerBorder(
          side: BorderSide(
            color: danger ? color.withValues(alpha: 0.5) : color,
          ),
        ),
        child: InkWell(
          customBorder: const CutCornerBorder(),
          onTap: enabled ? onPressed : null,
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 44),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  if (busy)
                    SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(
                        strokeWidth: 1.5,
                        color: color,
                      ),
                    )
                  else if (icon != null)
                    Icon(icon, size: 16, color: color),
                  if (busy || icon != null) const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      label.toUpperCase(),
                      textAlign: TextAlign.center,
                      style: HudText.label(size: 11, color: color),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// A 44×44 outlined icon button for header actions.
class HudIconAction extends StatelessWidget {
  const HudIconAction({
    super.key,
    required this.icon,
    required this.label,
    required this.onPressed,
    this.color = HudColors.cyan,
  });

  final IconData icon;
  final String label;
  final VoidCallback onPressed;
  final Color color;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(left: 6),
    child: Tooltip(
      message: label,
      child: Semantics(
        button: true,
        label: label,
        excludeSemantics: true,
        child: InkWell(
          onTap: onPressed,
          child: Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              border: Border.all(color: color.withValues(alpha: 0.45)),
              color: color.withValues(alpha: 0.05),
            ),
            child: Icon(icon, size: 20, color: color),
          ),
        ),
      ),
    ),
  );
}

/// A collapsible HUD panel (dossier sections, worn log).
class HudCollapsible extends StatefulWidget {
  const HudCollapsible({
    super.key,
    required this.title,
    required this.child,
    this.initiallyExpanded = false,
  });

  final String title;
  final Widget child;
  final bool initiallyExpanded;

  @override
  State<HudCollapsible> createState() => _HudCollapsibleState();
}

class _HudCollapsibleState extends State<HudCollapsible> {
  late bool _open = widget.initiallyExpanded;

  @override
  Widget build(BuildContext context) => HudPanel(
    padding: EdgeInsets.zero,
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Semantics(
          button: true,
          expanded: _open,
          label: widget.title,
          excludeSemantics: true,
          child: InkWell(
            onTap: () => setState(() => _open = !_open),
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 48),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        widget.title.toUpperCase(),
                        style: HudText.label(size: 10.5, color: HudColors.cyan),
                      ),
                    ),
                    Icon(
                      _open ? Icons.expand_less : Icons.expand_more,
                      size: 20,
                      color: HudColors.textDim,
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
        if (_open)
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
            child: widget.child,
          ),
      ],
    ),
  );
}

/// Centered spinner with a mono caption.
class HudLoading extends StatelessWidget {
  const HudLoading(this.label, {super.key});
  final String label;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 48),
    child: Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(
              strokeWidth: 1.5,
              color: HudColors.cyan,
            ),
          ),
          const SizedBox(height: 12),
          Text(label.toUpperCase(), style: HudText.label()),
        ],
      ),
    ),
  );
}

/// The link is down: what failed, a line in her voice, and RETRY.
class HudErrorState extends StatelessWidget {
  const HudErrorState({
    super.key,
    required this.title,
    required this.line,
    required this.onRetry,
  });
  final String title;
  final String line;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 40, horizontal: 8),
    child: Column(
      children: [
        Icon(
          Icons.sensors_off,
          size: 40,
          color: kHudDanger.withValues(alpha: 0.6),
        ),
        const SizedBox(height: 12),
        Text(
          title.toUpperCase(),
          textAlign: TextAlign.center,
          style: HudText.label(size: 11, color: kHudDanger),
        ),
        const SizedBox(height: 6),
        Text(
          line,
          textAlign: TextAlign.center,
          style: HudText.body(size: 14, color: HudColors.textDim),
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: 180,
          child: HudActionButton(label: 'Retry', onPressed: onRetry),
        ),
      ],
    ),
  );
}

/// Nothing here yet, said plainly.
class HudEmptyState extends StatelessWidget {
  const HudEmptyState({
    super.key,
    required this.icon,
    required this.title,
    required this.line,
  });
  final IconData icon;
  final String title;
  final String line;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 40, horizontal: 8),
    child: Column(
      children: [
        Icon(icon, size: 40, color: HudColors.cyanA(0.35)),
        const SizedBox(height: 12),
        Text(
          title.toUpperCase(),
          textAlign: TextAlign.center,
          style: HudText.label(size: 11),
        ),
        const SizedBox(height: 6),
        Text(
          line,
          textAlign: TextAlign.center,
          style: HudText.body(size: 14, color: HudColors.textDim),
        ),
      ],
    ),
  );
}

/// 45° stripes, the fill of something she hasn't shown him yet.
class HudStripes extends StatelessWidget {
  const HudStripes({super.key, this.color = HudColors.violet, this.child});
  final Color color;
  final Widget? child;
  @override
  Widget build(BuildContext context) =>
      CustomPaint(painter: _StripePainter(color), child: child);
}

class _StripePainter extends CustomPainter {
  const _StripePainter(this.color);
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = color.withValues(alpha: 0.07)
      ..strokeWidth = 6;
    canvas.save();
    canvas.clipRect(Offset.zero & size);
    for (double x = -size.height; x < size.width; x += 12) {
      canvas.drawLine(Offset(x, size.height), Offset(x + size.height, 0), p);
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _StripePainter old) => old.color != color;
}

/// A short HUD-styled snackbar line.
void showHudSnack(BuildContext context, String line, {Key? key}) {
  final messenger = ScaffoldMessenger.maybeOf(context);
  if (messenger == null) return;
  messenger
    ..hideCurrentSnackBar()
    ..showSnackBar(
      SnackBar(
        key: key,
        duration: const Duration(seconds: 3),
        behavior: SnackBarBehavior.floating,
        backgroundColor: const Color(0xFF0D141C),
        shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(0.45))),
        content: Text(line, style: HudText.body(size: 14)),
      ),
    );
}

const _months = [
  'JAN',
  'FEB',
  'MAR',
  'APR',
  'MAY',
  'JUN',
  'JUL',
  'AUG',
  'SEP',
  'OCT',
  'NOV',
  'DEC',
];

/// `OCT 18`.
String hudShortDate(DateTime d) =>
    '${_months[d.month - 1]} ${d.day.toString().padLeft(2, '0')}';

/// `MON 19 OCT`.
String hudDayDate(DateTime d) {
  const days = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
  return '${days[d.weekday - 1]} ${d.day} ${_months[d.month - 1]}';
}

/// `21:40`.
String hudClock(DateTime d) =>
    '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
