import copy
import unittest
from unittest.mock import patch

import app as app_module


class StaffReportDetailTests(unittest.TestCase):
    def setUp(self):
        self.original_reports = app_module.reports
        app_module.reports = copy.deepcopy(self.original_reports)
        self.client = app_module.app.test_client()
        self.weather_patch = patch.object(app_module, 'get_weather_warnings', return_value={
            'area_name': '青森市', 'warnings': [],
            'report_time': '2026年09月29日 09:00',
            'last_fetch_time': '2026年09月29日 09:05'
        })
        self.weather_patch.start()

    def tearDown(self):
        self.weather_patch.stop()
        app_module.reports = self.original_reports

    def login_staff(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['username'] = 'staff-test'

    def test_detail_is_private_and_login_return_url_is_preserved(self):
        response = self.client.get('/reports/1004')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=', response.headers['Location'])
        self.assertIn('/reports/1004', response.headers['Location'])
        self.assertNotIn('施設職員', response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['username'] = 'staff-test'
        navigation = self.client.get('/').get_data(as_text=True)
        self.assertIn('通報詳細を確認', navigation)

    def test_default_id_order_and_previous_next_links(self):
        self.login_staff()
        response = self.client.get('/report_detail')
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('通報ID: 1001', html)
        self.assertIn('1 / 全6件', html)
        self.assertIn('/reports/1002', html)
        self.assertIn('先頭の通報です', html)

        middle = self.client.get('/reports/1003').get_data(as_text=True)
        self.assertIn('3 / 全6件', middle)
        self.assertIn('/reports/1002', middle)
        self.assertIn('/reports/1004', middle)

        last = self.client.get('/reports/1006').get_data(as_text=True)
        self.assertIn('6 / 全6件', last)
        self.assertIn('最後の通報です', last)

    def test_empty_and_unknown_id_have_distinct_states(self):
        self.login_staff()
        original_reports = app_module.reports
        try:
            app_module.reports = []
            empty = self.client.get('/report_detail')
            self.assertEqual(empty.status_code, 200)
            self.assertIn('確認できる通報はありません', empty.get_data(as_text=True))
        finally:
            app_module.reports = original_reports

        missing = self.client.get('/reports/999999')
        self.assertEqual(missing.status_code, 404)
        self.assertIn('指定された通報IDは存在しない', missing.get_data(as_text=True))

    def test_fields_level_progress_history_and_photo_fallback(self):
        report = next(item for item in app_module.reports if item['id'] == 1004)
        report['disaster_level'] = 5
        report['comment'] = '<script>unsafe()</script>'
        report['response_history'][-1]['changed_at'] = ''
        self.login_staff()
        detail = self.client.get('/reports/1004')
        html = detail.get_data(as_text=True)
        self.assertIn('レベル5', html)
        self.assertIn('対応済み（指示済み）', html)
        self.assertIn('2026年09月29日 08:55', html)
        self.assertIn('時刻未記録', html)
        self.assertIn('指示済み', html)
        self.assertIn('写真', html)
        self.assertIn('写真は未登録です', html)
        self.assertIn('&lt;script&gt;unsafe()&lt;/script&gt;', html)
        self.assertNotIn('<script>unsafe()</script>', html)

        report.pop('disaster_level')
        unknown_level = self.client.get('/reports/1004').get_data(as_text=True)
        self.assertIn('level-unknown', unknown_level)
        self.assertIn('災害レベル</dt><dd><span class="level-badge level-unknown">未登録', unknown_level)
        self.assertNotIn('通報ID: 1004', self.client.get('/reports/999999').get_data(as_text=True))

        report['photo_key'] = 'a' * 32 + '.png'
        with patch.object(app_module, 'report_photo_url', return_value='/trusted-photo.png'):
            photo_html = self.client.get('/reports/1004').get_data(as_text=True)
        self.assertIn('src="/trusted-photo.png"', photo_html)
        self.assertIn('alt="通報ID 1004 に添付された災害現場の写真"', photo_html)

    def test_weather_warnings_none_and_failure_do_not_hide_detail(self):
        self.login_staff()
        with patch.object(app_module, 'get_weather_warnings', return_value={
            'area_name': '青森市',
            'warnings': [{'name': '大雨警報', 'code': '03', 'status': '継続'}],
            'report_time': '2026年09月29日 09:00',
            'last_fetch_time': '2026年09月29日 09:05'
        }):
            warning = self.client.get('/reports/1004')
            self.assertEqual(warning.status_code, 200)
            self.assertIn('大雨警報', warning.get_data(as_text=True))

        with patch.object(app_module, 'get_weather_warnings', side_effect=TimeoutError):
            failed = self.client.get('/reports/1004')
            html = failed.get_data(as_text=True)
            self.assertEqual(failed.status_code, 200)
            self.assertIn('通報詳細は引き続き確認できます', html)
            self.assertIn('通報ID: 1004', html)


if __name__ == '__main__':
    unittest.main()