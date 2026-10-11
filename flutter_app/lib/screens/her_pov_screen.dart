import 'dart:async';
import 'dart:convert';
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import '../main.dart' show GFL2Colors;
import '../models/memory.dart';
import '../services/memory_service.dart';
import '../services/session_auth.dart';
import 'memory_archive_screen.dart';

/// Her POV — she picks any real moment and draws it from her side.
/// Optimized for iOS web (safe area, large CTAs) and desktop (split layout).
class HerPovScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionLevel;
  final String affectionLevelName;

  /// Client for the start/poll calls. Injectable for tests.
  final http.Client? client;

  /// Gallery transport. Injectable for tests; defaults to a real service.
  final MemoryService? memoryService;

  /// What to do on a 401. Defaults to the shared login bounce; tests inject
  /// a recorder so the browser is never actually navigated.
  final VoidCallback? onAuthExpired;

  const HerPovScreen({
    super.key,
    required this.serverUrl,
    required this.affectionLevel,
    required this.affectionLevelName,
    this.client,
    this.memoryService,
    this.onAuthExpired,
  });

  /// What she says when the server throttles a request (429 on the POST —
  /// it charges the image_gen bucket). The link is fine, and so is she, so
  /// this must never read as "could not reach her".
  static String rateLimitedLine(int? retryAfterSeconds) {
    final s = retryAfterSeconds ?? 0;
    if (s <= 0) return '…One at a time, Commander. Give me a moment.';
    if (s <= 90) return '…One at a time, Commander. Give me a minute.';
    return '…One at a time, Commander. Ask me again in ${(s / 60).ceil()} minutes.';
  }

  @override
  State<HerPovScreen> createState() => _HerPovScreenState();
}

enum _Phase { idle, searching, thinking, drawing, done, failed }

class _HerPovScreenState extends State<HerPovScreen>
    with TickerProviderStateMixin {
  late final MemoryService _memories;
  late final http.Client _client;
  late final AnimationController _pulse;
  late final AnimationController _spin;

  _Phase _phase = _Phase.idle;
  String _status = 'She will choose a moment herself.';
  String? _jobId;
  String? _title;
  String? _annotation;
  String? _memoryId;
  List<Memory> _gallery = [];
  bool _galleryLoading = true;
  Timer? _poll;

  /// A poll is in flight. Timer.periodic keeps firing on a slow link; without
  /// this the ticks stack up and land out of order.
  bool _ticking = false;
  int _pollFailures = 0;
  static const int _maxPollFailures = 5;

  @override
  void initState() {
    super.initState();
    _memories = widget.memoryService ?? MemoryService(serverUrl: widget.serverUrl);
    _client = widget.client ?? http.Client();
    _pulse = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 1700))
      ..repeat(reverse: true);
    _spin = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 2200))
      ..repeat();
    _loadGallery();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _pulse.dispose();
    _spin.dispose();
    super.dispose();
  }

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${readAuthToken()}',
        'Content-Type': 'application/json',
      };

  void _authExpired() {
    _poll?.cancel();
    if (!mounted) return;
    (widget.onAuthExpired ?? handleAuthExpired)();
  }

  Future<void> _loadGallery() async {
    setState(() => _galleryLoading = true);
    try {
      // NOTE(paging): this is "newest 40, then filter on the client", so an
      // old Her POV portrait can fall off the end once newer memories pile
      // up. It cannot be pushed server-side yet: /api/memories filters by
      // category/month/before only, and a her_pov memory's category depends
      // on the affection level at the time (Precious Memories / The
      // Commander / Quiet Hours) — the stable marker is the `her_pov` scene
      // tag, which the server has no query for.
      final all = await _memories.fetchMemories(limit: 40);
      final hers = all.where((m) {
        final tags = m.sceneTags.map((t) => t.toLowerCase());
        return tags.contains('her_pov') ||
            tags.contains('from_her_side') ||
            m.category == 'Precious Memories';
      }).toList();
      if (!mounted) return;
      setState(() {
        _gallery = hers.isNotEmpty ? hers : all.take(12).toList();
        _galleryLoading = false;
      });
    } on MemoryServiceException catch (e) {
      if (!mounted) return;
      if (e.isAuthExpired) {
        _authExpired();
        return;
      }
      setState(() {
        _gallery = [];
        _galleryLoading = false;
      });
    } catch (_) {
      if (mounted) {
        setState(() {
          _gallery = [];
          _galleryLoading = false;
        });
      }
    }
  }

  bool get _busy =>
      _phase == _Phase.searching ||
      _phase == _Phase.thinking ||
      _phase == _Phase.drawing;

  Future<void> _start() async {
    if (_busy) return;
    HapticFeedback.mediumImpact();
    setState(() {
      _phase = _Phase.searching;
      _status = 'Searching our records…';
      _title = null;
      _annotation = null;
      _memoryId = null;
    });
    try {
      final resp = await _client.post(
        Uri.parse('${widget.serverUrl}/api/memories/her-pov'),
        headers: _headers,
      );
      if (!mounted) return;
      if (resp.statusCode == 401) {
        _authExpired();
        return;
      }
      if (resp.statusCode == 429) {
        // Throttled, not unreachable. Stand down (so the button re-arms)
        // and tell him when to ask again.
        setState(() {
          _phase = _Phase.idle;
          _status = HerPovScreen.rateLimitedLine(_retryAfter(resp));
        });
        return;
      }
      if (resp.statusCode != 202 && resp.statusCode != 200) {
        throw Exception('HTTP ${resp.statusCode}');
      }
      final data = jsonDecode(resp.body) as Map<String, dynamic>;
      _jobId = data['job_id'] as String?;
      _pollFailures = 0;
      _ticking = false;
      _poll?.cancel();
      _poll = Timer.periodic(const Duration(seconds: 2), (_) => _tick());
      await _tick();
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.failed;
        _status = 'Could not reach her. Try again.';
      });
    }
  }

  /// Seconds until the bucket refills: the Retry-After header, else the
  /// `retry_after` the structured error body carries. Null when neither.
  static int? _retryAfter(http.Response resp) {
    final header = int.tryParse(resp.headers['retry-after']?.trim() ?? '');
    if (header != null) return header;
    try {
      final body = jsonDecode(resp.body);
      final v = body is Map ? body['retry_after'] : null;
      if (v is int) return v;
      if (v is num) return v.ceil();
      if (v is String) return int.tryParse(v);
    } catch (_) {}
    return null;
  }

  Future<void> _tick() async {
    final id = _jobId;
    // Don't stack a second poll on a slow one: Timer.periodic fires on the
    // clock, not on completion.
    if (id == null || _ticking) return;
    _ticking = true;
    try {
      final http.Response resp;
      try {
        resp = await _client.get(
          Uri.parse('${widget.serverUrl}/api/memories/her-pov/$id'),
          headers: _headers,
        );
      } catch (_) {
        // A transport failure counts toward the give-up like a bad status —
        // a dead link used to poll for the life of the tab with the button
        // stuck disabled.
        _pollFailed('Lost the signal. Ask me again.');
        return;
      }
      if (!mounted) return;
      if (resp.statusCode == 401) {
        _authExpired();
        return;
      }
      if (resp.statusCode != 200) {
        // The job board is in-process, so a core restart makes a live job
        // 404 forever. Give up instead of polling for the life of the tab
        // with the button stuck disabled.
        _pollFailed('Lost track of that one. Ask me again.');
        return;
      }
      final Map<String, dynamic> data;
      try {
        data = jsonDecode(resp.body) as Map<String, dynamic>;
      } catch (_) {
        _pollFailed('Lost track of that one. Ask me again.');
        return;
      }
      _pollFailures = 0;
      final phase = (data['phase'] as String?) ?? 'searching';
      final mapped = switch (phase) {
        'thinking' => _Phase.thinking,
        'drawing' => _Phase.drawing,
        'done' => _Phase.done,
        'failed' => _Phase.failed,
        _ => _Phase.searching,
      };
      setState(() {
        _phase = mapped;
        _status = (data['message'] as String?) ?? _status;
        _title = data['title'] as String? ?? _title;
        _annotation = data['annotation'] as String? ?? _annotation;
        _memoryId = data['memory_id'] as String? ?? _memoryId;
      });
      if (mapped == _Phase.done || mapped == _Phase.failed) {
        _poll?.cancel();
        if (mapped == _Phase.done) {
          HapticFeedback.lightImpact();
          await _loadGallery();
        }
      }
    } finally {
      _ticking = false;
    }
  }

  /// One more strike against the poll. On the fifth, stop and say so.
  void _pollFailed(String line) {
    if (++_pollFailures < _maxPollFailures) return;
    _poll?.cancel();
    if (!mounted) return;
    setState(() {
      _phase = _Phase.failed;
      _status = line;
    });
  }

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.sizeOf(context);
    final pad = MediaQuery.paddingOf(context);
    final wide = size.width >= 900;

    return Scaffold(
      backgroundColor: GFL2Colors.background,
      body: Stack(
        children: [
          AnimatedBuilder(
            animation: _pulse,
            builder: (context, child) => DecoratedBox(
              decoration: BoxDecoration(
                gradient: RadialGradient(
                  center: const Alignment(0, -0.55),
                  radius: 1.15,
                  colors: [
                    Color.lerp(const Color(0xFF1A2740), const Color(0xFF2A3A58),
                        _pulse.value)!,
                    GFL2Colors.background,
                  ],
                ),
              ),
              child: const SizedBox.expand(),
            ),
          ),
          SafeArea(
            child: wide
                ? _buildDesktop(pad)
                : _buildMobile(pad),
          ),
        ],
      ),
    );
  }

  Widget _buildDesktop(EdgeInsets pad) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 12, 24, 20),
      child: Column(
        children: [
          _topBar(showTitle: true),
          const SizedBox(height: 16),
          Expanded(
            child: Row(
              children: [
                Expanded(flex: 5, child: _stage(showCta: true)),
                const SizedBox(width: 20),
                Expanded(flex: 4, child: _buildGalleryPanel()),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildMobile(EdgeInsets pad) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 12, 0),
          child: _topBar(showTitle: true),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
            children: [
              SizedBox(height: 460, child: _stage(showCta: false)),
              const SizedBox(height: 16),
              SizedBox(height: 300, child: _buildGalleryPanel()),
              const SizedBox(height: 100),
            ],
          ),
        ),
        ClipRect(
          child: BackdropFilter(
            filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
            child: Container(
              width: double.infinity,
              padding: EdgeInsets.fromLTRB(16, 12, 16, 10 + pad.bottom * 0.25),
              decoration: BoxDecoration(
                color: GFL2Colors.surface.withValues(alpha: 0.88),
                border: Border(
                  top: BorderSide(
                      color: GFL2Colors.border.withValues(alpha: 0.55)),
                ),
              ),
              child: _cta(fullWidth: true),
            ),
          ),
        ),
      ],
    );
  }

  Widget _topBar({required bool showTitle}) {
    return Row(
      children: [
        IconButton(
          tooltip: 'Back',
          onPressed: () => Navigator.pop(context),
          icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 18),
          color: GFL2Colors.textPrimary,
        ),
        if (showTitle)
          Text(
            'HER POV',
            style: TextStyle(
              color: GFL2Colors.primary.withValues(alpha: 0.95),
              letterSpacing: 3.5,
              fontSize: 12,
              fontWeight: FontWeight.w700,
            ),
          ),
      ],
    );
  }

  Widget _stage({required bool showCta}) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(22),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 14, sigmaY: 14),
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(22),
            border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.75)),
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
              Text('From her side',
                  style: TextStyle(
                    color: GFL2Colors.textPrimary,
                    fontSize: 26,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.4,
                  )),
              const SizedBox(height: 6),
              Text(
                'She picks any real exchange from your history, journals it, '
                'and draws the moment the way she saw it.',
                style: TextStyle(color: GFL2Colors.textDim, fontSize: 13.5, height: 1.4),
              ),
              const SizedBox(height: 10),
              Align(
                alignment: Alignment.centerLeft,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                  decoration: BoxDecoration(
                    color: GFL2Colors.affinity.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(20),
                    border: Border.all(
                        color: GFL2Colors.affinity.withValues(alpha: 0.35)),
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
              Text(_status,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: GFL2Colors.textDim, fontSize: 12.5)),
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
                          )
                        ]
                      : null,
                ),
              ),
              const SizedBox(height: 5),
              Text(labels[i],
                  style: TextStyle(
                    color: idx >= i ? GFL2Colors.primary : GFL2Colors.textDim,
                    fontSize: 10,
                    letterSpacing: 0.6,
                    fontWeight: idx == i ? FontWeight.w700 : FontWeight.w500,
                  )),
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
            Text(_title!,
                style: const TextStyle(
                    color: GFL2Colors.primary,
                    fontWeight: FontWeight.w600,
                    fontSize: 15)),
          const SizedBox(height: 10),
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(14),
              child: Image.network(
                _memories.imageUrl(_memoryId!),
                fit: BoxFit.cover,
                width: double.infinity,
                headers: _headers,
                errorBuilder: (context, error, stack) => const Icon(
                    Icons.image_outlined,
                    color: GFL2Colors.textDim,
                    size: 48),
              ),
            ),
          ),
          if (_annotation != null) ...[
            const SizedBox(height: 10),
            Text('“$_annotation”',
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: GFL2Colors.textPrimary.withValues(alpha: 0.92),
                  fontStyle: FontStyle.italic,
                  fontSize: 13.5,
                  height: 1.35,
                )),
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
        Text(headline,
            style: const TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 18,
              fontWeight: FontWeight.w600,
            )),
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
                  Icon(Icons.auto_awesome,
                      color: _busy
                          ? GFL2Colors.textDim
                          : const Color(0xFF0B1220)),
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

  Widget _buildGalleryPanel() {
    return ClipRRect(
      borderRadius: BorderRadius.circular(22),
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.7)),
          color: GFL2Colors.surface.withValues(alpha: 0.55),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
              child: Row(
                children: [
                  const Text('KEPT FROM HER SIDE',
                      style: TextStyle(
                        color: GFL2Colors.textDim,
                        fontSize: 11,
                        letterSpacing: 1.5,
                        fontWeight: FontWeight.w600,
                      )),
                  const Spacer(),
                  IconButton(
                    onPressed: _loadGallery,
                    icon: const Icon(Icons.refresh_rounded, size: 18),
                    color: GFL2Colors.textDim,
                    tooltip: 'Refresh',
                  ),
                ],
              ),
            ),
            Expanded(
              child: _galleryLoading
                  ? const Center(
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: GFL2Colors.primary))
                  : _gallery.isEmpty
                      ? Center(
                          child: Text(
                            'No portraits yet.\nAsk her to find a moment.',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                                color: GFL2Colors.textDim, height: 1.5),
                          ),
                        )
                      : GridView.builder(
                          padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                          gridDelegate:
                              const SliverGridDelegateWithFixedCrossAxisCount(
                            crossAxisCount: 2,
                            mainAxisSpacing: 10,
                            crossAxisSpacing: 10,
                            childAspectRatio: 0.78,
                          ),
                          itemCount: _gallery.length,
                          itemBuilder: (_, i) {
                            final m = _gallery[i];
                            return ClipRRect(
                              borderRadius: BorderRadius.circular(12),
                              child: Stack(
                                fit: StackFit.expand,
                                children: [
                                  Image.network(
                                    _memories.thumbnailUrl(m.id),
                                    fit: BoxFit.cover,
                                    headers: _headers,
                                    errorBuilder: (context, error, stack) => Container(
                                      color: GFL2Colors.panel,
                                      child: const Icon(
                                          Icons.image_not_supported_outlined,
                                          color: GFL2Colors.textDim),
                                    ),
                                  ),
                                  Positioned(
                                    left: 0,
                                    right: 0,
                                    bottom: 0,
                                    child: Container(
                                      padding: const EdgeInsets.fromLTRB(
                                          8, 18, 8, 8),
                                      decoration: BoxDecoration(
                                        gradient: LinearGradient(
                                          begin: Alignment.topCenter,
                                          end: Alignment.bottomCenter,
                                          colors: [
                                            Colors.transparent,
                                            Colors.black.withValues(alpha: 0.78),
                                          ],
                                        ),
                                      ),
                                      child: Text(
                                        m.annotation ?? m.category,
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(
                                          color: Colors.white,
                                          fontSize: 11,
                                          height: 1.25,
                                        ),
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            );
                          },
                        ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RingPainter extends CustomPainter {
  final double progress;
  final Color color;
  final bool active;
  _RingPainter(
      {required this.progress, required this.color, required this.active});

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
          ..strokeWidth = 3);
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
