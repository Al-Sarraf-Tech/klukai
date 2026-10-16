import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../models/her_day.dart';
import '../services/session_auth.dart';
import '../services/wardrobe_service.dart';
import '../theme/gfl2_colors.dart';
import '../widgets/her_day_widgets.dart';
import '../widgets/wardrobe_widgets.dart';
import 'timeline_screen.dart';

part 'profile_view.dart';

enum _RecordPhase { loading, ready, empty, error }

class ProfileScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionScore;
  final int affectionLevel;
  final String affectionLevelName;

  /// Injectable for tests; defaults to a real [WardrobeService].
  final WardrobeService? wardrobeService;

  /// Client for the milestones / stats fetches. Injectable for tests.
  final http.Client? client;

  /// 401 keeps the chat logout/reauth path. The screen does not import the browser.
  final VoidCallback? onAuthExpired;

  const ProfileScreen({
    super.key,
    required this.serverUrl,
    required this.affectionScore,
    required this.affectionLevel,
    required this.affectionLevelName,
    this.wardrobeService,
    this.client,
    this.onAuthExpired,
  });

  @override
  State<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends State<ProfileScreen> {
  late final WardrobeService _wardrobeService;
  late final http.Client _client;
  Map<String, String> _milestones = {};
  int _interactions = 0;
  _RecordPhase _records = _RecordPhase.loading;
  bool _recordsGood = false;

  // Today's Outfit / Her Day. Each loads independently and fails soft: a
  // section whose fetch failed simply doesn't render.
  Wardrobe? _wardrobe;
  HerDay? _herDay;
  List<WardrobeLogEntry> _log = const [];
  bool _settingCostume = false;

  Map<String, String> get _authHeaders => {
    'Authorization': 'Bearer ${readAuthToken()}',
    'Content-Type': 'application/json',
  };

  @override
  void initState() {
    super.initState();
    _client = widget.client ?? http.Client();
    _wardrobeService =
        widget.wardrobeService ?? WardrobeService(serverUrl: widget.serverUrl);
    _loadData();
    _loadWardrobe();
  }

  Future<void> _loadData() async {
    if (_records != _RecordPhase.loading && !_recordsGood && mounted) {
      setState(() => _records = _RecordPhase.loading);
    }
    try {
      final milestonesR = await _client.get(
        Uri.parse('${widget.serverUrl}/api/milestones'),
        headers: _authHeaders,
      );
      final statsR = await _client.get(
        Uri.parse('${widget.serverUrl}/api/user/stats'),
        headers: _authHeaders,
      );
      if (!mounted) return;
      if (milestonesR.statusCode == 401 || statsR.statusCode == 401) {
        widget.onAuthExpired?.call();
        setState(() => _records = _RecordPhase.error);
        return;
      }
      final milestonesOk = milestonesR.statusCode == 200;
      final statsOk = statsR.statusCode == 200;
      final milestonesMissing = milestonesR.statusCode == 404;
      final statsMissing = statsR.statusCode == 404;
      if ((!milestonesOk && !milestonesMissing) ||
          (!statsOk && !statsMissing)) {
        setState(() => _records = _RecordPhase.error);
        return;
      }
      var milestones = _milestones;
      var interactions = _interactions;
      if (milestonesOk) {
        final body = jsonDecode(milestonesR.body);
        final data = body is Map<String, dynamic>
            ? body['milestones'] as Map<String, dynamic>? ?? {}
            : <String, dynamic>{};
        milestones = data.map((k, v) => MapEntry(k, v.toString()));
      }
      if (statsOk) {
        final body = jsonDecode(statsR.body);
        final aff = body is Map<String, dynamic>
            ? body['affection'] as Map<String, dynamic>?
            : null;
        final ti = aff?['total_interactions'];
        if (ti is int) interactions = ti;
      }
      setState(() {
        _milestones = milestones;
        _interactions = interactions;
        final empty = _milestones.isEmpty && _interactions == 0;
        _records = empty ? _RecordPhase.empty : _RecordPhase.ready;
        _recordsGood = _records == _RecordPhase.ready;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _records = _RecordPhase.error);
    }
  }

  Future<void> _loadWardrobe() async {
    await Future.wait([
      _guard(() async {
        final w = await _wardrobeService.fetchOutfits();
        if (mounted) setState(() => _wardrobe = w);
      }),
      _guard(() async {
        final d = await _wardrobeService.fetchHerDay();
        if (mounted) setState(() => _herDay = d);
      }),
      _guard(() async {
        final log = await _wardrobeService.fetchLog(days: 30);
        if (mounted) setState(() => _log = log);
      }),
    ]);
  }

  Future<void> _guard(Future<void> Function() load) async {
    try {
      await load();
    } catch (_) {}
  }

  Future<void> _setCostume(WardrobeItem item) async {
    if (_settingCostume) return;
    _settingCostume = true;
    try {
      await _wardrobeService.setCostume(item.id);
      if (!mounted) return;
      await _loadWardrobe();
    } on WardrobeServiceException catch (e) {
      _say(
        e.isLocked
            ? e.refusalLine
            : e.isUnknown
            ? "That isn't in my wardrobe, Commander."
            : 'Comms disrupted. Try again.',
      );
    } catch (_) {
      _say('Comms disrupted. Try again.');
    } finally {
      _settingCostume = false;
    }
  }

  void _say(String line) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(
          key: const Key('wardrobe-snackbar'),
          duration: const Duration(seconds: 2),
          backgroundColor: GFL2Colors.panel,
          content: Text(
            line,
            style: const TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 12,
              fontFamily: 'monospace',
            ),
          ),
        ),
      );
  }

  String get _todayName {
    final name = _wardrobe?.today?.name ?? _herDay?.outfit?.name;
    return name == null || name.isEmpty ? '—' : name.toUpperCase();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GFL2Colors.background,
      appBar: AppBar(
        backgroundColor: GFL2Colors.surface,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: GFL2Colors.textPrimary),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text(
          'KLUKAI // DOSSIER',
          style: TextStyle(
            color: GFL2Colors.primary,
            fontSize: 14,
            fontWeight: FontWeight.w700,
            letterSpacing: 2,
            fontFamily: 'monospace',
          ),
        ),
        centerTitle: true,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Portrait + stats
            _buildHeader(),
            const SizedBox(height: 20),
            _buildStats(),
            const SizedBox(height: 20),
            if (_herDay != null && _herDay!.schedule.isNotEmpty) ...[
              _buildTodaySchedule(),
              const SizedBox(height: 20),
            ],
            if (_wardrobe != null) ...[
              _buildCostumeSelector(),
              const SizedBox(height: 20),
            ],
            if (_log.isNotEmpty) ...[
              _buildWornLog(),
              const SizedBox(height: 20),
            ],
            _buildBackstory(),
            const SizedBox(height: 20),
            _buildSquadRoster(),
            const SizedBox(height: 20),
            _buildTimelineButton(),
            const SizedBox(height: 40),
          ],
        ),
      ),
    );
  }
}
