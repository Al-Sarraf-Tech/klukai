import 'package:companion_app/services/her_pov_job_owner.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('a failed status is not left on an in-progress phase', () {
    expect(
      herPovPhaseName({'status': 'failed', 'phase': 'drawing'}),
      'failed',
    );
    expect(
      herPovPhaseName({'status': 'done', 'phase': 'searching'}),
      'done',
    );
    expect(herPovPhaseName({'phase': 'thinking'}), 'thinking');
    expect(herPovPhaseName({}), 'searching');
  });
}
