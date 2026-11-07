import 'package:flutter/material.dart';
import '../models/conversation_search.dart';
import '../services/conversation_search_service.dart';
import '../services/session_auth.dart';
import '../theme/hud.dart';
import 'conversation_context_screen.dart';

class ConversationSearchScreen extends StatefulWidget {
  const ConversationSearchScreen({
    super.key,
    required this.serverUrl,
    this.service,
    this.onAuthExpired,
  });
  final String serverUrl;
  final ConversationSearchService? service;
  final VoidCallback? onAuthExpired;
  @override
  State<ConversationSearchScreen> createState() =>
      _ConversationSearchScreenState();
}

class _ConversationSearchScreenState extends State<ConversationSearchScreen> {
  late final ConversationSearchService _service =
      widget.service ?? ConversationSearchService(serverUrl: widget.serverUrl);
  final _query = TextEditingController();
  final _scroll = ScrollController();
  String _speaker = 'all';
  DateTime? _from, _through;
  List<ConversationSearchHit> _messages = [];
  MessageCursor? _next;
  bool _loading = false, _submitted = false, _expired = false;
  String? _error;
  int _generation = 0;
  @override
  void dispose() {
    _generation++;
    _query.dispose();
    _scroll.dispose();
    if (widget.service == null) _service.dispose();
    super.dispose();
  }

  void _invalidate() {
    setState(() {
      _generation++;
      _messages = [];
      _next = null;
      _loading = false;
      _submitted = false;
      _error = null;
    });
  }

  void _authExpired() {
    if (!mounted || _expired) return;
    setState(() {
      _expired = true;
      _generation++;
      _messages = [];
      _next = null;
      _loading = false;
      _error = null;
    });
    (widget.onAuthExpired ?? handleAuthExpired)();
  }

  Future<void> _search({bool more = false}) async {
    if (_expired || _loading) return;
    final phrase = _query.text.trim();
    String? validation;
    if (phrase.runes.length < 2 ||
        phrase.runes.length > 200 ||
        phrase.contains('\u0000')) {
      validation = 'Enter 2–200 characters.';
    }
    if (_from != null && _through != null && _from!.isAfter(_through!)) {
      validation = 'From must be on or before Through.';
    }
    if (validation != null) {
      setState(() => _error = validation);
      return;
    }
    final generation = ++_generation;
    setState(() {
      _loading = true;
      _submitted = true;
      _error = null;
      if (!more) {
        _messages = [];
        _next = null;
      }
    });
    if (!more && _scroll.hasClients) _scroll.jumpTo(0);
    try {
      final page = await _service.search(
        query: phrase,
        speaker: _speaker,
        from: _from,
        through: _through,
        cursor: more ? _next : null,
      );
      if (!mounted || generation != _generation || _expired) return;
      setState(() {
        final ids = _messages.map((m) => m.id).toSet();
        _messages = [
          ..._messages,
          ...page.messages.where((m) => ids.add(m.id)),
        ];
        _next = page.nextCursor;
        _loading = false;
      });
    } on ConversationSearchException catch (e) {
      if (!mounted) return;
      if (e.statusCode == 401) {
        _authExpired();
        return;
      }
      if (generation != _generation || _expired) return;
      setState(() {
        _loading = false;
        _error = 'Search is unavailable. Try again.';
      });
    } catch (_) {
      if (!mounted || generation != _generation || _expired) return;
      setState(() {
        _loading = false;
        _error = 'Search is unavailable. Try again.';
      });
    }
  }

  Future<void> _pickDate(bool from) async {
    final selected = await showDatePicker(
      context: context,
      initialDate: (from ? _from : _through) ?? DateTime.now(),
      firstDate: DateTime(1900),
      lastDate: DateTime(2100),
    );
    if (!mounted || selected == null || _expired) return;
    _invalidate();
    setState(() {
      if (from) {
        _from = selected;
      } else {
        _through = selected;
      }
    });
  }

  String _date(DateTime? date) => date == null
      ? 'Any date'
      : '${date.year}-${date.month.toString().padLeft(2, '0')}-${date.day.toString().padLeft(2, '0')}';
  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: HudColors.ground,
    appBar: AppBar(
      backgroundColor: HudColors.ground,
      foregroundColor: HudColors.cyan,
      title: Text('Search conversations', style: HudText.display(size: 22)),
    ),
    body: SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: CustomScrollView(
            controller: _scroll,
            slivers: [
              SliverToBoxAdapter(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        'Search an exact word or phrase',
                        style: HudText.body(),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        key: const Key('conversation-query'),
                        controller: _query,
                        enabled: !_expired,
                        maxLength: 200,
                        style: HudText.body(),
                        decoration: const InputDecoration(
                          labelText: 'Word or phrase',
                          border: OutlineInputBorder(),
                          counterText: '',
                        ),
                        onChanged: (_) => _invalidate(),
                        onSubmitted: (_) => _search(),
                        textInputAction: TextInputAction.search,
                      ),
                      const SizedBox(height: 12),
                      DropdownButtonFormField<String>(
                        key: const Key('conversation-speaker'),
                        initialValue: _speaker,
                        isExpanded: true,
                        decoration: const InputDecoration(
                          labelText: 'Speaker',
                          border: OutlineInputBorder(),
                        ),
                        dropdownColor: HudColors.ground,
                        style: HudText.body(),
                        items: const [
                          DropdownMenuItem(
                            value: 'all',
                            child: Text('Everyone'),
                          ),
                          DropdownMenuItem(value: 'user', child: Text('You')),
                          DropdownMenuItem(
                            value: 'assistant',
                            child: Text('Klukai'),
                          ),
                        ],
                        onChanged: _expired
                            ? null
                            : (v) {
                                if (v == null) return;
                                _invalidate();
                                setState(() => _speaker = v);
                              },
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          OutlinedButton(
                            key: const Key('conversation-from'),
                            onPressed: _expired ? null : () => _pickDate(true),
                            child: Text('From: ${_date(_from)}'),
                          ),
                          OutlinedButton(
                            key: const Key('conversation-through'),
                            onPressed: _expired ? null : () => _pickDate(false),
                            child: Text('Through: ${_date(_through)}'),
                          ),
                          TextButton(
                            onPressed: _expired
                                ? null
                                : () {
                                    _invalidate();
                                    setState(() {
                                      _from = null;
                                      _through = null;
                                      _speaker = 'all';
                                    });
                                  },
                            child: const Text('Clear filters'),
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      FilledButton.icon(
                        key: const Key('conversation-submit'),
                        onPressed: _loading || _expired
                            ? null
                            : () => _search(),
                        icon: const Icon(Icons.search),
                        label: const Text('Search'),
                      ),
                      if (_expired)
                        Text('Session expired.', style: HudText.body()),
                      if (!_submitted && _error == null && !_expired)
                        Padding(
                          padding: const EdgeInsets.only(top: 24),
                          child: Text(
                            'Find original messages by speaker and local date.',
                            style: HudText.body(color: HudColors.textDim),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              SliverList(
                delegate: SliverChildBuilderDelegate((context, index) {
                  final hit = _messages[index];
                  return Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                    child: Material(
                      color: HudColors.cyanA(.06),
                      shape: CutCornerBorder(
                        side: BorderSide(color: HudColors.cyanA(.35)),
                      ),
                      child: InkWell(
                        onTap: _expired
                            ? null
                            : () => Navigator.push(
                                context,
                                MaterialPageRoute(
                                  builder: (_) => ConversationContextScreen(
                                    serverUrl: widget.serverUrl,
                                    anchorId: hit.id,
                                    service: _service,
                                    onAuthExpired: _authExpired,
                                  ),
                                ),
                              ),
                        child: Padding(
                          padding: const EdgeInsets.all(14),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              Text(
                                conversationMessageStamp(
                                  hit.role,
                                  hit.createdAt,
                                ),
                                style: HudText.label(size: 11),
                              ),
                              const SizedBox(height: 8),
                              Text(hit.excerpt, style: HudText.body(size: 17)),
                              const SizedBox(height: 8),
                              Text(
                                'Open original exchange',
                                style: HudText.body(color: HudColors.cyan),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  );
                }, childCount: _messages.length),
              ),
              SliverToBoxAdapter(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    children: [
                      if (_loading)
                        const Padding(
                          padding: EdgeInsets.all(16),
                          child: CircularProgressIndicator(),
                        ),
                      if (_error != null) ...[
                        Text(
                          _error!,
                          style: HudText.body(color: HudColors.danger),
                        ),
                        if (_submitted)
                          OutlinedButton(
                            onPressed: () =>
                                _search(more: _messages.isNotEmpty),
                            child: const Text('Retry'),
                          ),
                      ],
                      if (_submitted &&
                          !_loading &&
                          _error == null &&
                          _messages.isEmpty &&
                          !_expired)
                        Text(
                          'No matching conversations.',
                          style: HudText.body(),
                        ),
                      if (_next != null && !_loading && _error == null)
                        OutlinedButton(
                          onPressed: () => _search(more: true),
                          child: const Text('Load more'),
                        ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}
