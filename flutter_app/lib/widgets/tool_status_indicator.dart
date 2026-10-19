import 'package:flutter/material.dart';
import '../theme/hud.dart';

String _frameToolName(String toolName) {
  final lower = toolName.toLowerCase();
  if (lower.contains('search') || lower.contains('web')) return 'INTEL';
  if (lower.contains('browse') || lower.contains('fetch')) return 'RECON';
  if (lower.contains('code') || lower.contains('run')) return 'SYSTEMS';
  if (lower.contains('file') || lower.contains('read')) return 'DATA';
  return 'OPERATION';
}

class ToolStatusIndicator extends StatelessWidget {
  final String toolName;
  final String status;

  const ToolStatusIndicator({
    super.key,
    required this.toolName,
    required this.status,
  });

  @override
  Widget build(BuildContext context) {
    final isDone = status == 'done';
    final framedName = _frameToolName(toolName);

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: HudColors.cyanA(0.05),
          border: Border(
            left: BorderSide(
              color: isDone ? HudColors.online : HudColors.cyan,
              width: 2,
            ),
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (!isDone)
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: SizedBox(
                  width: 10,
                  height: 10,
                  child: CircularProgressIndicator(
                    strokeWidth: 1.5,
                    color: HudColors.cyanA(0.6),
                  ),
                ),
              )
            else
              Padding(
                padding: const EdgeInsets.only(right: 6),
                child: Icon(
                  Icons.check,
                  size: 12,
                  color: HudColors.online.withValues(alpha: 0.8),
                ),
              ),
            Flexible(
              child: Text(
                '$framedName // $toolName',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: HudText.label(
                  size: 10,
                  weight: FontWeight.w600,
                  color: isDone
                      ? HudColors.online.withValues(alpha: 0.7)
                      : HudColors.cyanA(0.8),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
