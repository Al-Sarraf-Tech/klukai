import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../models/dossier.dart';
import '../services/local_prefs.dart';
import '../services/session_auth.dart';
import '../services/voice_settings_service.dart';
import '../theme/hud.dart';
import '../widgets/hud_parts.dart';
import 'subscription_screen.dart';
import 'timeline_screen.dart';

/// The bond readout from GET /api/user/stats.
class BondStats {
  const BondStats({
    this.userId = '',
    this.daysActive,
    this.streak,
    this.memoriesKept,
  });

  final String userId;

  /// Distinct days he has talked to her.
  final int? daysActive;

  /// Consecutive days, from her affection state.
  final int? streak;

  /// Memories she has kept.
  final int? memoriesKept;

  factory BondStats.fromJson(Map<String, dynamic> json) {
    int? asInt(Object? v) => v is int ? v : (v is num ? v.toInt() : null);
    final aff = json['affection'];
    final mem = json['memories'];
    return BondStats(
      userId: json['user_id'] is String ? json['user_id'] as String : '',
      daysActive: asInt(json['days_active']),
      streak: aff is Map ? asInt(aff['consecutive_days']) : null,
      memoriesKept: mem is Map ? asInt(mem['kept']) : null,
    );
  }
}

/// Me tab of the Tactical HUD: the bond, her voice language, stage settings,
/// links to the timeline and subscription, her dossier, and SIGN OUT. An
/// embedded tab body; the shell owns the Scaffold and the tab bar.
class HudMeScreen extends StatefulWidget {
  const HudMeScreen({
    super.key,
    required this.serverUrl,
    required this.affectionScore,
    required this.affectionLevel,
    required this.affectionLevelName,
    required this.onLogout,
    this.client,
    this.voiceSettings,
    this.prefs,
    this.onAuthExpired,
  });

  final String serverUrl;

  /// 0–1000 (the bond bar fills score / 1000).
  final int affectionScore;
  final int affectionLevel;
  final String affectionLevelName;

  /// SIGN OUT, after he confirms. The shell revokes the token and the
  /// service worker, then returns to login.
  final VoidCallback onLogout;

  /// Client for /api/user/stats, /api/milestones and the subscription page.
  final http.Client? client;

  /// GET/PUT /api/voice/settings; defaults to one on [client].
  final VoiceSettingsService? voiceSettings;

  /// localStorage seam for the stage settings; defaults to the browser's.
  final LocalPrefs? prefs;

  /// 401: the token is dead; the shell's logout/reauth path.
  final VoidCallback? onAuthExpired;

  @override
  State<HudMeScreen> createState() => _HudMeScreenState();
}

class _HudMeScreenState extends State<HudMeScreen> {
  late final http.Client _client = widget.client ?? http.Client();
  late final VoiceSettingsService _voice =
      widget.voiceSettings ??
      VoiceSettingsService(serverUrl: widget.serverUrl, client: _client);
  late final LocalPrefs _prefs = widget.prefs ?? defaultLocalPrefs();

  BondStats? _stats;
  Map<String, String> _milestones = const {};
  VoiceLanguage _language = VoiceLanguage.en;
  bool _savingLanguage = false;
  late bool _open3d = _prefs.read(kOpenIn3dPrefKey) == 'true';
  late bool _music = readStageMusic(_prefs);
  bool _authBounced = false;

  Map<String, String> get _headers => {
    'Authorization': 'Bearer ${readAuthToken()}',
    'Content-Type': 'application/json',
  };

  @override
  void initState() {
    super.initState();
    _loadStats();
    _loadMilestones();
    _loadVoice();
  }

  void _authExpired() {
    if (_authBounced || !mounted) return;
    _authBounced = true;
    widget.onAuthExpired?.call();
  }

  Future<void> _loadStats() async {
    try {
      final r = await _client.get(
        Uri.parse('${widget.serverUrl}/api/user/stats'),
        headers: _headers,
      );
      if (r.statusCode == 401) return _authExpired();
      if (r.statusCode != 200) return;
      final body = jsonDecode(utf8.decode(r.bodyBytes));
      if (body is Map<String, dynamic> && mounted) {
        setState(() => _stats = BondStats.fromJson(body));
      }
    } catch (_) {}
  }

  Future<void> _loadMilestones() async {
    try {
      final r = await _client.get(
        Uri.parse('${widget.serverUrl}/api/milestones'),
        headers: _headers,
      );
      if (r.statusCode == 401) return _authExpired();
      if (r.statusCode != 200) return;
      final body = jsonDecode(utf8.decode(r.bodyBytes));
      final data = body is Map ? body['milestones'] : null;
      if (data is Map && mounted) {
        setState(
          () => _milestones = data.map(
            (k, v) => MapEntry(k.toString(), v.toString()),
          ),
        );
      }
    } catch (_) {}
  }

  Future<void> _loadVoice() async {
    try {
      final lang = await _voice.load();
      if (mounted && !_savingLanguage) setState(() => _language = lang);
    } on VoiceSettingsAuthExpired {
      _authExpired();
    } catch (_) {
      // No endpoint, or the link is down: English, as the server defaults.
    }
  }

  Future<void> _setLanguage(VoiceLanguage next) async {
    if (next == _language || _savingLanguage) return;
    final previous = _language;
    setState(() {
      _language = next;
      _savingLanguage = true;
    });
    try {
      final saved = await _voice.save(next);
      if (mounted) setState(() => _language = saved);
    } on VoiceSettingsAuthExpired {
      if (mounted) setState(() => _language = previous);
      _authExpired();
    } catch (_) {
      if (!mounted) return;
      setState(() => _language = previous);
      showHudSnack(
        context,
        "Couldn't save her voice language. Try again.",
        key: const Key('me-voice-error'),
      );
    } finally {
      if (mounted) setState(() => _savingLanguage = false);
    }
  }

  void _toggle3d() {
    final next = !_open3d;
    _prefs.write(kOpenIn3dPrefKey, next ? 'true' : 'false');
    setState(() => _open3d = next);
  }

  void _toggleMusic() {
    final next = !_music;
    writeStageMusic(_prefs, next);
    setState(() => _music = next);
  }

  void _openTimeline() => Navigator.of(context).push(
    MaterialPageRoute(builder: (_) => TimelineScreen(milestones: _milestones)),
  );

  void _openSubscription() => Navigator.of(context).push(
    MaterialPageRoute(
      builder: (_) => SubscriptionScreen(
        serverUrl: widget.serverUrl,
        authToken: readAuthToken(),
        client: widget.client,
        onAuthExpired: widget.onAuthExpired,
      ),
    ),
  );

  Future<void> _signOut() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: HudColors.ground,
        shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(0.45))),
        title: Text(
          'Sign out?',
          style: HudText.body(size: 18, weight: FontWeight.w700),
        ),
        content: Text(
          'You will need your password to come back to her.',
          style: HudText.body(size: 14, color: HudColors.textDim),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            style: TextButton.styleFrom(minimumSize: const Size(64, 44)),
            child: Text(
              'CANCEL',
              style: HudText.label(size: 11, color: HudColors.text),
            ),
          ),
          TextButton(
            key: const Key('me-signout-confirm'),
            onPressed: () => Navigator.of(ctx).pop(true),
            style: TextButton.styleFrom(minimumSize: const Size(64, 44)),
            child: Text(
              'SIGN OUT',
              style: HudText.label(size: 11, color: kHudDanger),
            ),
          ),
        ],
      ),
    );
    if (ok == true) widget.onLogout();
  }

  String _n(int? v) => v == null ? '—' : '$v';

  @override
  Widget build(BuildContext context) {
    final stats = _stats;
    final progress = (widget.affectionScore / 1000).clamp(0.0, 1.0);
    return HudBackground(
      child: ListView(
        key: const Key('me-scroll'),
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
        children: [
          HudScreenHeader(
            kicker: 'Commander // Profile',
            trailing: (stats?.userId.isNotEmpty ?? false)
                ? stats!.userId
                : null,
            trailingKey: const Key('me-user'),
            trailingColor: HudColors.cyan,
            title: 'Me',
            subtitle: 'Settings',
          ),
          const SizedBox(height: 12),
          HudPanel(
            key: const Key('me-bond'),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                HudSplitRow(
                  rightShare: 0.55,
                  left: const HudLabel('Bond with Klukai'),
                  right: Text(
                    'LV ${widget.affectionLevel} · ${widget.affectionLevelName}'
                        .toUpperCase(),
                    key: const Key('me-level'),
                    textAlign: TextAlign.end,
                    style: HudText.label(color: HudColors.violet),
                  ),
                ),
                const SizedBox(height: 8),
                Semantics(
                  label: 'Affection',
                  value: '${widget.affectionScore} of 1000',
                  child: Container(
                    height: 8,
                    color: HudColors.pipOff,
                    alignment: Alignment.centerLeft,
                    child: FractionallySizedBox(
                      key: const Key('me-bond-bar'),
                      widthFactor: progress,
                      heightFactor: 1,
                      child: Container(
                        decoration: const BoxDecoration(
                          color: HudColors.violet,
                          boxShadow: [
                            BoxShadow(color: HudColors.violet, blurRadius: 8),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    Expanded(
                      child: _Stat(
                        key: const Key('me-days'),
                        label: 'Days',
                        value: _n(stats?.daysActive),
                      ),
                    ),
                    Expanded(
                      child: _Stat(
                        key: const Key('me-streak'),
                        label: 'Streak',
                        value: _n(stats?.streak),
                      ),
                    ),
                    Expanded(
                      child: _Stat(
                        key: const Key('me-memories'),
                        label: 'Memories',
                        value: _n(stats?.memoriesKept),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 6),
          const HudSectionLabel('Voice'),
          HudSettingRow(
            key: const Key('me-voice-language'),
            title: 'Her voice language',
            subtitle: 'Plays only when you tap VOICE',
            subtitleWidget: const HudInlineText([
              'Plays only when you tap ',
              Icons.play_arrow,
              ' VOICE',
            ]),
            trailing: HudSegmented<VoiceLanguage>(
              key: const Key('me-voice-segmented'),
              semanticLabel: 'Her voice language',
              options: {for (final l in VoiceLanguage.values) l: l.label},
              value: _language,
              onChanged: _savingLanguage ? null : _setLanguage,
            ),
          ),
          const SizedBox(height: 10),
          const HudSettingRow(
            key: Key('me-voice-input'),
            title: 'Voice input',
            subtitle: 'Mic is off for now · type to her',
            toggled: false,
            enabled: false,
            trailing: HudToggle(value: false, enabled: false),
          ),
          const SizedBox(height: 6),
          const HudSectionLabel('Stage'),
          HudSettingRow(
            key: const Key('me-open-3d'),
            title: 'Open in 3D by default',
            subtitle: 'Chat and 3D flip stays one tap away',
            subtitleWidget: const HudInlineText([
              'Chat ',
              Icons.swap_horiz,
              ' 3D flip stays one tap away',
            ]),
            toggled: _open3d,
            onTap: _toggle3d,
            trailing: HudToggle(value: _open3d),
          ),
          const SizedBox(height: 10),
          HudSettingRow(
            key: const Key('me-stage-music'),
            title: 'Stage music',
            subtitle: 'Ambient track in 3D',
            toggled: _music,
            onTap: _toggleMusic,
            trailing: HudToggle(value: _music),
          ),
          const SizedBox(height: 6),
          const HudSectionLabel('Records'),
          HudSettingRow(
            key: const Key('me-timeline'),
            title: 'Relationship timeline',
            subtitle: _milestones.isEmpty
                ? 'Your milestones together'
                : _milestones.length == 1
                ? '1 milestone together'
                : '${_milestones.length} milestones together',
            onTap: _openTimeline,
            trailing: const Icon(Icons.chevron_right, color: HudColors.textDim),
          ),
          const SizedBox(height: 10),
          HudSettingRow(
            key: const Key('me-subscription'),
            title: 'Subscription',
            subtitle: 'Plan and usage',
            onTap: _openSubscription,
            trailing: const Icon(Icons.chevron_right, color: HudColors.textDim),
          ),
          const SizedBox(height: 6),
          const HudSectionLabel('Dossier'),
          HudCollapsible(
            key: const Key('me-dossier-profile'),
            title: 'Classified profile',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const HudLabel(kKlukaiDesignation, color: HudColors.cyan),
                const SizedBox(height: 8),
                Text(
                  kKlukaiBackstory,
                  style: HudText.body(size: 14.5).copyWith(height: 1.5),
                ),
              ],
            ),
          ),
          const SizedBox(height: 10),
          HudCollapsible(
            key: const Key('me-dossier-squad'),
            title: 'Squad roster · H.I.D.E. 404',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                for (final (name, note) in kSquadRoster)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          name,
                          style: HudText.label(
                            size: 11,
                            color: HudColors.text,
                            weight: FontWeight.w600,
                          ),
                        ),
                        Text(
                          note,
                          style: HudText.body(
                            size: 13.5,
                            color: HudColors.textDim,
                          ),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          HudActionButton(
            key: const Key('me-signout'),
            label: 'Sign out',
            danger: true,
            onPressed: _signOut,
          ),
        ],
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({super.key, required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Semantics(
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
            size: 17,
            color: HudColors.text,
            weight: FontWeight.w600,
          ).copyWith(letterSpacing: 0.5),
        ),
      ],
    ),
  );
}
