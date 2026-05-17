// SubscriptionScreen — pricing tier display + Stripe checkout integration.
//
// SACRED: never tells the user a cancel deletes their memories — because
// it doesn't. Free tier downgrade just revokes feature access; every
// memory remains.

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

class SubscriptionScreen extends StatefulWidget {
  final String serverUrl;
  final String? authToken;
  const SubscriptionScreen({super.key, required this.serverUrl, this.authToken});

  @override
  State<SubscriptionScreen> createState() => _SubscriptionScreenState();
}

class _SubscriptionScreenState extends State<SubscriptionScreen> {
  Map<String, dynamic>? _pricing;
  Map<String, dynamic>? _currentSub;
  Map<String, dynamic>? _usage;
  bool _loading = true;
  bool _annual = false;
  String? _checkoutInProgress;

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
        _pricing = jsonDecode(tiersResp.body);
        _currentSub = sub;
        _usage = usage;
        _loading = false;
      });
    } catch (e) {
      setState(() => _loading = false);
    }
  }

  Future<void> _checkout(String tier) async {
    if (widget.authToken == null) return;
    setState(() => _checkoutInProgress = tier);
    try {
      final resp = await http.post(
        Uri.parse('${widget.serverUrl}/api/billing/checkout'),
        headers: {
          'Authorization': 'Bearer ${widget.authToken}',
          'Content-Type': 'application/json',
        },
        body: jsonEncode({
          'tier': tier,
          'cadence': _annual ? 'annual' : 'monthly',
        }),
      );
      if (resp.statusCode == 200) {
        final body = jsonDecode(resp.body);
        final url = body['url'] as String?;
        if (url != null) {
          web.window.location.href = url;
          return;
        }
      } else if (resp.statusCode == 503) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text('Billing not configured yet. Coming soon.'),
            backgroundColor: Colors.orange,
          ));
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('Checkout failed: $e'),
        ));
      }
    } finally {
      setState(() => _checkoutInProgress = null);
    }
  }

  Future<void> _openPortal() async {
    if (widget.authToken == null) return;
    try {
      final resp = await http.post(
        Uri.parse('${widget.serverUrl}/api/billing/portal'),
        headers: {'Authorization': 'Bearer ${widget.authToken}'},
      );
      if (resp.statusCode == 200) {
        final body = jsonDecode(resp.body);
        web.window.location.href = body['url'];
      } else if (resp.statusCode == 400) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text('Subscribe to a paid tier first to access the portal.'),
          ));
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final currentTier = _currentSub?['tier'] as String? ?? 'free';
    return Scaffold(
      backgroundColor: const Color(0xFF0A0F1A),
      appBar: AppBar(
        title: const Text('Subscription', style: TextStyle(color: Colors.white)),
        backgroundColor: const Color(0xFF12182A),
        iconTheme: const IconThemeData(color: Colors.white),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _pricing == null
              ? const Center(child: Text('Failed to load pricing',
                  style: TextStyle(color: Colors.white)))
              : _buildContent(currentTier),
    );
  }

  Widget _buildContent(String currentTier) {
    final pricing = _pricing!['pricing'] as Map<String, dynamic>;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_currentSub != null) _buildCurrentBanner(currentTier),
          const SizedBox(height: 16),
          _buildCadenceToggle(),
          const SizedBox(height: 16),
          for (final tier in ['free', 'pro', 'elite'])
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: _buildTierCard(tier, pricing[tier], currentTier),
            ),
          const SizedBox(height: 24),
          if (currentTier != 'free') _buildPortalButton(),
          const SizedBox(height: 24),
          _buildSacredPromise(),
        ],
      ),
    );
  }

  Widget _buildCurrentBanner(String currentTier) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF1A2138),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: const Color(0xFF4A6FA5)),
      ),
      child: Row(
        children: [
          const Icon(Icons.verified_user, color: Color(0xFF7DD3FC)),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Current tier: ${currentTier.toUpperCase()}',
                  style: const TextStyle(color: Colors.white,
                    fontWeight: FontWeight.bold)),
                if (_usage != null) ..._buildUsageLines(),
              ],
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _buildUsageLines() {
    final counters = _usage?['counters'] as Map<String, dynamic>? ?? {};
    return counters.entries.map((e) {
      final used = e.value['used'] ?? 0;
      final limit = e.value['limit'];
      final label = e.key.replaceAll('_', ' ');
      final right = limit == null ? '$used / ∞' : '$used / $limit';
      return Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Text('$label: $right',
          style: const TextStyle(color: Colors.white70, fontSize: 12)),
      );
    }).toList();
  }

  Widget _buildCadenceToggle() {
    return Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: const Color(0xFF1A2138),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          _toggleButton('Monthly', !_annual),
          _toggleButton('Annual (save 16%)', _annual),
        ],
      ),
    );
  }

  Widget _toggleButton(String label, bool active) {
    return GestureDetector(
      onTap: () => setState(() => _annual = label.startsWith('Annual')),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        decoration: BoxDecoration(
          color: active ? const Color(0xFF4A6FA5) : Colors.transparent,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Text(label, style: TextStyle(
          color: active ? Colors.white : Colors.white60,
          fontWeight: active ? FontWeight.bold : FontWeight.normal,
        )),
      ),
    );
  }

  Widget _buildTierCard(String tier, dynamic info, String currentTier) {
    final name = info['name'] as String;
    final priceMonth = info['price_monthly_usd'] as int;
    final priceAnnual = info['price_annual_usd'] as int;
    final headline = info['headline'] as String;
    final bullets = (info['bullets'] as List).cast<String>();
    final isCurrent = tier == currentTier;
    final isUpgrade = (currentTier == 'free' && tier != 'free') ||
                     (currentTier == 'pro' && tier == 'elite');
    final price = _annual ? priceAnnual : priceMonth;
    final priceLabel = price == 0 ? 'Free'
        : (_annual ? '\$$price / yr' : '\$$price / mo');

    final isLoading = _checkoutInProgress == tier;

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFF12182A),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: isCurrent ? const Color(0xFF7DD3FC)
              : (tier == 'elite' ? const Color(0xFFFFD700) : Colors.transparent),
          width: 2,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(name, style: TextStyle(
                color: tier == 'elite' ? const Color(0xFFFFD700) : Colors.white,
                fontSize: 24, fontWeight: FontWeight.bold,
              )),
              Text(priceLabel,
                style: const TextStyle(color: Colors.white,
                  fontSize: 20, fontWeight: FontWeight.bold)),
            ],
          ),
          const SizedBox(height: 8),
          Text(headline, style: const TextStyle(color: Colors.white70)),
          const SizedBox(height: 12),
          for (final b in bullets)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                children: [
                  const Icon(Icons.check, color: Color(0xFF7DD3FC), size: 16),
                  const SizedBox(width: 8),
                  Expanded(child: Text(b,
                    style: const TextStyle(color: Colors.white))),
                ],
              ),
            ),
          const SizedBox(height: 16),
          if (isCurrent)
            Container(
              padding: const EdgeInsets.symmetric(vertical: 10),
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: const Color(0xFF1A2138),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Text('Your current tier',
                style: TextStyle(color: Colors.white60)),
            )
          else if (tier == 'free')
            const SizedBox.shrink()
          else
            SizedBox(
              width: double.infinity,
              child: ElevatedButton(
                onPressed: isLoading ? null : () => _checkout(tier),
                style: ElevatedButton.styleFrom(
                  backgroundColor: tier == 'elite'
                      ? const Color(0xFFFFD700) : const Color(0xFF4A6FA5),
                  foregroundColor: tier == 'elite' ? Colors.black : Colors.white,
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                child: isLoading
                    ? const SizedBox(
                        height: 18, width: 18,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(isUpgrade ? 'Upgrade to $name' : 'Switch to $name',
                        style: const TextStyle(fontWeight: FontWeight.bold)),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildPortalButton() {
    return OutlinedButton.icon(
      onPressed: _openPortal,
      icon: const Icon(Icons.credit_card, color: Colors.white70),
      label: const Text('Manage payment + billing history',
        style: TextStyle(color: Colors.white70)),
      style: OutlinedButton.styleFrom(
        side: const BorderSide(color: Color(0xFF4A6FA5)),
        padding: const EdgeInsets.symmetric(vertical: 12),
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
            'Your conversations, episodes, affection, and memory archive are '
            'sacred. Canceling a subscription downgrades your tier — it does '
            'NOT delete anything. Resubscribe and every memory is back.',
            style: TextStyle(color: Colors.white70, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
