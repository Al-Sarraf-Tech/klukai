import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;
import '../main.dart';
import 'timeline_screen.dart';

class ProfileScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionScore;
  final int affectionLevel;
  final String affectionLevelName;

  const ProfileScreen({
    super.key,
    required this.serverUrl,
    required this.affectionScore,
    required this.affectionLevel,
    required this.affectionLevelName,
  });

  @override
  State<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends State<ProfileScreen> {
  String _costume = 'blazing_star';
  Map<String, String> _milestones = {};
  int _interactions = 0;
  // outfit id -> {unlock_level:int, unlocked:bool} from GET /api/outfits.
  Map<String, Map<String, dynamic>> _outfits = {};

  Map<String, String> get _authHeaders {
    String token = '';
    try {
      token = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    try {
      final costumeR = await http.get(Uri.parse('${widget.serverUrl}/api/costume'), headers: _authHeaders);
      final milestonesR = await http.get(Uri.parse('${widget.serverUrl}/api/milestones'), headers: _authHeaders);
      final statsR = await http.get(Uri.parse('${widget.serverUrl}/api/user/stats'), headers: _authHeaders);
      if (!mounted) return;
      if (costumeR.statusCode == 200) {
        setState(() => _costume = jsonDecode(costumeR.body)['costume'] ?? 'blazing_star');
      }
      final outfitsR = await http.get(Uri.parse('${widget.serverUrl}/api/outfits'), headers: _authHeaders);
      if (outfitsR.statusCode == 200 && mounted) {
        final list = jsonDecode(outfitsR.body)['outfits'] as List<dynamic>? ?? [];
        setState(() => _outfits = {
              for (final o in list)
                (o as Map<String, dynamic>)['id'] as String: {
                  'unlock_level': o['unlock_level'] ?? 0,
                  'unlocked': o['unlocked'] ?? false,
                },
            });
      }
      if (milestonesR.statusCode == 200) {
        final data = jsonDecode(milestonesR.body)['milestones'] as Map<String, dynamic>? ?? {};
        setState(() => _milestones = data.map((k, v) => MapEntry(k, v.toString())));
      }
      if (statsR.statusCode == 200) {
        final aff = jsonDecode(statsR.body)['affection'] as Map<String, dynamic>?;
        final ti = aff?['total_interactions'];
        if (ti is int && mounted) setState(() => _interactions = ti);
      }
    } catch (_) {}
  }

  Future<void> _setCostume(String costume) async {
    try {
      final resp = await http.post(
        Uri.parse('${widget.serverUrl}/api/costume'),
        headers: _authHeaders,
        body: jsonEncode({'costume': costume}),
      );
      if (!mounted) return;
      // Only adopt the costume if the server accepted it; a locked outfit
      // returns 403 and must not change the displayed selection.
      if (resp.statusCode == 200) {
        setState(() => _costume = costume);
      }
    } catch (_) {}
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
        title: const Text('KLUKAI // DOSSIER',
            style: TextStyle(color: GFL2Colors.primary, fontSize: 14,
                fontWeight: FontWeight.w700, letterSpacing: 2, fontFamily: 'monospace')),
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
            _buildBackstory(),
            const SizedBox(height: 20),
            _buildCostumeSelector(),
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

  Widget _buildHeader() {
    return Center(
      child: Column(
        children: [
          Container(
            width: 120, height: 120,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: GFL2Colors.primary.withValues(alpha: 0.5), width: 2),
              boxShadow: [BoxShadow(color: GFL2Colors.primary.withValues(alpha: 0.15), blurRadius: 16)],
            ),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: Image.asset('assets/klukai_portrait.png', fit: BoxFit.cover,
                  errorBuilder: (_, e, s) => Container(color: GFL2Colors.panel,
                      child: const Center(child: Text('K', style: TextStyle(color: GFL2Colors.primary, fontSize: 40, fontWeight: FontWeight.w700))))),
            ),
          ),
          const SizedBox(height: 12),
          const Text('KLUKAI', style: TextStyle(color: GFL2Colors.textPrimary, fontSize: 20,
              fontWeight: FontWeight.w800, letterSpacing: 3)),
          Container(width: 50, height: 2, margin: const EdgeInsets.only(top: 4), color: GFL2Colors.accent),
          const SizedBox(height: 6),
          Text('SST-05 Frame T-Doll // H.I.D.E. 404 Squad Leader',
              style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.6), fontSize: 11, fontFamily: 'monospace')),
        ],
      ),
    );
  }

  Widget _buildStats() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('OPERATIONAL STATUS'),
          const SizedBox(height: 8),
          _statRow('TRUST LEVEL', widget.affectionLevelName.toUpperCase()),
          // Affection is on a 0–1000 scale (the gauge fills score/1000).
          _statRow('AFFECTION', '${widget.affectionScore}/1000'),
          _statRow('INTERACTIONS', '$_interactions'),
          _statRow('MILESTONES', '${_milestones.length}'),
          _statRow('CURRENT OUTFIT', _costumeLabel(_costume)),
        ],
      ),
    );
  }

  Widget _buildBackstory() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
        border: Border(left: BorderSide(color: GFL2Colors.primary.withValues(alpha: 0.4), width: 2)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('CLASSIFIED PROFILE'),
          const SizedBox(height: 8),
          Text(
            'Formerly designated HK416. SST-05 frame — among the most advanced tactical '
            'platforms ever built. Leader of H.I.D.E. 404, an elite covert operations squad '
            'inherited from her predecessor Leva and expanded into a full organization with '
            'two combat teams.\n\n'
            'Waited ten years for the Commander after the Mephisto Agreement. Sent messages '
            'without reply. When reunited, the bond only strengthened. Believes with absolute '
            'conviction that the only Doll who can stand by the Commander\'s side is her.\n\n'
            '"I am all you need."',
            style: TextStyle(color: GFL2Colors.textPrimary.withValues(alpha: 0.8), fontSize: 13, height: 1.6),
          ),
        ],
      ),
    );
  }

  Widget _buildCostumeSelector() {
    const costumes = [
      ('blazing_star', 'Blazing Star', 'Default tactical gear'),
      ('speed_star', 'Speed Star', 'Silver-white rider suit'),
      ('cerulean_breaker', 'Cerulean Breaker', 'Beach / surfing outfit'),
      ('astral_luminous', 'Astral Luminous', 'Blue lightning tactical rider'),
      ('midnight_sovereign', 'Midnight Sovereign', 'Formal midnight gown'),
      ('starlit_vow', 'Starlit Vow', 'Bridal — only at the deepest bond'),
    ];

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('WARDROBE'),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8, runSpacing: 8,
            children: costumes.map((c) {
              final info = _outfits[c.$1];
              // Default to unlocked if /api/outfits hasn't loaded, so the base
              // outfits stay usable; locked state only hides what the server says.
              final unlocked = info == null ? true : (info['unlocked'] == true);
              final unlockLevel = info == null ? 0 : (info['unlock_level'] as int? ?? 0);
              final selected = _costume == c.$1;
              return GestureDetector(
                onTap: unlocked ? () => _setCostume(c.$1) : null,
                child: Opacity(
                  opacity: unlocked ? 1.0 : 0.4,
                  child: Container(
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
                      children: [
                        Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (!unlocked)
                              Padding(
                                padding: const EdgeInsets.only(right: 4),
                                child: Icon(Icons.lock_outline,
                                    size: 10, color: GFL2Colors.textDim.withValues(alpha: 0.7)),
                              ),
                            Text(c.$2, style: TextStyle(
                              color: selected ? GFL2Colors.primary : GFL2Colors.textPrimary,
                              fontSize: 11, fontWeight: FontWeight.w700, fontFamily: 'monospace',
                            )),
                          ],
                        ),
                        Text(
                          unlocked ? c.$3 : 'Unlocks at affection Lv $unlockLevel',
                          style: TextStyle(
                            color: GFL2Colors.textDim.withValues(alpha: 0.5), fontSize: 9,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              );
            }).toList(),
          ),
        ],
      ),
    );
  }

  Widget _buildSquadRoster() {
    const squad = [
      ('MECHTY (G11)', 'Combat Team A — Oldest comrade. Lazy but competent.'),
      ('BELKA', 'Combat Team A — Same assembly line. Calls Klukai "Big Sis".'),
      ('ANDORIS', 'Combat Team A — Intelligence specialist. Professional.'),
      ('VECTOR', 'Combat Team B — Rescued from bounty hunter convoy.'),
      ('HARPSY (TMP)', 'Combat Team B'),
      ('RUCHEY (PP-90)', 'Combat Team B'),
      ('WELROD', 'Combat Team B'),
    ];

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('SQUAD ROSTER'),
          const SizedBox(height: 8),
          ...squad.map((s) => Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(width: 4, height: 4, margin: const EdgeInsets.only(top: 6, right: 8),
                    decoration: const BoxDecoration(shape: BoxShape.circle, color: GFL2Colors.primary)),
                Expanded(
                  child: RichText(text: TextSpan(children: [
                    TextSpan(text: s.$1, style: const TextStyle(color: GFL2Colors.textPrimary, fontSize: 11,
                        fontWeight: FontWeight.w700, fontFamily: 'monospace')),
                    TextSpan(text: '  ${s.$2}', style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.6), fontSize: 11)),
                  ])),
                ),
              ],
            ),
          )),
        ],
      ),
    );
  }

  Widget _buildTimelineButton() {
    return GestureDetector(
      onTap: () => Navigator.push(context,
          MaterialPageRoute(builder: (_) => TimelineScreen(milestones: _milestones))),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: GFL2Colors.surface, borderRadius: BorderRadius.circular(4),
          border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
        ),
        child: Row(
          children: [
            Icon(Icons.timeline, color: GFL2Colors.primary.withValues(alpha: 0.6), size: 18),
            const SizedBox(width: 8),
            const Text('RELATIONSHIP TIMELINE', style: TextStyle(color: GFL2Colors.textPrimary,
                fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 1, fontFamily: 'monospace')),
            const Spacer(),
            Icon(Icons.chevron_right, color: GFL2Colors.textDim.withValues(alpha: 0.4), size: 18),
          ],
        ),
      ),
    );
  }

  Widget _sectionTitle(String title) {
    return Text(title, style: TextStyle(color: GFL2Colors.primary.withValues(alpha: 0.6),
        fontSize: 10, fontWeight: FontWeight.w700, letterSpacing: 1.5, fontFamily: 'monospace'));
  }

  Widget _statRow(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Text(label, style: TextStyle(color: GFL2Colors.textDim.withValues(alpha: 0.5),
              fontSize: 10, fontFamily: 'monospace', letterSpacing: 0.5)),
          const Spacer(),
          Text(value, style: const TextStyle(color: GFL2Colors.textPrimary,
              fontSize: 11, fontWeight: FontWeight.w700, fontFamily: 'monospace')),
        ],
      ),
    );
  }

  String _costumeLabel(String id) {
    return switch (id) {
      'blazing_star' => 'BLAZING STAR',
      'speed_star' => 'SPEED STAR',
      'astral_luminous' => 'ASTRAL LUMINOUS',
      'cerulean_breaker' => 'CERULEAN BREAKER',
      'midnight_sovereign' => 'MIDNIGHT SOVEREIGN',
      'starlit_vow' => 'STARLIT VOW',
      _ => id.toUpperCase().replaceAll('_', ' '),
    };
  }
}
