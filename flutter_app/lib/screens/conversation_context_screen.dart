import 'package:flutter/material.dart';
import '../models/conversation_search.dart';
import '../services/conversation_search_service.dart';
import '../services/session_auth.dart';
import '../theme/hud.dart';

String conversationMessageStamp(String role, DateTime date) {
  final local = date.toLocal();
  String two(int value) => value.toString().padLeft(2, '0');
  return '${role == 'user' ? 'You' : 'Klukai'} · ${local.year}-${two(local.month)}-${two(local.day)} ${two(local.hour)}:${two(local.minute)}:${two(local.second)} (local)';
}

class ConversationContextScreen extends StatefulWidget {
  const ConversationContextScreen({
    super.key,
    required this.serverUrl,
    required this.anchorId,
    this.service,
    this.onAuthExpired,
  });
  final String serverUrl, anchorId;
  final ConversationSearchService? service;
  final VoidCallback? onAuthExpired;
  @override
  State<ConversationContextScreen> createState() =>
      _ConversationContextScreenState();
}

class _ConversationContextScreenState extends State<ConversationContextScreen> {
  late final ConversationSearchService _service =
      widget.service ?? ConversationSearchService(serverUrl: widget.serverUrl);
  final _scroll = ScrollController();
  final _anchor = GlobalKey();
  List<ConversationSourceMessage> _messages = [];
  MessageCursor? _older, _newer;
  bool _loading = true, _notFound = false, _failed = false, _expired = false;
  String? _retryDirection;
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _scroll.dispose();
    if (widget.service == null) _service.dispose();
    super.dispose();
  }

  Future<void> _load([String? direction]) async {
    if (_expired) return;
    final previousExtent = _scroll.hasClients
        ? _scroll.position.maxScrollExtent
        : 0.0;
    final previousOffset = _scroll.hasClients ? _scroll.offset : 0.0;
    setState(() {
      _loading = true;
      _failed = false;
      _notFound = false;
      _retryDirection = direction;
    });
    try {
      final page = await _service.context(
        anchorId: widget.anchorId,
        before: direction == 'older' ? _older : null,
        after: direction == 'newer' ? _newer : null,
      );
      if (!mounted || _expired) return;
      if (page.anchorId != widget.anchorId) {
        throw const FormatException('Unexpected anchor');
      }
      setState(() {
        final merged = {
          for (final m in _messages) m.id: m,
          for (final m in page.messages) m.id: m,
        };
        _messages = merged.values.toList()
          ..sort((a, b) {
            final order = a.createdAt.compareTo(b.createdAt);
            return order != 0 ? order : a.id.compareTo(b.id);
          });
        if (direction != 'newer') _older = page.olderCursor;
        if (direction != 'older') _newer = page.newerCursor;
        _loading = false;
      });
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted || !_scroll.hasClients) return;
        if (direction == null) {
          final target = _anchor.currentContext;
          if (target != null) Scrollable.ensureVisible(target, alignment: .35);
        } else if (direction == 'older') {
          _scroll.jumpTo(
            (previousOffset + _scroll.position.maxScrollExtent - previousExtent)
                .clamp(0.0, _scroll.position.maxScrollExtent),
          );
        }
      });
    } on ConversationSearchException catch (e) {
      if (!mounted || _expired) return;
      if (e.statusCode == 401) {
        setState(() {
          _expired = true;
          _loading = false;
          _messages = [];
        });
        (widget.onAuthExpired ?? handleAuthExpired)();
        return;
      }
      setState(() {
        _loading = false;
        _notFound = e.statusCode == 404;
        _failed = !_notFound;
      });
    } catch (_) {
      if (!mounted || _expired) return;
      setState(() {
        _loading = false;
        _failed = true;
      });
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: HudColors.ground,
    appBar: AppBar(
      backgroundColor: HudColors.ground,
      foregroundColor: HudColors.cyan,
      title: Text('Original exchange', style: HudText.display(size: 22)),
    ),
    body: SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: SingleChildScrollView(
            controller: _scroll,
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (_expired) Text('Session expired.', style: HudText.body()),
                if (_notFound)
                  Text('Original message not found.', style: HudText.body()),
                if (_older != null && !_notFound)
                  OutlinedButton(
                    onPressed: _loading || _expired
                        ? null
                        : () => _load('older'),
                    child: const Text('Load earlier'),
                  ),
                for (final message in _messages)
                  Padding(
                    key: ValueKey(message.id),
                    padding: const EdgeInsets.only(bottom: 16),
                    child: DecoratedBox(
                      decoration: ShapeDecoration(
                        color: message.id == widget.anchorId
                            ? HudColors.violetA(.15)
                            : HudColors.cyanA(.05),
                        shape: CutCornerBorder(
                          side: BorderSide(
                            color: message.id == widget.anchorId
                                ? HudColors.violet
                                : HudColors.cyanA(.3),
                          ),
                        ),
                      ),
                      child: Padding(
                        padding: const EdgeInsets.all(14),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            if (message.id == widget.anchorId)
                              Padding(
                                key: _anchor,
                                padding: const EdgeInsets.only(bottom: 8),
                                child: Text(
                                  'Selected message',
                                  style: HudText.body(
                                    color: HudColors.violet,
                                    weight: FontWeight.w700,
                                  ),
                                ),
                              ),
                            Text(
                              conversationMessageStamp(
                                message.role,
                                message.createdAt,
                              ),
                              style: HudText.label(size: 11),
                            ),
                            const SizedBox(height: 8),
                            SelectableText(
                              message.content,
                              style: HudText.body(size: 17),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                if (_newer != null && !_notFound)
                  OutlinedButton(
                    onPressed: _loading || _expired
                        ? null
                        : () => _load('newer'),
                    child: const Text('Load later'),
                  ),
                if (_loading)
                  const Center(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: CircularProgressIndicator(),
                    ),
                  ),
                if (_failed) ...[
                  Text(
                    'Original exchange is unavailable. Try again.',
                    style: HudText.body(color: HudColors.danger),
                  ),
                  OutlinedButton(
                    onPressed: () => _load(_retryDirection),
                    child: const Text('Retry'),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    ),
  );
}
