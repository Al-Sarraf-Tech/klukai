// FeaturesScreen — read-only view of tier/feature set + usage counters.
//
// Personal mode: everyone is elite, all features on, no buttons to "Subscribe".
// Future: when monetization is activated, this screen can be extended back
// into a checkout flow. The data model + endpoints stay the same.

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

enum _StatusPhase { loading, ready, empty, error }

class SubscriptionScreen extends StatefulWidget {
  final String serverUrl;
  final String? authToken;
  final http.Client? client;
  final VoidCallback? onAuthExpired;
  const SubscriptionScreen({
    super.key,
    required this.serverUrl,
    this.authToken,
    this.client,
    this.onAuthExpired,
  });

  @override
  State<SubscriptionScreen> createState() => _SubscriptionScreenState();
}

class _SubscriptionScreenState extends State<SubscriptionScreen> {
  Map<String, dynamic>? _tiersInfo;
  Map<String, dynamic>? _currentSub;
  Map<String, dynamic>? _usage;
  _StatusPhase _phase = _StatusPhase.loading;
  bool _hasGood = false;
  late final http.Client _client = widget.client ?? http.Client();
  late final bool _ownClient = widget.client == null;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    if (_ownClient) _client.close();
    super.dispose();
  }

  String? get _verifiedTier {
    final tier = _currentSub?['tier'];
    if (tier is String && tier.isNotEmpty) return tier;
    return null;
  }

  bool _expired(int status) {
    if (status != 401) return false;
    widget.onAuthExpired?.call();
    return true;
  }

  Future<void> _load() async {
    if (_phase != _StatusPhase.loading && !_hasGood && mounted) {
      setState(() => _phase = _StatusPhase.loading);
    }
    try {
      final tiersResp = await _client.get(Uri.parse('${widget.serverUrl}/api/billing/tiers'));
      if (!mounted) return;
      if (_expired(tiersResp.statusCode)) {
        setState(() => _phase = _StatusPhase.error);
        return;
      }
      Map<String, dynamic>? sub;
      Map<String, dynamic>? usage;
      var failed = tiersResp.statusCode != 200;
      if (widget.authToken != null) {
        final subResp = await _client.get(
          Uri.parse('${widget.serverUrl}/api/billing/subscription'),
          headers: {'Authorization': 'Bearer ${widget.authToken}'},
        );
        if (!mounted) return;
        if (_expired(subResp.statusCode)) {
          setState(() => _phase = _StatusPhase.error);
          return;
        }
        if (subResp.statusCode == 200) {
          final decoded = jsonDecode(subResp.body);
          if (decoded is Map<String, dynamic>) sub = decoded;
        } else if (subResp.statusCode != 404) {
          failed = true;
        }
        final usageResp = await _client.get(
          Uri.parse('${widget.serverUrl}/api/billing/usage'),
          headers: {'Authorization': 'Bearer ${widget.authToken}'},
        );
        if (!mounted) return;
        if (_expired(usageResp.statusCode)) {
          setState(() => _phase = _StatusPhase.error);
          return;
        }
        if (usageResp.statusCode == 200) {
          final decoded = jsonDecode(usageResp.body);
          if (decoded is Map<String, dynamic>) usage = decoded;
        } else if (usageResp.statusCode != 404) {
          failed = true;
        }
      }
      if (!mounted) return;
      if (failed) {
        setState(() => _phase = _StatusPhase.error);
        return;
      }
      final decodedTiers = jsonDecode(tiersResp.body);
      setState(() {
        if (decodedTiers is Map<String, dynamic>) _tiersInfo = decodedTiers;
        if (sub != null) {
          _currentSub = sub;
        } else if (widget.authToken != null) {
          _currentSub = null;
        }
        if (usage != null) _usage = usage;
        final features = _tiersInfo?['features'];
        final hasFeatures = features is Map && features.isNotEmpty;
        if (_verifiedTier == null && !hasFeatures && _usage == null) {
          _phase = _StatusPhase.empty;
          _hasGood = false;
        } else {
          _phase = _StatusPhase.ready;
          _hasGood = true;
        }
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _phase = _StatusPhase.error);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0A0F1A),
      appBar: AppBar(
        title: const Text('Status', style: TextStyle(color: Colors.white)),
        backgroundColor: const Color(0xFF12182A),
        iconTheme: const IconThemeData(color: Colors.white),
      ),
      body: _body(),
    );
  }

  Widget _body() {
    if (_phase == _StatusPhase.loading && !_hasGood) {
      return const Center(
        child: CircularProgressIndicator(key: Key('subscription-loading')),
      );
    }
    if (_phase == _StatusPhase.empty) {
      return _statusNotice(
        key: const Key('subscription-empty'),
        message: 'No status on file',
      );
    }
    if (_phase == _StatusPhase.error && !_hasGood) {
      return _statusNotice(
        key: const Key('subscription-error'),
        message: 'Status unavailable',
        retry: true,
      );
    }
    return Column(
      children: [
        if (_phase == _StatusPhase.error)
          const Padding(
            key: Key('subscription-error'),
            padding: EdgeInsets.fromLTRB(16, 16, 16, 0),
            child: Text(
              'Status unavailable',
              style: TextStyle(color: Colors.white, fontSize: 16),
            ),
          ),
        Expanded(child: _buildContent(_verifiedTier)),
      ],
    );
  }

  Widget _statusNotice({
    required Key key,
    required String message,
    bool retry = false,
  }) {
    return Center(
      child: Column(
        key: key,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(message, style: const TextStyle(color: Colors.white, fontSize: 16)),
          if (retry)
            TextButton(
              key: const Key('subscription-retry'),
              onPressed: _load,
              child: const Text('Retry'),
            ),
        ],
      ),
    );
  }

  Widget _buildContent(String? currentTier) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              key: const Key('subscription-retry'),
              onPressed: _load,
              child: const Text('Retry'),
            ),
          ),
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

  Widget _buildPersonalModeBanner(String? currentTier) {
    if (currentTier == null) {
      return const Text(
        'No verified status',
        key: Key('subscription-unverified'),
        style: TextStyle(color: Colors.white70, fontSize: 14),
      );
    }
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
