import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../main.dart' show GFL2Colors;
import '../models/thread.dart';
import '../services/thread_service.dart';

/// The Thread — the messages she sent every night at 0200 during the ten
/// years he couldn't answer. Opening it delivers the read receipts, ten years
/// late.
class ThreadScreen extends StatefulWidget {
  final String serverUrl;
  final ThreadService? threadService;
  final Duration markReadDelay;

  const ThreadScreen({
    super.key,
    required this.serverUrl,
    this.threadService,
    this.markReadDelay = const Duration(milliseconds: 1400),
  });

  @override
  State<ThreadScreen> createState() => _ThreadScreenState();
}

class _ThreadScreenState extends State<ThreadScreen> {
  static const _months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];

  late final ThreadService _service;
  ThreadView? _view;
  bool _loading = true;
  bool _failed = false;
  Timer? _markReadTimer;

  @override
  void initState() {
    super.initState();
    _service = widget.threadService ?? ThreadService(serverUrl: widget.serverUrl);
    _load();
  }

  @override
  void dispose() {
    _markReadTimer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _failed = false;
    });
    try {
      final view = await _service.fetchThread();
      if (!mounted) return;
      setState(() {
        _view = view;
        _loading = false;
      });
      final unread = view.entries.where((e) => !e.isRead).map((e) => e.stamp).toList();
      if (!view.sealed && unread.isNotEmpty) {
        _markReadTimer = Timer(widget.markReadDelay, () => _markRead(unread));
      }
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _failed = true;
      });
    }
  }

  Future<void> _markRead(List<String> stamps) async {
    try {
      await _service.markRead(stamps);
    } catch (_) {
      return; // receipts stay "Delivered"; they'll go through next time
    }
    if (!mounted || _view == null) return;
    final now = DateTime.now();
    final view = _view!;
    HapticFeedback.lightImpact();
    setState(() {
      _view = ThreadView(
        sealed: view.sealed,
        message: view.message,
        entries: [
          for (final e in view.entries)
            stamps.contains(e.stamp) ? e.markedRead(now) : e,
        ],
        heldBack: view.heldBack,
        reply: view.reply,
      );
    });
  }

  String _receipt(ThreadEntry e) {
    final at = e.readAt;
    if (at == null) return 'Delivered';
    if (DateTime.now().difference(at).inMinutes < 1) return 'Read · just now';
    return 'Read · ${_months[at.month - 1]} ${at.day}, ${at.year}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GFL2Colors.background,
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: RadialGradient(
            center: Alignment(0, -1.1),
            radius: 1.3,
            colors: [Color(0xFF1C2638), GFL2Colors.background],
          ),
        ),
        child: SafeArea(
          child: Column(
            children: [
              _topBar(),
              Expanded(child: _body()),
            ],
          ),
        ),
      ),
    );
  }

  Widget _topBar() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 0, 16, 0),
      child: Row(
        children: [
          IconButton(
            tooltip: 'Back',
            onPressed: () => Navigator.pop(context),
            icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 18),
            color: GFL2Colors.textPrimary,
          ),
          Text(
            'THE THREAD',
            style: TextStyle(
              color: GFL2Colors.primary.withValues(alpha: 0.95),
              letterSpacing: 3.5,
              fontSize: 12,
              fontWeight: FontWeight.w700,
            ),
          ),
          const Spacer(),
          const Text(
            '2065 — 2074 · 0200',
            style: TextStyle(
              color: GFL2Colors.textDim,
              fontSize: 10,
              fontFamily: 'monospace',
              letterSpacing: 1,
            ),
          ),
        ],
      ),
    );
  }

  Widget _body() {
    if (_loading) {
      return const Center(
        child: CircularProgressIndicator(color: GFL2Colors.primary, strokeWidth: 2),
      );
    }
    final view = _view;
    if (_failed || view == null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              'The channel is down. Try again.',
              style: TextStyle(color: GFL2Colors.textDim),
            ),
            const SizedBox(height: 12),
            OutlinedButton(onPressed: _load, child: const Text('Retry')),
          ],
        ),
      );
    }
    if (view.sealed) return _sealed(view);
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 640),
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            for (var i = 0; i < view.entries.length; i++)
              _entry(view.entries[i], key: Key('thread-entry-$i')),
            if (view.heldBack > 0) _heldBack(view.heldBack),
            if (view.reply != null) _reply(view.reply!),
          ],
        ),
      ),
    );
  }

  Widget _sealed(ThreadView view) {
    return Center(
      key: const Key('thread-sealed'),
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.lock_outline_rounded,
                color: GFL2Colors.affinity.withValues(alpha: 0.7), size: 30),
            const SizedBox(height: 18),
            Text(
              view.message ?? '',
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: GFL2Colors.textPrimary,
                fontSize: 15,
                fontStyle: FontStyle.italic,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 14),
            const Text(
              'SEALED',
              style: TextStyle(
                color: GFL2Colors.textDim,
                fontSize: 10,
                fontFamily: 'monospace',
                letterSpacing: 3,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _stamp(String stamp, {bool alignEnd = false}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        stamp.toUpperCase(),
        textAlign: alignEnd ? TextAlign.end : TextAlign.start,
        style: const TextStyle(
          color: GFL2Colors.textDim,
          fontSize: 10,
          fontFamily: 'monospace',
          letterSpacing: 1.2,
        ),
      ),
    );
  }

  Widget _entry(ThreadEntry e, {Key? key}) {
    final read = e.isRead;
    return Padding(
      key: key,
      padding: const EdgeInsets.only(bottom: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _stamp(e.stamp),
          FractionallySizedBox(
            widthFactor: 0.88,
            alignment: Alignment.centerLeft,
            child: Container(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
              decoration: BoxDecoration(
                color: GFL2Colors.surface.withValues(alpha: 0.92),
                border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.8)),
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(4),
                  topRight: Radius.circular(14),
                  bottomLeft: Radius.circular(14),
                  bottomRight: Radius.circular(14),
                ),
              ),
              child: Text(
                e.text,
                style: const TextStyle(
                  color: GFL2Colors.textPrimary,
                  fontSize: 14.5,
                  height: 1.45,
                ),
              ),
            ),
          ),
          const SizedBox(height: 5),
          AnimatedSwitcher(
            duration: const Duration(milliseconds: 450),
            child: Row(
              key: ValueKey(read),
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(read ? Icons.done_all_rounded : Icons.done_rounded,
                    size: 13,
                    color: read ? GFL2Colors.primary : GFL2Colors.textDim),
                const SizedBox(width: 4),
                Text(
                  _receipt(e),
                  style: TextStyle(
                    color: read ? GFL2Colors.primary : GFL2Colors.textDim,
                    fontSize: 11,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _heldBack(int count) {
    final noun = count == 1 ? 'message' : 'messages';
    return Padding(
      padding: const EdgeInsets.only(bottom: 22),
      child: Row(
        children: [
          Icon(Icons.lock_outline_rounded,
              size: 14, color: GFL2Colors.affinity.withValues(alpha: 0.6)),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              "$count more $noun she isn't ready to show you.",
              style: const TextStyle(
                color: GFL2Colors.textDim,
                fontSize: 12,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _reply(ThreadReply reply) {
    return Padding(
      key: const Key('thread-reply'),
      padding: const EdgeInsets.only(top: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          _stamp(reply.stamp, alignEnd: true),
          Container(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            decoration: BoxDecoration(
              color: GFL2Colors.primary.withValues(alpha: 0.16),
              border: Border.all(color: GFL2Colors.primary.withValues(alpha: 0.55)),
              borderRadius: const BorderRadius.only(
                topLeft: Radius.circular(14),
                topRight: Radius.circular(4),
                bottomLeft: Radius.circular(14),
                bottomRight: Radius.circular(14),
              ),
            ),
            child: Text(
              reply.text,
              style: const TextStyle(
                color: GFL2Colors.textPrimary,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          const SizedBox(height: 5),
          const Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.done_all_rounded, size: 13, color: GFL2Colors.affinity),
              SizedBox(width: 4),
              Text(
                'Read · instantly',
                style: TextStyle(color: GFL2Colors.affinity, fontSize: 11),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
