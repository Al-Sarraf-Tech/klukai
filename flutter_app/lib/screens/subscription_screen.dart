// FeaturesScreen — read-only view of tier/feature set + usage counters.
//
// Personal mode: everyone is elite, all features on, no buttons to "Subscribe".
// Future: when monetization is activated, this screen can be extended back
// into a checkout flow. The data model + endpoints stay the same.

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

class SubscriptionScreen extends StatefulWidget {
  final String serverUrl;
  final String? authToken;
  const SubscriptionScreen({super.key, required this.serverUrl, this.authToken});

  @override
  State<SubscriptionScreen> createState() => _SubscriptionScreenState();
}

class _SubscriptionScreenState extends State<SubscriptionScreen> {
  Map<String, dynamic>? _tiersInfo;
  Map<String, dynamic>? _currentSub;
  Map<String, dynamic>? _usage;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final tiersResp = await http.get(Uri.parse('${widget.serverUrl}/api/billing/tiers'));
      Map<String, dynamic>? sub;
      Map<String, dynamic>? usage;
      if (widget.authToken != null) {
        final subResp = await http.get(
          Uri.parse('${widget.serverUrl}/api/billing/subscription'),
          headers: {'Authorization': 'Bearer ${widget.authToken}'},
        );
        if (subResp.statusCode == 200) {
          sub = jsonDecode(subResp.body);
        }
        final usageResp = await http.get(
          Uri.parse('${widget.serverUrl}/api/billing/usage'),
          headers: {'Authorization': 'Bearer ${widget.authToken}'},
        );
        if (usageResp.statusCode == 200) {
          usage = jsonDecode(usageResp.body);
        }
      }
      setState(() {
        _tiersInfo = jsonDecode(tiersResp.body);
        _currentSub = sub;
        _usage = usage;
        _loading = false;
      });
    } catch (_) {
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final currentTier = _currentSub?['tier'] as String? ?? 'elite';
    return Scaffold(
      backgroundColor: const Color(0xFF0A0F1A),
      appBar: AppBar(
        title: const Text('Status', style: TextStyle(color: Colors.white)),
        backgroundColor: const Color(0xFF12182A),
        iconTheme: const IconThemeData(color: Colors.white),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _buildContent(currentTier),
    );
  }

  Widget _buildContent(String currentTier) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _buildPersonalModeBanner(currentTier),
          const SizedBox(height: 16),
          if (_usage != null) ..._buildUsageBlock(),
          const SizedBox(height: 16),
          _buildFeatureBlock(),
          const SizedBox(height: 16),
          _buildSacredPromise(),
        ],
      ),
    );
  }

  Widget _buildPersonalModeBanner(String currentTier) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [Color(0xFF1A2138), Color(0xFF12182A)],
        ),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFFFD700), width: 1.5),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.workspace_premium, color: Color(0xFFFFD700), size: 28),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(currentTier.toUpperCase(),
                      style: const TextStyle(color: Color(0xFFFFD700),
                        fontSize: 20, fontWeight: FontWeight.bold,
                        letterSpacing: 1.2)),
                    const Text('Personal mode — all features unlocked',
                      style: TextStyle(color: Colors.white70, fontSize: 12)),
                  ],
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  List<Widget> _buildUsageBlock() {
    final counters = (_usage?['counters'] as Map<String, dynamic>?) ?? {};
    if (counters.isEmpty) return [];
    return [
      const Padding(
        padding: EdgeInsets.only(left: 4, bottom: 8),
        child: Text('Usage today',
          style: TextStyle(color: Colors.white,
            fontSize: 16, fontWeight: FontWeight.bold)),
      ),
      Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: const Color(0xFF12182A),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: const Color(0xFF1A2138)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: counters.entries.map((e) {
            final used = (e.value['used'] ?? 0) as num;
            final limit = e.value['limit'];
            final label = _humanize(e.key);
            final right = limit == null ? '$used' : '$used / $limit';
            final progress = limit == null ? null : (used / (limit as num)).clamp(0, 1).toDouble();
            return Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Text(label, style: const TextStyle(color: Colors.white70)),
                      Text(right, style: const TextStyle(color: Colors.white)),
                    ],
                  ),
                  if (progress != null) Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: LinearProgressIndicator(
                      value: progress,
                      backgroundColor: const Color(0xFF1A2138),
                      valueColor: AlwaysStoppedAnimation(
                        progress > 0.8 ? const Color(0xFFFF6B6B) : const Color(0xFF7DD3FC),
                      ),
                      minHeight: 4,
                    ),
                  ),
                ],
              ),
            );
          }).toList(),
        ),
      ),
    ];
  }

  String _humanize(String key) {
    return key.replaceAll('_', ' ')
      .replaceFirstMapped(RegExp(r'^[a-z]'), (m) => m.group(0)!.toUpperCase());
  }

  Widget _buildFeatureBlock() {
    final features = (_tiersInfo?['features']?['elite'] as Map<String, dynamic>?) ?? {};
    final enabled = <String>[];
    features.forEach((k, v) {
      if (v == true) enabled.add(_humanize(k.replaceAll('_enabled', '').replaceAll('_per_day', '')));
      if (v is num) enabled.add('${_humanize(k)}: ${v == 0 ? "off" : v}');
      if (v == null) enabled.add('${_humanize(k.replaceAll('_per_day', ''))}: unlimited');
    });
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFF12182A),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: const Color(0xFF1A2138)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Features active',
            style: TextStyle(color: Colors.white,
              fontSize: 16, fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          for (final feat in enabled)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Row(
                children: [
                  const Icon(Icons.check, color: Color(0xFF7DD3FC), size: 16),
                  const SizedBox(width: 8),
                  Expanded(child: Text(feat,
                    style: const TextStyle(color: Colors.white))),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildSacredPromise() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF12182A),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: const Color(0xFF4A6FA5)),
      ),
      child: const Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.lock, color: Color(0xFFFFD700), size: 18),
              SizedBox(width: 8),
              Text('Memory promise',
                style: TextStyle(color: Color(0xFFFFD700),
                  fontWeight: FontWeight.bold)),
            ],
          ),
          SizedBox(height: 6),
          Text(
            'Your conversations, episodes, affection, memories, dreams, and '
            'photo archive are sacred. They are preserved forever — never '
            'deleted automatically. Always yours.',
            style: TextStyle(color: Colors.white70, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
