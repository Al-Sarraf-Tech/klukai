import 'dart:async';
import 'dart:convert';
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

import '../widgets/artwork_view.dart';
import '../main.dart' show GFL2Colors;
import '../models/memory.dart';
import '../services/gallery_board.dart';
import '../services/her_pov_job_owner.dart';
import '../services/memory_service.dart';
import 'memory_archive_screen.dart';

part 'her_pov_stage.dart';

part 'her_pov_layout.dart';

/// Her POV — she picks any real moment and draws it from her side.
/// Optimized for iOS web (safe area, large CTAs) and desktop (split layout).
class HerPovScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionLevel;
  final String affectionLevelName;

  /// Deterministic archive and image-job boundaries; caller owns injected clients.
  final MemoryService? memoryService;
  final http.Client? httpClient;

  const HerPovScreen({
    super.key,
    required this.serverUrl,
    required this.affectionLevel,
    required this.affectionLevelName,
    this.memoryService,
    this.httpClient,
  });

  @override
  State<HerPovScreen> createState() => _HerPovScreenState();
}

enum _Phase { idle, searching, thinking, drawing, done, failed }

class _HerPovScreenState extends State<HerPovScreen>
    with TickerProviderStateMixin {
  late final MemoryService _memories;
  late final AnimationController _pulse;
  late final AnimationController _spin;

  _Phase _phase = _Phase.idle;
  String _status = 'She will choose a moment herself.';
  final _jobOwner = HerPovJobOwner();
  http.Client? _ownedHttp;
  String? _title;
  String? _annotation;
  String? _memoryId;
  GalleryBoard _board = const GalleryBoard();
  Timer? _poll;
  int _pollFailures = 0;
  static const int _maxPollFailures = 5;

  @override
  void initState() {
    super.initState();
    _memories =
        widget.memoryService ?? MemoryService(serverUrl: widget.serverUrl);
    _pulse = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1700),
    )..repeat(reverse: true);
    _spin = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2200),
    )..repeat();
    _loadGallery();
  }

  @override
  void dispose() {
    _jobOwner.dispose();
    _poll?.cancel();
    _ownedHttp?.close();
    _pulse.dispose();
    _spin.dispose();
    super.dispose();
  }

  Map<String, String> get _headers {
    var token = '';
    try {
      token = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  void _handleAuthExpired() {
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
    web.window.location.href = '/';
  }

  Future<void> _loadGallery() async {
    final previous = _board;
    if (previous.phase != GalleryPhase.loading &&
        !previous.hasGood &&
        mounted) {
      setState(() => _board = previous.loading());
    }
    try {
      final next = await GalleryBoard.load(
        previous: previous,
        fetch: () => _memories.fetchMemories(limit: 40),
      );
      if (!mounted) return;
      setState(() => _board = next);
    } on MemoryServiceException catch (error) {
      if (!mounted) return;
      if (error.isAuthExpired) {
        _handleAuthExpired();
        return;
      }
      setState(() {
        _board = GalleryBoard(
          phase: GalleryPhase.error,
          items: previous.items,
          hasGood: previous.hasGood,
        );
      });
    }
  }

  bool get _busy =>
      _phase == _Phase.searching ||
      _phase == _Phase.thinking ||
      _phase == _Phase.drawing;

  Future<void> _start() async {
    if (!mounted || _busy) return;
    final generation = _jobOwner.begin();
    _poll?.cancel();
    HapticFeedback.mediumImpact();
    setState(() {
      _phase = _Phase.searching;
      _status = 'Searching our records…';
      _title = null;
      _annotation = null;
      _memoryId = null;
    });
    try {
      final resp = await _jobHttp.post(
        Uri.parse('${widget.serverUrl}/api/memories/her-pov'),
        headers: _headers,
      );
      if (!mounted || !_jobOwner.isCurrent(generation)) return;
      if (resp.statusCode != 202 && resp.statusCode != 200) {
        throw Exception('HTTP ${resp.statusCode}');
      }
      final data = jsonDecode(resp.body) as Map<String, dynamic>;
      final id = data['job_id'] as String?;
      if (id == null || !_jobOwner.accept(generation, id)) {
        throw const FormatException('Missing Her POV job identity');
      }
      _pollFailures = 0;
      _poll?.cancel();
      _poll = Timer.periodic(const Duration(seconds: 2), (_) => _tick());
      await _tick();
    } catch (_) {
      if (!mounted || !_jobOwner.isCurrent(generation)) return;
      setState(() {
        _phase = _Phase.failed;
        _status = 'Could not reach her. Try again.';
      });
    }
  }

  // Caller-owned injected clients stay open. The screen can abort only its own
  // transport on disposal; no request cancels accepted work on the server.
  http.Client get _jobHttp =>
      widget.httpClient ?? (_ownedHttp ??= http.Client());

  Future<void> _tick() async {
    if (!mounted) return;
    final poll = _jobOwner.claimPoll();
    if (poll == null) return;
    try {
      final resp = await _jobHttp.get(
        Uri.parse('${widget.serverUrl}/api/memories/her-pov/${poll.jobId}'),
        headers: _headers,
      );
      if (!mounted || !_jobOwner.ownsPoll(poll)) return;
      if (resp.statusCode != 200) {
        // The job board is in-process, so a core restart makes a live job
        // 404 forever. Give up instead of polling for the life of the tab
        // with the button stuck disabled.
        if (++_pollFailures >= _maxPollFailures) {
          _poll?.cancel();
          _jobOwner.complete(poll);
          setState(() {
            _phase = _Phase.failed;
            _status = 'Lost track of that one. Ask me again.';
          });
        }
        return;
      }
      _pollFailures = 0;
      final data = jsonDecode(resp.body) as Map<String, dynamic>;
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
        _jobOwner.complete(poll);
        if (mapped == _Phase.done) {
          HapticFeedback.lightImpact();
          await _loadGallery();
        }
      }
    } catch (_) {
      // A transport/parse failure releases this poll; the existing bounded
      // status-error policy remains responsible for a lost server job.
    } finally {
      _jobOwner.release(poll);
    }
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
                    Color.lerp(
                      const Color(0xFF1A2740),
                      const Color(0xFF2A3A58),
                      _pulse.value,
                    )!,
                    GFL2Colors.background,
                  ],
                ),
              ),
              child: const SizedBox.expand(),
            ),
          ),
          SafeArea(child: wide ? _buildDesktop(pad) : _buildMobile(pad)),
        ],
      ),
    );
  }
}
