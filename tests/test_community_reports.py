import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from werkzeug.datastructures import FileStorage

import app as app_module
from bousai_app import community_storage
from bousai_app.community_reports import ReportInputError, sanitize_uploaded_photo


class CommunityReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.environment = patch.dict(os.environ, {
            'COMMUNITY_STORAGE_MODE': 'sqlite',
            'COMMUNITY_REPORTS_DB_PATH': str(root / 'reports.sqlite3'),
            'COMMUNITY_REPORTS_UPLOAD_DIR': str(root / 'photos'),
            'VERCEL': '0',
            'VERCEL_ENV': ''
        }, clear=False)
        self.environment.start()
        self.original_config = {
            key: app_module.app.config[key]
            for key in ('TESTING', 'MAX_CONTENT_LENGTH', 'PROPAGATE_EXCEPTIONS')
        }
        app_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.app.config.update(self.original_config)
        self.environment.stop()
        self.temp_dir.cleanup()

    def csrf_token(self):
        response = self.client.get('/community-report/new')
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session['_csrf_token']

    def submission(self, **overrides):
        data = {
            'csrf_token': self.csrf_token(),
            'category': '津波',
            'reported_at': '2026/09/29 09:10',
            'latitude': '40.8222',
            'longitude': '140.7478',
            'comment': ''
        }
        data.update(overrides)
        return self.client.post('/community-report/new', data=data)

    def test_public_routes_and_form_contract(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/community-reports').status_code, 200)
        html = self.client.get('/community-report/new').get_data(as_text=True)
        fields = ('id="category"', 'id="reported-at"', 'id="locate-me"', 'id="photo"', 'id="comment"', 'class="submit-button"')
        positions = [html.index(field) for field in fields]
        self.assertEqual(positions, sorted(positions))
        for category in ('津波', '河川氾濫', '道路冠水', '道路寸断', '熊', 'その他'):
            self.assertIn(f'>{category}</option>', html)
        self.assertEqual(html.count('id="locate-me"'), 1)
        self.assertIn('getCurrentPosition', html)
        self.assertIn('draggable: true', html)
        self.assertNotIn('type="datetime-local"', html)
        self.assertNotIn('type="number"', html)
        self.assertNotIn('name="reporter"', html)
        self.assertIn('YYYY/MM/DD HH:MM', html)

    def test_required_values_and_datetime_are_server_validated(self):
        missing_point = self.submission(latitude='', longitude='')
        self.assertEqual(missing_point.status_code, 400)
        self.assertIn('地図上で場所を選択', missing_point.get_data(as_text=True))

        invalid_date = self.submission(reported_at='2026/02/30 12:00')
        self.assertEqual(invalid_date.status_code, 400)
        self.assertIn('YYYY/MM/DD HH:MM', invalid_date.get_data(as_text=True))

        invalid_category = self.submission(category='火災')
        self.assertEqual(invalid_category.status_code, 400)
        self.assertIn('災害の種類を選択', invalid_category.get_data(as_text=True))

        invalid_coordinates = self.submission(latitude='91', longitude='181')
        self.assertEqual(invalid_coordinates.status_code, 400)
        self.assertIn('有効な範囲', invalid_coordinates.get_data(as_text=True))
        self.assertFalse(Path(os.environ['COMMUNITY_REPORTS_DB_PATH']).exists())

    def test_optional_comment_and_photo_submission_persists_jst(self):
        response = self.submission()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/community-reports'))
        saved = community_storage.list_reports(include_private=True)[0]
        self.assertEqual(saved['category'], '津波')
        self.assertEqual(saved['reported_at'], '2026-09-29T09:10:00+09:00')
        self.assertEqual(saved['comment'], '')
        self.assertIsNone(saved['photo_key'])
        self.assertEqual(saved['reporter'], '')
        self.assertEqual(saved['place'], '地図で選択した地点')

        public = self.client.get('/community-reports').get_data(as_text=True)
        self.assertIn('津波', public)
        self.assertNotIn('reporter', public)

    def test_jpeg_png_and_webp_are_safely_stored_as_png(self):
        for image_format, mime_type, extension in (
            ('JPEG', 'image/jpeg', '.jpg'),
            ('PNG', 'image/png', '.png'),
            ('WEBP', 'image/webp', '.webp')
        ):
            image = Image.new('RGB', (12, 12), 'blue')
            source = BytesIO()
            image.save(source, format=image_format)
            response = self.submission(photo=(BytesIO(source.getvalue()), f'photo{extension}', mime_type))
            self.assertEqual(response.status_code, 302)

        saved_reports = community_storage.list_reports(include_private=True)
        self.assertEqual(len(saved_reports), 3)
        for report in saved_reports:
            self.assertRegex(report['photo_key'], r'^[a-f0-9]{32}\.png$')
            with Image.open(community_storage.local_photo_path(report['photo_key'])) as normalized:
                self.assertEqual(normalized.format, 'PNG')

    def test_disallowed_and_oversized_photos_are_rejected(self):
        svg = self.submission(photo=(BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), 'bad.png', 'image/png'))
        self.assertEqual(svg.status_code, 400)

        gif_image = Image.new('RGB', (10, 10), 'red')
        gif_content = BytesIO()
        gif_image.save(gif_content, format='GIF')
        gif = self.submission(photo=(BytesIO(gif_content.getvalue()), 'bad.gif', 'image/gif'))
        self.assertEqual(gif.status_code, 400)

        with self.assertRaises(ReportInputError):
            sanitize_uploaded_photo(FileStorage(
                stream=BytesIO(b'x' * (8 * 1024 * 1024 + 1)),
                filename='large.png',
                content_type='image/png'
            ))
        self.assertEqual(community_storage.list_reports(include_private=True), [])

    def test_login_and_production_storage_are_protected(self):
        login_html = self.client.get('/login').get_data(as_text=True)
        self.assertNotIn('123', login_html)
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        with patch.dict(os.environ, {'STAFF_USERNAME': 'staff', 'STAFF_PASSWORD': 'test-pass'}):
            response = self.client.post('/login', data={
                'csrf_token': token,
                'username': 'staff',
                'password': 'test-pass',
                'next': '/staff/community-reports'
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/staff/community-reports'))

        database_path = Path(os.environ['COMMUNITY_REPORTS_DB_PATH'])
        with patch.dict(os.environ, {
            'VERCEL': '1', 'VERCEL_ENV': 'production',
            'SUPABASE_URL': '', 'SUPABASE_SERVICE_ROLE_KEY': ''
        }, clear=False):
            response = self.client.get('/community-reports')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(database_path.exists())


if __name__ == '__main__':
    unittest.main()