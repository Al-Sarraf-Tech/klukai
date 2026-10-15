import 'dart:convert';

/// A deliberately closed subset of marked's lexer output. If any token cannot
/// be represented faithfully, the consumer presents the entire source instead.
class MarkdownToken {
  MarkdownToken._(
    this.type,
    this.text,
    this.raw,
    this.href,
    this.depth,
    this.ordered,
    this.start,
    this.tokens,
    this.items,
    this.header,
    this.rows,
  );
  final String type, text, raw;
  final String? href;
  final int depth, start;
  final bool ordered;
  final List<MarkdownToken> tokens, items, header;
  final List<List<MarkdownToken>> rows;

  static MarkdownToken read(dynamic value, [int nesting = 0]) {
    if (value is! Map<String, dynamic> || nesting > 40) {
      throw const FormatException('Invalid token');
    }
    final type = value['type'] as String? ?? 'cell';
    const supported = {
      'paragraph',
      'text',
      'heading',
      'blockquote',
      'list',
      'list_item',
      'code',
      'space',
      'hr',
      'strong',
      'em',
      'del',
      'codespan',
      'escape',
      'br',
      'link',
      'cell',
      'table',
    };
    if (!supported.contains(type) || value['task'] == true) {
      throw const FormatException('Unsupported token');
    }
    List<MarkdownToken> children(String key) {
      final items = value[key];
      if (items == null) return [];
      if (items is! List) throw const FormatException('Invalid children');
      return items.map((v) => read(v, nesting + 1)).toList();
    }

    final text = value['text'] as String? ?? '';
    final tokens = children('tokens');
    final items = children('items');
    final header = type == 'table' ? children('header') : <MarkdownToken>[];
    final rawRows = type == 'table' ? value['rows'] : null;
    final rows = <List<MarkdownToken>>[];
    if (rawRows != null) {
      if (rawRows is! List) throw const FormatException('Invalid rows');
      for (final row in rawRows) {
        if (row is! List || row.length != header.length) {
          throw const FormatException('Invalid cells');
        }
        rows.add(row.map((v) => read(v, nesting + 1)).toList());
      }
    }
    const inlineTypes = {
      'text',
      'escape',
      'strong',
      'em',
      'del',
      'codespan',
      'br',
      'link',
    };
    const inlineContainers = {
      'paragraph',
      'text',
      'heading',
      'strong',
      'em',
      'del',
      'link',
      'cell',
    };
    if (inlineContainers.contains(type) &&
        tokens.any((t) => !inlineTypes.contains(t.type))) {
      throw const FormatException('Invalid inline token');
    }
    if (inlineContainers.contains(type) &&
        value['text'] is! String &&
        tokens.isEmpty) {
      throw const FormatException('Missing text');
    }
    if (type == 'table' &&
        (header.isEmpty ||
            header.any((t) => t.type != 'cell') ||
            rows.any((row) => row.any((t) => t.type != 'cell')))) {
      throw const FormatException('Invalid table');
    }
    if (type == 'list' &&
        (items.any((t) => t.type != 'list_item') ||
            value['ordered'] == true && value['start'] is! int)) {
      throw const FormatException('Invalid list');
    }
    if (type == 'list' && items.isEmpty ||
        type == 'blockquote' && tokens.isEmpty) {
      throw const FormatException('Missing content');
    }
    if (type == 'heading' &&
        (value['depth'] is! int || value['depth'] < 1 || value['depth'] > 6)) {
      throw const FormatException('Invalid heading');
    }
    if (type == 'link' && value['href'] is! String) {
      throw const FormatException('Missing URL');
    }
    return MarkdownToken._(
      type,
      text,
      value['raw'] as String? ?? '',
      value['href'] as String?,
      value['depth'] as int? ?? 1,
      value['ordered'] as bool? ?? false,
      value['start'] is int ? value['start'] as int : 1,
      tokens,
      items,
      header,
      rows,
    );
  }

  static List<MarkdownToken>? document(String source, String? json) {
    try {
      if (json == null) return null;
      final values = jsonDecode(json);
      if (values is! List || values.isEmpty && source.isNotEmpty) return null;
      return values.map((value) => read(value)).toList();
    } catch (_) {
      return null;
    }
  }
}
