import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module


class EvacuationBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.instructions_path = Path(self.temp_dir.name) / 'instructions.json'
        self.original_file = app_module.INSTRUCTIONS_FILE
        self.original_instructions = app_module.instructions
        self.legacy_instruction = {
            'id': 4, 'target': '住民', 'content': '既存のお知らせ',
            'shelter': '片瀬小学校', 'status': '解除'
        }
        app_module.INSTRUCTIONS_FILE = str(self.instructions_path)
        app_module.instructions = [copy.deepcopy(self.legacy_instruction)]
        self.areas = app_module.get_evacuation_areas()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['username'] = 'staff-test'
            session['_csrf_token'] = 'evacuation-test-token'

    def tearDown(self):
        app_module.INSTRUCTIONS_FILE = self.original_file
        app_module.instructions = self.original_instructions
        self.temp_dir.cleanup()

    def post_board(self, regions, shelters, **overrides):
        now = datetime.now(app_module.JST)
        data = {
            'csrf_token': 'evacuation-test-token',
            'region_ids': regions,
            'shelter_ids': shelters,
            'period_start': (now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M'),
            'period_end': (now + timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M'),
            'reason': '津波',
            'reason_details': ''
        }
        data.update(overrides)
        return self.client.post('/board', data=data)

    def test_unauthenticated_access_redirects_and_ui_has_no_instruction_table(self):
        client = app_module.app.test_client()
        response = client.get('/board')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=', response.headers['Location'])

        html = self.client.get('/board').get_data(as_text=True)
        self.assertEqual(html.count('<button class="choice-button area-button"'), 6)
        self.assertEqual(html.count('<button class="choice-button shelter-button"'), 18)
        self.assertEqual(html.count('aria-pressed="true">'), 0)
        self.assertIn('指示を更新する', html)
        self.assertIn('履歴', html)
        self.assertNotIn('発信一覧', html)
        self.assertNotIn('<table', html)

    def test_home_shows_only_active_resident_evacuation_instructions(self):
        now = datetime.now(app_module.JST)
        app_module.instructions = [
            {
                'id': 'active', 'type': 'evacuation', 'target': '住民',
                'region_name': '発令中の区', 'content': '発令中の指示',
                'issue_start_at': (now - timedelta(minutes=5)).isoformat(),
                'issue_end_at': (now + timedelta(hours=1)).isoformat()
            },
            {
                'id': 'planned', 'type': 'evacuation', 'target': '住民',
                'region_name': '発令予定の区', 'content': '発令予定の指示',
                'issue_start_at': (now + timedelta(hours=1)).isoformat(),
                'issue_end_at': (now + timedelta(hours=2)).isoformat()
            },
            {
                'id': 'revoked', 'type': 'evacuation', 'target': '住民',
                'region_name': '解除済みの区', 'content': '解除済みの指示',
                'status': '解除済み',
                'issue_start_at': (now - timedelta(hours=1)).isoformat(),
                'issue_end_at': (now + timedelta(hours=1)).isoformat()
            },
            {
                'id': 'notice', 'target': '住民',
                'region_name': '通常のお知らせ', 'content': '通常のお知らせ内容'
            }
        ]

        html = self.client.get('/').get_data(as_text=True)

        self.assertIn('発令中の指示', html)
        self.assertNotIn('発令予定の指示', html)
        self.assertNotIn('解除済みの指示', html)
        self.assertNotIn('通常のお知らせ内容', html)

    def test_incomplete_or_cross_region_selection_does_not_save(self):
        response = self.post_board([self.areas[0]['id']], [])
        self.assertEqual(response.status_code, 400)
        self.assertIn('の避難先を一つ以上選択', response.get_data(as_text=True))
        self.assertFalse(self.instructions_path.exists())

        response = self.post_board(
            [self.areas[0]['id']],
            [self.areas[1]['shelters'][0]['id']]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('を選択してから避難先', response.get_data(as_text=True))
        self.assertFalse(self.instructions_path.exists())

        response = self.post_board(
            [self.areas[0]['id']], [self.areas[0]['shelters'][0]['id']],
            period_start='2026-09-29T12:00', period_end='2026-09-29T11:00'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('終了日時は開始日時より後', response.get_data(as_text=True))
        self.assertFalse(self.instructions_path.exists())

        response = self.post_board(
            [self.areas[0]['id']], [self.areas[0]['shelters'][0]['id']], reason=''
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('理由を選択', response.get_data(as_text=True))
        self.assertFalse(self.instructions_path.exists())

    def test_multiple_regions_save_matching_shelters_and_clear_selection_after_update(self):
        selected_regions = [self.areas[0], self.areas[1]]
        selected_shelters = [
            selected_regions[0]['shelters'][0],
            selected_regions[0]['shelters'][2],
            selected_regions[1]['shelters'][1]
        ]
        response = self.post_board(
            [area['id'] for area in selected_regions],
            [shelter['id'] for shelter in selected_shelters]
        )
        self.assertEqual(response.status_code, 302)

        saved = json.loads(self.instructions_path.read_text(encoding='utf-8'))
        self.assertEqual(saved[0], self.legacy_instruction)
        directives = [item for item in saved if item.get('type') == 'evacuation']
        self.assertEqual(len(directives), 2)
        expected_by_region = {
            selected_regions[0]['id']: [selected_shelters[0]['name'], selected_shelters[1]['name']],
            selected_regions[1]['id']: [selected_shelters[2]['name']]
        }
        for directive in directives:
            self.assertEqual(directive['status'], '発令中')
            self.assertEqual(directive['shelters'], expected_by_region[directive['region_id']])
            self.assertEqual(directive['reason'], '津波')
            self.assertTrue(directive['issue_start_at'])
            self.assertTrue(directive['issue_end_at'])
            self.assertEqual(directive['history'][0]['action'], '追加')
            self.assertEqual(directive['history'][0]['operator'], 'staff-test')

        refreshed = self.client.get('/board').get_data(as_text=True)
        self.assertNotIn('aria-pressed="true">', refreshed)
        self.assertIn('aria-pressed="false"', refreshed)
        self.assertNotIn('発信一覧', refreshed)

    def test_jma_instruction_reason_requires_current_official_notice(self):
        with patch.object(app_module, 'get_weather_warnings', return_value={
            'area_name': '青森市',
            'warnings': [{'code': '03', 'name': '大雨警報', 'status': '発表'}],
            'report_time': '2026年09月29日 09:00',
            'last_fetch_time': '2026年09月29日 09:05'
        }):
            response = self.post_board(
                [self.areas[0]['id']], [self.areas[0]['shelters'][0]['id']],
                reason='気象庁情報', weather_code='03'
            )
        self.assertEqual(response.status_code, 302)
        saved = json.loads(self.instructions_path.read_text(encoding='utf-8'))
        directive = next(item for item in saved if item.get('type') == 'evacuation')
        self.assertEqual(directive['reason'], '気象庁情報')
        self.assertEqual(directive['reason_details'], '大雨警報（発表）')
        self.assertEqual(directive['source'], '気象庁')

        other_area = self.areas[1]
        with patch.object(app_module, 'get_weather_warnings', return_value={
            'area_name': '青森市', 'warnings': [], 'report_time': '発表時刻'
        }):
            response = self.post_board(
                [other_area['id']], [other_area['shelters'][0]['id']],
                reason='気象庁情報', weather_code='03'
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn('対象の気象庁情報を確認できません', response.get_data(as_text=True))

    def test_revoke_keeps_audit_record_and_hides_directive_from_residents(self):
        area = self.areas[0]
        shelter = area['shelters'][0]
        response = self.post_board([area['id']], [shelter['id']])
        self.assertEqual(response.status_code, 302)
        directive = next(item for item in app_module.instructions if item.get('type') == 'evacuation')
        response = self.client.post(
            f"/board/instructions/{directive['id']}/revoke",
            data={'csrf_token': 'evacuation-test-token', 'confirm_revoke': 'yes'}
        )
        self.assertEqual(response.status_code, 302)

        stored = json.loads(self.instructions_path.read_text(encoding='utf-8'))
        revoked = next(item for item in stored if item.get('id') == directive['id'])
        self.assertEqual(revoked['status'], '解除済み')
        self.assertEqual([event['action'] for event in revoked['history']], ['追加', '解除'])
        self.assertTrue(revoked['revoked_at'])

        history_page = self.client.get('/board').get_data(as_text=True)
        self.assertIn('追加', history_page)
        self.assertIn('解除', history_page)
        self.assertNotIn('指示を解除', history_page)
        resident_page = self.client.get('/').get_data(as_text=True)
        self.assertNotIn(revoked['content'], resident_page)
        self.assertNotIn('既存のお知らせ', resident_page)

    def test_revoke_requires_csrf_and_confirmation(self):
        area = self.areas[0]
        self.post_board([area['id']], [area['shelters'][0]['id']])
        directive = next(item for item in app_module.instructions if item.get('type') == 'evacuation')
        url = f"/board/instructions/{directive['id']}/revoke"
        no_confirmation = self.client.post(url, data={
            'csrf_token': 'evacuation-test-token'
        })
        self.assertEqual(no_confirmation.status_code, 400)
        no_csrf = self.client.post(url, data={'confirm_revoke': 'yes'})
        self.assertEqual(no_csrf.status_code, 400)
        self.assertEqual(directive['status'], '発令中')


if __name__ == '__main__':
    unittest.main()