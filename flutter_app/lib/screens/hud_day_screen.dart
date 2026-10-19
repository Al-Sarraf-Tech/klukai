import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../services/wardrobe_service.dart';
import '../theme/hud.dart';
import '../widgets/hud_parts.dart';

/// Where a block of her day stands against now.
enum BlockState { done, active, later }

/// Done, active and later for each block. The server marks the current
/// block; without a mark (none fits the hour) the device clock decides.
List<BlockState> scheduleStates(List<ScheduleBlock> blocks, DateTime now) {
  final hhmm = now.hour * 100 + now.minute;
  // `end` is exclusive; a block ending at 0000 runs to midnight.
  int endOf(ScheduleBlock b, int start) {
    final end = int.tryParse(b.end);
    return end == null || end <= start ? 2400 : end;
  }

  var current = blocks.indexWhere((b) => b.current);
  if (current < 0) {
    current = blocks.indexWhere((b) {
      final start = int.tryParse(b.start);
      return start != null && start <= hhmm && hhmm < endOf(b, start);
    });
  }
  if (current < 0) {
    return [
      for (final b in blocks)
        endOf(b, int.tryParse(b.start) ?? 0) <= hhmm
            ? BlockState.done
            : BlockState.later,
    ];
  }
  return [
    for (var i = 0; i < blocks.length; i++)
      i < current
          ? BlockState.done
          : i == current
          ? BlockState.active
          : BlockState.later,
  ];
}

/// `0500` → `05:00`.
String hudHhmm(String s) =>
    s.length == 4 ? '${s.substring(0, 2)}:${s.substring(2)}' : s;

enum _Phase { loading, ready, error }

/// Day tab of the Tactical HUD: what she has on and why, the weather, and her
/// roster for today as a timeline. An embedded tab body; the shell owns the
/// Scaffold and the tab bar.
class HudDayScreen extends StatefulWidget {
  const HudDayScreen({
    super.key,
    required this.serverUrl,
    this.wardrobeService,
    this.onAuthExpired,
    this.refresh,
    this.now,
  });

  final String serverUrl;

  /// /api/her-day; defaults to a real [WardrobeService].
  final WardrobeService? wardrobeService;

  /// 401: the token is dead; the shell's logout/reauth path.
  final VoidCallback? onAuthExpired;

  /// Reloads whenever this fires (e.g. the Kit tab changed her outfit).
  final Listenable? refresh;

  /// Clock for the timeline fallback and the date line; tests pin it.
  final DateTime Function()? now;

  @override
  State<HudDayScreen> createState() => _HudDayScreenState();
}

class _HudDayScreenState extends State<HudDayScreen> {
  late final WardrobeService _service =
      widget.wardrobeService ?? WardrobeService(serverUrl: widget.serverUrl);
  _Phase _phase = _Phase.loading;
  HerDay? _day;
  bool _authBounced = false;

  DateTime get _now => (widget.now ?? DateTime.now)();

  @override
  void initState() {
    super.initState();
    widget.refresh?.addListener(_load);
    _load();
  }

  @override
  void didUpdateWidget(HudDayScreen old) {
    super.didUpdateWidget(old);
    if (old.refresh != widget.refresh) {
      old.refresh?.removeListener(_load);
      widget.refresh?.addListener(_load);
    }
  }

  @override
  void dispose() {
    widget.refresh?.removeListener(_load);
    super.dispose();
  }

  Future<void> _load() async {
    if (_day == null && _phase != _Phase.loading) {
      setState(() => _phase = _Phase.loading);
    }
    try {
      final day = await _service.fetchHerDay();
      if (!mounted) return;
      setState(() {
        _day = day;
        _phase = _Phase.ready;
      });
    } on WardrobeServiceException catch (e) {
      if (e.isAuthExpired) {
        if (!_authBounced) {
          _authBounced = true;
          widget.onAuthExpired?.call();
        }
        return;
      }
      if (mounted && _day == null) setState(() => _phase = _Phase.error);
    } catch (_) {
      // A refresh that fails keeps the day already on screen.
      if (mounted && _day == null) setState(() => _phase = _Phase.error);
    }
  }

  DateTime get _date => DateTime.tryParse(_day?.date ?? '') ?? _now;

  @override
  Widget build(BuildContext context) => HudBackground(
    child: RefreshIndicator(
      color: HudColors.cyan,
      backgroundColor: HudColors.ground,
      onRefresh: _load,
      child: ListView(
        key: const Key('day-scroll'),
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
        children: [
          HudScreenHeader(
            kicker: 'Ops log // Today',
            trailing: hudDayDate(_date),
            trailingColor: HudColors.cyan,
            title: 'Her day',
            subtitle: 'Live',
          ),
          const SizedBox(height: 12),
          ...switch (_phase) {
            _Phase.loading => [const HudLoading('Syncing her roster...')],
            _Phase.error => [
              HudErrorState(
                key: const Key('day-error'),
                title: 'Signal lost // roster unreachable',
                line: 'Her day is still happening. The link is not.',
                onRetry: _load,
              ),
            ],
            _Phase.ready => _body(_day!),
          },
        ],
      ),
    ),
  );

  List<Widget> _body(HerDay day) {
    final status = day.status;
    final states = scheduleStates(day.schedule, _now);
    return [
      if (status?.override != null) ...[
        HudPanel(
          key: const Key('day-override'),
          accent: HudColors.violet,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              HudLabel(
                'Override · ${status!.override}',
                color: HudColors.violet,
              ),
              if (status.label.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text(status.label, style: HudText.body(size: 15)),
              ],
            ],
          ),
        ),
        const SizedBox(height: 8),
      ],
      _EquippedCard(outfit: day.outfit),
      const SizedBox(height: 8),
      IntrinsicHeight(
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Expanded(
              child: _Readout(
                key: const Key('day-weather'),
                label: 'Weather',
                value: _weatherLine(day.weather),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _Readout(
                key: const Key('day-requests'),
                label: 'Outfit requests',
                value: (day.outfit?.requestedByCommander ?? false)
                    ? 'Yours today'
                    : 'None today',
              ),
            ),
          ],
        ),
      ),
      const SizedBox(height: 14),
      if (day.schedule.isEmpty)
        const HudEmptyState(
          key: Key('day-empty'),
          icon: Icons.schedule,
          title: 'No roster today.',
          line: 'Nothing on the board. Ask her what she is up to.',
        )
      else
        for (var i = 0; i < day.schedule.length; i++)
          _TimelineEntry(
            key: ValueKey('day-block-$i'),
            block: day.schedule[i],
            state: states[i],
            first: i == 0,
            last: i == day.schedule.length - 1,
          ),
    ];
  }

  /// `Clear · 9°C`.
  static String _weatherLine(HerDayWeather? w) {
    if (w == null) return '—';
    final cond = w.condition.isEmpty
        ? ''
        : w.condition[0].toUpperCase() + w.condition.substring(1);
    final temp = w.tempC == null ? '' : '${w.tempC!.round()}°C';
    if (cond.isEmpty && temp.isEmpty) return '—';
    if (cond.isEmpty) return temp;
    return temp.isEmpty ? cond : '$cond · $temp';
  }
}

class _EquippedCard extends StatelessWidget {
  const _EquippedCard({required this.outfit});
  final OutfitInfo? outfit;

  @override
  Widget build(BuildContext context) {
    final o = outfit;
    final line = o?.sourceLine ?? '';
    final quoted = o != null && o.source == 'auto' && line.isNotEmpty;
    return HudPanel(
      key: const Key('day-equipped'),
      padding: const EdgeInsets.all(10),
      child: Row(
        children: [
          Container(
            width: 74,
            height: 92,
            decoration: BoxDecoration(
              border: Border.all(color: HudColors.violetA(0.6)),
            ),
            child: Image.asset(
              'assets/klukai_portrait.png',
              fit: BoxFit.cover,
              alignment: const Alignment(0, -0.7),
              semanticLabel: 'Klukai',
              errorBuilder: (_, _, _) => Center(
                child: Text('K', style: HudText.display(color: HudColors.cyan)),
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const HudLabel('Equipped today'),
                const SizedBox(height: 3),
                Text(
                  o?.name ?? '—',
                  key: const Key('day-outfit-name'),
                  style: HudText.body(size: 18, weight: FontWeight.w600),
                ),
                if (line.isNotEmpty) ...[
                  const SizedBox(height: 4),
                  Text(
                    quoted ? '"$line"' : line,
                    key: const Key('day-outfit-reason'),
                    style: HudText.body(
                      size: 13.5,
                      color: o!.requestedByCommander
                          ? HudColors.violet
                          : const Color(0xFFA9C6D4),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Readout extends StatelessWidget {
  const _Readout({super.key, required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => HudPanel(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 7),
    child: Semantics(
      label: '$label: $value',
      excludeSemantics: true,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          HudLabel(label),
          const SizedBox(height: 2),
          Text(
            value,
            style: HudText.label(
              size: 15,
              color: HudColors.text,
              weight: FontWeight.w600,
            ).copyWith(letterSpacing: 0.4),
          ),
        ],
      ),
    ),
  );
}

/// One block on the vertical timeline: a diamond on the rail, time, place and
/// what she is doing. The active block is boxed and tagged ACTIVE.
class _TimelineEntry extends StatelessWidget {
  const _TimelineEntry({
    super.key,
    required this.block,
    required this.state,
    required this.first,
    required this.last,
  });

  final ScheduleBlock block;
  final BlockState state;
  final bool first;
  final bool last;

  @override
  Widget build(BuildContext context) {
    final active = state == BlockState.active;
    final later = state == BlockState.later;
    final title = block.location.isNotEmpty ? block.location : block.activity;
    final sub = block.location.isNotEmpty ? block.activity : '';
    final time = active
        ? '${hudHhmm(block.start)} — NOW'
        : hudHhmm(block.start);
    final stateWord = switch (state) {
      BlockState.done => 'done',
      BlockState.active => 'active now',
      BlockState.later => 'later',
    };

    final text = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                time,
                style: HudText.label(
                  size: 11,
                  color: later ? kHudDimText : HudColors.cyan,
                ).copyWith(letterSpacing: 1.1),
              ),
            ),
            if (active)
              const HudBadge(
                'Active',
                key: Key('day-active-tag'),
                color: HudColors.violet,
                filled: true,
              ),
          ],
        ),
        const SizedBox(height: 2),
        Text(
          title,
          style: HudText.body(
            size: 16,
            weight: FontWeight.w600,
            color: later ? kHudDimText : HudColors.text,
          ),
        ),
        if (sub.isNotEmpty)
          Text(
            sub,
            style: HudText.body(
              size: 12.5,
              color: later ? kHudDimText : HudColors.textDim,
            ),
          ),
      ],
    );

    return Semantics(
      label:
          '${hudHhmm(block.start)} to ${hudHhmm(block.end)}, $stateWord. ${block.line}',
      excludeSemantics: true,
      child: IntrinsicHeight(
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SizedBox(
              width: 22,
              child: CustomPaint(
                painter: _RailPainter(state: state, first: first, last: last),
              ),
            ),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: active
                    ? DecoratedBox(
                        key: const Key('day-active-block'),
                        decoration: BoxDecoration(
                          color: HudColors.cyanA(0.09),
                          border: Border.all(color: HudColors.cyan),
                        ),
                        child: Padding(
                          padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
                          child: text,
                        ),
                      )
                    : Padding(
                        padding: const EdgeInsets.fromLTRB(0, 4, 0, 6),
                        child: text,
                      ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RailPainter extends CustomPainter {
  const _RailPainter({
    required this.state,
    required this.first,
    required this.last,
  });
  final BlockState state;
  final bool first;
  final bool last;

  @override
  void paint(Canvas canvas, Size size) {
    const x = 7.5;
    const y = 18.0;
    final rail = Paint()
      ..color = HudColors.cyanA(0.3)
      ..strokeWidth = 1;
    canvas.drawLine(
      Offset(x, first ? y : 0),
      Offset(x, last ? y : size.height),
      rail,
    );
    final active = state == BlockState.active;
    final color = active ? HudColors.violet : HudColors.cyan;
    final d = active ? 6.5 : 5.0;
    final diamond = Path()
      ..moveTo(x, y - d)
      ..lineTo(x + d, y)
      ..lineTo(x, y + d)
      ..lineTo(x - d, y)
      ..close();
    if (active) {
      canvas.drawPath(
        diamond,
        Paint()
          ..color = HudColors.violet
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 5),
      );
    }
    canvas.drawPath(
      diamond,
      Paint()..color = state == BlockState.later ? HudColors.ground : color,
    );
    canvas.drawPath(
      diamond,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1,
    );
  }

  @override
  bool shouldRepaint(covariant _RailPainter old) =>
      old.state != state || old.first != first || old.last != last;
}
