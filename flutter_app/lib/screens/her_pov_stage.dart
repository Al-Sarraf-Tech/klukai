part of 'her_pov_screen.dart';

/// Generation stage, phase rail, progress, and retry action presentation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _HerPovStage on _HerPovScreenState {
  Widget _stage({required bool showCta}) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(22),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 14, sigmaY: 14),
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(22),
            border: Border.all(
              color: GFL2Colors.border.withValues(alpha: 0.75),
            ),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                GFL2Colors.panel.withValues(alpha: 0.92),
                GFL2Colors.surface.withValues(alpha: 0.72),
              ],
            ),
          ),
          padding: const EdgeInsets.all(22),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'From her side',
                style: TextStyle(
                  color: GFL2Colors.textPrimary,
                  fontSize: 26,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.4,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                'She picks any real exchange from your history, journals it, '
                'and draws the moment the way she saw it.',
                style: TextStyle(
                  color: GFL2Colors.textDim,
                  fontSize: 13.5,
                  height: 1.4,
                ),
              ),
              const SizedBox(height: 10),
              Align(
                alignment: Alignment.centerLeft,
                child: Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 5,
                  ),
                  decoration: BoxDecoration(
                    color: GFL2Colors.affinity.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(20),
                    border: Border.all(
                      color: GFL2Colors.affinity.withValues(alpha: 0.35),
                    ),
                  ),
                  child: Text(
                    'Bond · Lv.${widget.affectionLevel} ${widget.affectionLevelName}',
                    style: const TextStyle(
                      color: GFL2Colors.affinity,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 18),
              _phaseRail(),
              const SizedBox(height: 18),
              Expanded(child: _center()),
              const SizedBox(height: 10),
              Text(
                _status,
                textAlign: TextAlign.center,
                style: TextStyle(color: GFL2Colors.textDim, fontSize: 12.5),
              ),
              if (showCta) ...[
                const SizedBox(height: 14),
                _cta(fullWidth: true),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _phaseRail() {
    const labels = ['Search', 'Think', 'Draw', 'Keep'];
    final idx = switch (_phase) {
      _Phase.searching => 0,
      _Phase.thinking => 1,
      _Phase.drawing => 2,
      _Phase.done => 3,
      _ => -1,
    };
    return Row(
      children: [
        for (var i = 0; i < labels.length; i++) ...[
          if (i > 0)
            Expanded(
              child: Container(
                height: 2,
                margin: const EdgeInsets.symmetric(horizontal: 4),
                color: idx >= i
                    ? GFL2Colors.primary.withValues(alpha: 0.75)
                    : GFL2Colors.border,
              ),
            ),
          Column(
            children: [
              Container(
                width: 11,
                height: 11,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: idx > i ? GFL2Colors.primary : Colors.transparent,
                  border: Border.all(
                    color: idx >= i ? GFL2Colors.primary : GFL2Colors.textDim,
                    width: 2,
                  ),
                  boxShadow: idx == i
                      ? [
                          BoxShadow(
                            color: GFL2Colors.primary.withValues(alpha: 0.5),
                            blurRadius: 10,
                          ),
                        ]
                      : null,
                ),
              ),
              const SizedBox(height: 5),
              Text(
                labels[i],
                style: TextStyle(
                  color: idx >= i ? GFL2Colors.primary : GFL2Colors.textDim,
                  fontSize: 10,
                  letterSpacing: 0.6,
                  fontWeight: idx == i ? FontWeight.w700 : FontWeight.w500,
                ),
              ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _center() {
    if (_phase == _Phase.done && _memoryId != null) {
      return Column(
        children: [
          if (_title != null)
            Text(
              _title!,
              style: const TextStyle(
                color: GFL2Colors.primary,
                fontWeight: FontWeight.w600,
                fontSize: 15,
              ),
            ),
          const SizedBox(height: 10),
          Expanded(
            child: ArtworkView.network(
              url: _memories.imageUrl(_memoryId!),
              headers: _headers,
            ),
          ),
          if (_annotation != null) ...[
            const SizedBox(height: 10),
            Text(
              '“$_annotation”',
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
              style: TextStyle(
                color: GFL2Colors.textPrimary.withValues(alpha: 0.92),
                fontStyle: FontStyle.italic,
                fontSize: 13.5,
                height: 1.35,
              ),
            ),
          ],
          TextButton.icon(
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(
                builder: (_) => MemoryArchiveScreen(
                  serverUrl: widget.serverUrl,
                  affectionLevel: widget.affectionLevel,
                  affectionLevelName: widget.affectionLevelName,
                ),
              ),
            ),
            icon: const Icon(Icons.photo_album_outlined, size: 16),
            label: const Text('Open album'),
            style: TextButton.styleFrom(foregroundColor: GFL2Colors.primary),
          ),
        ],
      );
    }

    final headline = switch (_phase) {
      _Phase.idle => 'Standing by.',
      _Phase.searching => 'Combing the archive…',
      _Phase.thinking => 'Replaying the moment…',
      _Phase.drawing => 'Sketching from her side…',
      _Phase.done => 'Kept.',
      _Phase.failed => 'Interrupted.',
    };

    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        AnimatedBuilder(
          animation: _spin,
          builder: (context, child) => CustomPaint(
            size: const Size(118, 118),
            painter: _RingPainter(
              progress: _spin.value,
              color: _phase == _Phase.failed
                  ? GFL2Colors.danger
                  : GFL2Colors.primary,
              active: _busy,
            ),
          ),
        ),
        const SizedBox(height: 18),
        Text(
          headline,
          style: const TextStyle(
            color: GFL2Colors.textPrimary,
            fontSize: 18,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }

  Widget _cta({required bool fullWidth}) {
    final label = _busy
        ? 'Working…'
        : (_phase == _Phase.done ? 'Another moment' : 'Find a moment');
    return AnimatedBuilder(
      animation: _pulse,
      builder: (context, child) {
        final glow = _busy ? 0.08 : 0.22 + 0.14 * _pulse.value;
        return Material(
          color: Colors.transparent,
          child: InkWell(
            onTap: _busy ? null : _start,
            borderRadius: BorderRadius.circular(16),
            child: Ink(
              height: 58,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(16),
                gradient: LinearGradient(
                  colors: _busy
                      ? [GFL2Colors.panel, GFL2Colors.surface]
                      : [
                          GFL2Colors.primary.withValues(alpha: 0.95),
                          const Color(0xFF2B8FD9),
                        ],
                ),
                boxShadow: [
                  BoxShadow(
                    color: GFL2Colors.primary.withValues(alpha: glow),
                    blurRadius: 22,
                  ),
                ],
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    Icons.auto_awesome,
                    color: _busy ? GFL2Colors.textDim : const Color(0xFF0B1220),
                  ),
                  const SizedBox(width: 10),
                  Flexible(
                    child: Text(
                      label,
                      style: TextStyle(
                        color: _busy
                            ? GFL2Colors.textDim
                            : const Color(0xFF0B1220),
                        fontSize: 16,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

class _RingPainter extends CustomPainter {
  final double progress;
  final Color color;
  final bool active;
  _RingPainter({
    required this.progress,
    required this.color,
    required this.active,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final c = Offset(size.width / 2, size.height / 2);
    final r = size.width * 0.38;
    canvas.drawCircle(
      c,
      r,
      Paint()
        ..color = color.withValues(alpha: 0.15)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 3,
    );
    if (!active) return;
    canvas.drawCircle(
      c,
      r,
      Paint()
        ..shader = SweepGradient(
          colors: [
            color.withValues(alpha: 0),
            color,
            color.withValues(alpha: 0),
          ],
          transform: GradientRotation(progress * 6.28318),
        ).createShader(Rect.fromCircle(center: c, radius: r))
        ..style = PaintingStyle.stroke
        ..strokeWidth = 3
        ..strokeCap = StrokeCap.round,
    );
  }

  @override
  bool shouldRepaint(covariant _RingPainter old) =>
      old.progress != progress || old.active != active;
}
