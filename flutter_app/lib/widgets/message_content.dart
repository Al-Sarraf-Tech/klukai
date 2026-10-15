import 'package:flutter/material.dart';
import '../models/markdown_document.dart';
import '../platform/browser.dart';
import '../services/markdown_parser.dart';
import '../theme/gfl2_colors.dart';

/// Selectable native layout. Parsing never owns geometry or navigation.
class MessageContent extends StatelessWidget {
  const MessageContent({super.key, required this.source, this.browser});
  final String source;
  final BrowserPlatform? browser;
  static const _style = TextStyle(
    color: GFL2Colors.textPrimary,
    fontSize: 15,
    height: 1.5,
  );

  @override
  Widget build(BuildContext context) {
    final document = MarkdownToken.document(source, parseMarkdown(source));
    if (document == null) return SelectableText(source, style: _style);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        ...document.map((token) => _block(token)),
        if (document.any(
              (token) => token.type != 'paragraph' && token.type != 'space',
            ) ||
            _links(document).isNotEmpty)
          Material(
            color: Colors.transparent,
            child: ExpansionTile(
              title: const Text(
                'Message source',
                style: TextStyle(color: GFL2Colors.textMuted, fontSize: 12),
              ),
              tilePadding: EdgeInsets.zero,
              children: [SelectableText(source, style: _style)],
            ),
          ),
      ],
    );
  }

  List<MarkdownToken> _links(Iterable<MarkdownToken> tokens) => [
    for (final token in tokens) ...[
      if (token.type == 'link') token,
      ..._links(token.tokens),
      ..._links(token.items),
      ..._links(token.header),
      for (final row in token.rows) ..._links(row),
    ],
  ];

  TextSpan _inline(MarkdownToken token, [TextStyle? inherited]) {
    var style = inherited ?? _style;
    if (token.type == 'strong') {
      style = style.copyWith(fontWeight: FontWeight.w700);
    }
    if (token.type == 'em') style = style.copyWith(fontStyle: FontStyle.italic);
    if (token.type == 'del') {
      style = style.copyWith(decoration: TextDecoration.lineThrough);
    }
    if (token.type == 'codespan') {
      style = style.copyWith(
        fontFamily: 'monospace',
        color: GFL2Colors.primary,
      );
    }
    if (token.type == 'link') {
      style = style.copyWith(
        color: GFL2Colors.primary,
        decoration: TextDecoration.underline,
      );
    }
    if (token.type == 'br') return TextSpan(text: '\n', style: style);
    return token.tokens.isEmpty
        ? TextSpan(text: token.text, style: style)
        : TextSpan(
            style: style,
            children: token.tokens.map((t) => _inline(t, style)).toList(),
          );
  }

  Widget _text(MarkdownToken token, {TextStyle? style}) => Column(
    mainAxisSize: MainAxisSize.min,
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      SelectableText.rich(_inline(token, style)),
      for (final link in _links([token])) _linkAction(link),
    ],
  );

  Widget _linkAction(MarkdownToken token) {
    final uri = Uri.tryParse(token.href ?? '');
    final safe =
        uri != null &&
        (uri.scheme == 'http' || uri.scheme == 'https') &&
        uri.host.isNotEmpty;
    // The URL is always inspectable; only explicit native buttons navigate.
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: safe
          ? TextButton(
              onPressed: () => (browser ?? defaultBrowserPlatform()).openUrl(
                uri.toString(),
                newWindow: true,
              ),
              style: TextButton.styleFrom(
                alignment: Alignment.centerLeft,
                foregroundColor: GFL2Colors.primary,
              ),
              child: Text('Open link: ${token.href}', softWrap: true),
            )
          : SelectableText(
              'Link blocked: ${token.href}',
              style: const TextStyle(color: GFL2Colors.textMuted, fontSize: 13),
            ),
    );
  }

  Widget _block(MarkdownToken token) {
    Widget child;
    switch (token.type) {
      case 'space':
        return const SizedBox(height: 6);
      case 'hr':
        return const Divider(color: GFL2Colors.border);
      case 'heading':
        child = Semantics(
          header: true,
          child: _text(
            token,
            style: _style.copyWith(
              fontSize: 24 - token.depth.toDouble(),
              fontWeight: FontWeight.w700,
            ),
          ),
        );
      case 'blockquote':
        child = Container(
          padding: const EdgeInsets.only(left: 12),
          decoration: const BoxDecoration(
            border: Border(
              left: BorderSide(color: GFL2Colors.primary, width: 2),
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: token.tokens.map(_block).toList(),
          ),
        );
      case 'list':
        child = Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            for (var i = 0; i < token.items.length; i++)
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SelectableText(
                      token.ordered ? '${token.start + i}.' : '•',
                      style: _style,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        mainAxisSize: MainAxisSize.min,
                        children: token.items[i].tokens.isEmpty
                            ? [_text(token.items[i])]
                            : token.items[i].tokens.map(_block).toList(),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        );
      case 'code':
        child = Container(
          color: GFL2Colors.panel,
          padding: const EdgeInsets.all(8),
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: SelectableText(
              token.text,
              style: _style.copyWith(
                fontFamily: 'monospace',
                color: GFL2Colors.primary,
              ),
            ),
          ),
        );
      case 'table':
        child = SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Table(
            defaultColumnWidth: const IntrinsicColumnWidth(),
            border: TableBorder.all(color: GFL2Colors.border),
            children: [
              TableRow(
                decoration: const BoxDecoration(color: GFL2Colors.panel),
                children: [
                  for (final cell in token.header)
                    Padding(
                      padding: const EdgeInsets.all(8),
                      child: _text(
                        cell,
                        style: _style.copyWith(fontWeight: FontWeight.w700),
                      ),
                    ),
                ],
              ),
              for (final row in token.rows)
                TableRow(
                  children: [
                    for (final cell in row)
                      Padding(
                        padding: const EdgeInsets.all(8),
                        child: _text(cell),
                      ),
                  ],
                ),
            ],
          ),
        );
      default:
        child = _text(token);
    }
    return Padding(padding: const EdgeInsets.only(bottom: 8), child: child);
  }
}
