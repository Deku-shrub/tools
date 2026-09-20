import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import statusline  # noqa: E402
import usage  # noqa: E402

NOW = 1_800_000_000


def payload(five=74, seven=15, five_resets=NOW + 3600, seven_resets=NOW + 5 * 86400):
    return {'session_id': 's', 'rate_limits': {
        'five_hour': {'used_percentage': five, 'resets_at': five_resets},
        'seven_day': {'used_percentage': seven, 'resets_at': seven_resets}}}


class StatuslineTests(unittest.TestCase):
    def test_record_from_a_full_payload(self):
        record = statusline.build_record(payload(), now=NOW)
        self.assertTrue(record['available'])
        self.assertEqual(record['five_hour'], {'used_percentage': 74.0, 'resets_at': NOW + 3600})

    def test_payload_without_rate_limits_is_recorded_as_unavailable_with_keys(self):
        record = statusline.build_record({'model': 'x', 'cwd': '/'}, now=NOW)
        self.assertFalse(record['available'])
        self.assertEqual(record['payload_keys'], ['cwd', 'model'])

    def test_one_window_only(self):
        record = statusline.build_record({'rate_limits': {'five_hour': {'used_percentage': 5}}}, now=NOW)
        self.assertTrue(record['available'])
        self.assertIsNone(record['seven_day'])

    def test_line_shows_percentages_and_time_to_reset(self):
        line = statusline.format_line(statusline.build_record(payload(), now=NOW), now=NOW)
        self.assertEqual(line, '5h 74% (resets 1h00m) | 7d 15% (resets 120h00m)')

    def test_end_to_end_through_the_script(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, 'CLAUDE_USAGE_DIR': directory}
            result = subprocess.run([sys.executable, str(HERE / 'statusline.py')], input=json.dumps(payload()),
                                    capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0)
            self.assertIn('5h 74%', result.stdout)
            self.assertTrue(json.loads((Path(directory) / 'latest.json').read_text())['available'])

    def test_garbage_stdin_never_crashes_the_status_line(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, 'CLAUDE_USAGE_DIR': directory}
            result = subprocess.run([sys.executable, str(HERE / 'statusline.py')], input='not json',
                                    capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'usage: n/a')


class UsageTests(unittest.TestCase):
    def record(self, **kwargs):
        return statusline.build_record(payload(**kwargs), now=NOW)

    def test_over_limit_names_the_windows(self):
        hits = usage.over_limit(self.record(five=92, seven=15), 90, now=NOW)
        self.assertEqual([hit[0] for hit in hits], ['five_hour'])

    def test_exactly_at_the_limit_counts_as_over(self):
        self.assertTrue(usage.over_limit(self.record(five=90), 90, now=NOW))

    def test_a_window_that_has_reset_is_not_over(self):
        record = self.record(five=95, five_resets=NOW - 10)
        self.assertEqual(usage.over_limit(record, 90, now=NOW), [])

    def test_window_filter(self):
        record = self.record(five=10, seven=95)
        self.assertEqual(usage.over_limit(record, 90, windows=('five_hour',), now=NOW), [])
        self.assertTrue(usage.over_limit(record, 90, windows=('seven_day',), now=NOW))

    def test_stale_or_missing_data_is_not_usable(self):
        record = self.record()
        self.assertTrue(usage.usable(record, 30, now=NOW + 60))
        self.assertFalse(usage.usable(record, 30, now=NOW + 31 * 60))
        self.assertFalse(usage.usable(None, 30, now=NOW))
        self.assertFalse(usage.usable({'available': False, 'updated_at': NOW}, 30, now=NOW))

    def run_cli(self, directory, *arguments):
        environment = {**os.environ, 'CLAUDE_USAGE_DIR': directory}
        return subprocess.run([sys.executable, str(HERE / 'usage.py'), *arguments], capture_output=True, text=True, env=environment)

    def test_check_exit_codes(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli(directory, '--check', '90').returncode, usage.NO_DATA)  # nothing recorded
            now = time.time()
            record = statusline.build_record(payload(five=50, five_resets=now + 3600, seven_resets=now + 86400), now=now)
            (Path(directory) / 'latest.json').write_text(json.dumps(record))
            self.assertEqual(self.run_cli(directory, '--check', '90').returncode, usage.OK)
            self.assertEqual(self.run_cli(directory, '--check', '40').returncode, usage.OVER)


if __name__ == '__main__':
    unittest.main()
