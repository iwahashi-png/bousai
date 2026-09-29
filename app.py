from flask import Flask, abort, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import hmac
import json
import os
import secrets
import tempfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from bousai_app.community_routes import community
from bousai_app import community_storage

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
is_vercel = os.environ.get('VERCEL') == '1' or bool(os.environ.get('VERCEL_ENV'))
configured_secret_key = os.environ.get('FLASK_SECRET_KEY')
if is_vercel and not configured_secret_key:
    raise RuntimeError('FLASK_SECRET_KEY must be configured in the Vercel environment')
app.secret_key = configured_secret_key or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=is_vercel
)
app.register_blueprint(community)


def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def valid_csrf_token(value):
    expected = session.get('_csrf_token', '')
    return bool(expected and value and hmac.compare_digest(expected, value))


app.jinja_env.globals['csrf_token'] = csrf_token

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁の青森市区域コード
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
EVACUATION_AREAS_FILE = os.path.join(APP_DIR, 'data', 'evacuation_areas.json')
REPORTS_FILE = os.path.join(APP_DIR, 'data', 'reports.json')
WEATHER_STATUS_FILE = os.path.join(APP_DIR, 'data', 'weather_notice_statuses.json')
WEATHER_NOTICE_STATUSES = ('未対応', '勧告済み')
REPORT_STATUSES = {
    '未対応': '未着手',
    '対応中': '指示を検討中',
    '対応済み': '指示済み'
}
REPORT_STATUS_CLASSES = {
    '未対応': 'status-open',
    '対応中': 'status-progress',
    '対応済み': 'status-done'
}
INSTRUCTION_REASONS = (
    '地震', '津波', '河川氾濫', '土砂災害', '気象庁情報', 'その他'
)

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
evacuation_area_settings = load_json(EVACUATION_AREAS_FILE, [])
reports = load_json(REPORTS_FILE, [])
weather_notice_statuses = load_json(WEATHER_STATUS_FILE, {})
for report in reports:
    report.setdefault('response_status', '未対応')
    report.setdefault('response_history', [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_evacuation_areas():
    existing_shelter_ids = {
        shelter.get('name'): str(shelter.get('id'))
        for shelter in shelters
        if shelter.get('name') and shelter.get('id') is not None
    }
    areas = []
    for area_index, area_setting in enumerate(evacuation_area_settings, start=1):
        area_id = area_setting.get('id') or f'area-{area_index}'
        area_name = area_setting.get('name')
        shelter_names = area_setting.get('shelters', [])
        if not area_name or not isinstance(shelter_names, list):
            continue
        areas.append({
            'id': area_id,
            'name': area_name,
            'shelters': [
                {
                    'id': existing_shelter_ids.get(name, f'{area_id}-shelter-{shelter_index}'),
                    'name': name
                }
                for shelter_index, name in enumerate(shelter_names, start=1)
                if isinstance(name, str) and name
            ]
        })
    return areas


def save_instruction_records(records):
    directory = os.path.dirname(INSTRUCTIONS_FILE)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.instructions-', suffix='.tmp', dir=directory
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(records, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, INSTRUCTIONS_FILE)
    except OSError:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def parse_instruction_datetime(value, label):
    if not value:
        raise ValueError(f'{label}を選択してください。')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M').replace(tzinfo=JST)
    except (TypeError, ValueError):
        raise ValueError(f'{label}の形式が正しくありません。') from None
    return parsed


def instruction_period_status(instruction, now=None):
    if instruction.get('status') == '解除済み':
        return '解除済み'
    start_value = instruction.get('issue_start_at')
    end_value = instruction.get('issue_end_at')
    if not start_value or not end_value:
        return '期間未設定'
    try:
        start = datetime.fromisoformat(start_value)
        end = datetime.fromisoformat(end_value)
    except (TypeError, ValueError):
        return '期間未設定'
    now = now or datetime.now(JST)
    if now >= end:
        return '期間終了'
    if now < start:
        return '発令予定'
    return '発令中'


def get_current_weather_notice(code):
    if not code or code not in WARNING_CODES:
        return None
    weather_data = get_weather_warnings()
    if not isinstance(weather_data, dict) or weather_data.get('error'):
        return None
    warning = next((
        warning for warning in weather_data.get('warnings', [])
        if warning.get('code') == code and warning.get('status') in ('発表', '継続')
    ), None)
    if not warning:
        return None
    return {
        **warning,
        'area_name': weather_data.get('area_name', AREA_NAME),
        'report_time': weather_data.get('report_time', '不明'),
        'last_fetch_time': weather_data.get('last_fetch_time', get_japan_time())
    }
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def format_report_datetime(value):
    if not value:
        return '未登録'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        else:
            parsed = parsed.astimezone(JST)
        return parsed.strftime('%Y/%m/%d %H:%M')
    except ValueError:
        return str(value)


def report_id_sort_key(report):
    report_id = report.get('id')
    try:
        return (0, int(report_id))
    except (TypeError, ValueError):
        return (1, str(report_id or ''))


def report_level_display(report):
    level = report.get('disaster_level')
    if isinstance(level, bool):
        return {'label': '未登録', 'class_name': 'level-unknown'}
    if isinstance(level, int):
        level = int(level)
    elif isinstance(level, float) and level.is_integer():
        level = int(level)
    elif isinstance(level, str) and level.strip().isdecimal():
        level = int(level.strip())
    else:
        return {'label': '未登録', 'class_name': 'level-unknown'}
    if level not in range(1, 6):
        return {'label': '未登録', 'class_name': 'level-unknown'}
    return {'label': f'レベル{level}', 'class_name': f'level-{level}'}


def report_progress(report):
    steps = ('要対応', '対応中', '対応済み')
    current_status = report.get('response_status')
    status_to_index = {'未対応': 0, '要対応': 0, '対応中': 1, '対応済み': 2}
    current_index = status_to_index.get(current_status, 0)
    history = report.get('response_history')
    if not isinstance(history, list):
        history = []

    progress = []
    for index, status in enumerate(steps):
        if index > current_index:
            break
        history_statuses = ('未対応', '要対応') if index == 0 else (status,)
        timestamp = next((
            entry.get('changed_at')
            for entry in reversed(history)
            if isinstance(entry, dict)
            and entry.get('status') in history_statuses
            and entry.get('changed_at')
        ), None)
        progress.append({
            'status': status,
            'index': index + 1,
            'is_current': index == current_index,
            'time': format_report_datetime(timestamp) if timestamp else '時刻未記録'
        })
    return progress


def report_photo_url(report):
    photo_key = report.get('photo_key')
    if not photo_key:
        return None
    try:
        external_url = community_storage.photo_public_url(photo_key)
        if external_url:
            return external_url
        local_path = community_storage.local_photo_path(photo_key)
        if local_path and os.path.isfile(local_path):
            return url_for('community.community_report_photo', photo_key=photo_key)
    except Exception:
        return None
    return None


def weather_notice_key(code, report_time):
    return f'{report_time}|{code}'


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [
        item for item in instructions
        if item.get('target') == '住民'
        and (item.get('type') != 'evacuation' or instruction_period_status(item) == '発令中')
    ]
    return render_template('index.html', resident_notices=resident_notices)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next') or request.form.get('next')
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('board')

    if request.method == 'POST':
        if not valid_csrf_token(request.form.get('csrf_token')):
            return render_template(
                'login.html', error=True,
                message='フォームの有効期限が切れました。ページを再読み込みしてください。',
                next=next_url
            ), 400

        expected_username = os.environ.get('STAFF_USERNAME', '')
        expected_password = os.environ.get('STAFF_PASSWORD', '')
        if not expected_username or not expected_password:
            return render_template(
                'login.html', error=True,
                message='職員ログインが設定されていません。管理者にご連絡ください。',
                next=next_url
            ), 503

        username = request.form.get('username', '')
        password = request.form.get('password', '').strip()

        credentials_match = (
            hmac.compare_digest(username, expected_username)
            and hmac.compare_digest(password, expected_password)
        )
        if credentials_match:
            session.clear()
            session['logged_in'] = True
            session['username'] = username
            return redirect(next_url)
        return render_template(
            'login.html', error=True,
            message='ユーザー名またはパスワードが正しくありません。',
            next=next_url
        )

    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            return render_template(
                'shelter_register.html', error=True,
                message='避難所名を入力してください。'
            )

        existing_ids = [
            shelter.get('id', 0) for shelter in shelters
            if isinstance(shelter.get('id'), int)
        ]
        new_shelter = {'id': max(existing_ids, default=0) + 1, 'name': name}
        updated_shelters = shelters + [new_shelter]
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(updated_shelters, f, ensure_ascii=False, indent=2)
        except OSError:
            return render_template(
                'shelter_register.html', error=True,
                message='避難所情報を保存できませんでした。'
            )

        shelters.append(new_shelter)
        return render_template(
            'shelter_register.html', success=True,
            message='避難所情報を登録しました。'
        )

    return render_template('shelter_register.html')

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


def evacuation_board_context(errors=None, weather_notice=None, weather_error=None):
    areas = get_evacuation_areas()
    draft = session.get('evacuation_draft', {})
    selected_regions = draft.get('region_ids', [])
    selected_shelters = draft.get('shelter_ids', [])
    if not isinstance(selected_regions, list):
        selected_regions = []
    if not isinstance(selected_shelters, list):
        selected_shelters = []
    now = datetime.now(JST)
    period_start = draft.get('period_start') or now.strftime('%Y-%m-%dT%H:%M')
    period_end = draft.get('period_end') or (now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M')
    selected_reason = draft.get('reason') or ('気象庁情報' if weather_notice else '')
    reason_details = draft.get('reason_details', '')
    if weather_notice and not reason_details:
        reason_details = f"{weather_notice['name']}（{weather_notice['status']}）"

    history_events = []
    for instruction in instructions:
        if instruction.get('type') != 'evacuation':
            continue
        instruction_history = instruction.get('history', [])
        if not isinstance(instruction_history, list):
            continue
        for event in instruction_history:
            if isinstance(event, dict):
                history_events.append({
                    **event,
                    'instruction_id': instruction.get('id'),
                    'instruction_status': instruction_period_status(instruction),
                    'issue_start_display': instruction.get('issue_start_display', '未登録'),
                    'issue_end_display': instruction.get('issue_end_display', '未登録'),
                    'reason': instruction.get('reason', '未登録'),
                    'reason_details': instruction.get('reason_details', '')
                })
    history_events.sort(key=lambda event: event.get('timestamp', ''), reverse=True)

    return render_template(
        'board.html',
        evacuation_areas=areas,
        selected_regions=selected_regions,
        selected_shelters=selected_shelters,
        has_server_draft='evacuation_draft' in session,
        period_start=period_start,
        period_end=period_end,
        selected_reason=selected_reason,
        reason_details=reason_details,
        weather_code=draft.get('weather_code') or (weather_notice.get('code', '') if weather_notice else ''),
        weather_notice=weather_notice,
        weather_error=weather_error,
        instruction_reasons=INSTRUCTION_REASONS,
        history_events=history_events,
        errors=errors or []
    )


@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    if request.method == 'GET':
        weather_code = request.args.get('weather_code', '')
        weather_notice = get_current_weather_notice(weather_code) if weather_code else None
        weather_error = bool(weather_code and not weather_notice)
        return evacuation_board_context(
            weather_notice=weather_notice,
            weather_error=weather_error
        )

    if not valid_csrf_token(request.form.get('csrf_token')):
        abort(400, description='フォームの有効期限が切れました。ページを再読み込みしてください。')

    areas = get_evacuation_areas()
    areas_by_id = {area['id']: area for area in areas}
    shelters_by_id = {
        shelter['id']: (area['id'], shelter)
        for area in areas
        for shelter in area['shelters']
    }
    requested_regions = request.form.getlist('region_ids')
    requested_shelters = request.form.getlist('shelter_ids')
    period_start_value = request.form.get('period_start', '')
    period_end_value = request.form.get('period_end', '')
    reason = request.form.get('reason', '')
    reason_details = request.form.get('reason_details', '').strip()
    weather_code = request.form.get('weather_code', '')
    valid_regions = list(dict.fromkeys(
        region_id for region_id in requested_regions if region_id in areas_by_id
    ))
    valid_shelters = []
    shelters_by_region = defaultdict(list)
    errors = []
    weather_notice = get_current_weather_notice(weather_code) if weather_code else None
    weather_error = bool(weather_code and not weather_notice)

    try:
        period_start = parse_instruction_datetime(period_start_value, '発令開始日時')
    except ValueError as error:
        errors.append(str(error))
        period_start = None
    try:
        period_end = parse_instruction_datetime(period_end_value, '発令終了日時')
    except ValueError as error:
        errors.append(str(error))
        period_end = None
    if period_start and period_end:
        if period_start >= period_end:
            errors.append('発令終了日時は開始日時より後にしてください。')
        if period_end <= datetime.now(JST):
            errors.append('発令終了日時は現在より後にしてください。')

    if reason not in INSTRUCTION_REASONS:
        errors.append('指示の理由を選択してください。')
    if weather_code:
        if reason != '気象庁情報':
            errors.append('気象庁情報から発信する場合は、理由を気象庁情報にしてください。')
        if weather_notice is None:
            errors.append('対象の気象庁情報を確認できません。警報一覧から選び直してください。')
        else:
            reason_details = f"{weather_notice['name']}（{weather_notice['status']}）"
    elif reason == '気象庁情報':
        errors.append('気象庁情報から発信する場合は、警報一覧の作成ボタンから開始してください。')
    if len(reason_details) > 500:
        errors.append('理由の補足は500文字以内で入力してください。')

    if not valid_regions:
        errors.append('避難地域を一つ以上選択してください。')
    if any(region_id not in areas_by_id for region_id in requested_regions):
        errors.append('不明な避難地域が含まれています。画面を再読み込みしてください。')

    for shelter_id in dict.fromkeys(requested_shelters):
        shelter_info = shelters_by_id.get(shelter_id)
        if shelter_info is None:
            errors.append('不明な避難先が含まれています。画面を再読み込みしてください。')
            continue
        region_id, shelter = shelter_info
        if region_id not in valid_regions:
            errors.append(f'{areas_by_id[region_id]["name"]}を選択してから避難先を指定してください。')
            continue
        valid_shelters.append(shelter_id)
        shelters_by_region[region_id].append(shelter)

    for region_id in valid_regions:
        if not shelters_by_region[region_id]:
            errors.append(f'{areas_by_id[region_id]["name"]}の避難先を一つ以上選択してください。')

    session['evacuation_draft'] = {
        'region_ids': valid_regions,
        'shelter_ids': valid_shelters,
        'period_start': period_start_value,
        'period_end': period_end_value,
        'reason': reason,
        'reason_details': reason_details,
        'weather_code': weather_code
    }

    if errors:
        return evacuation_board_context(
            errors,
            weather_notice=weather_notice,
            weather_error=weather_error
        ), 400

    timestamp = datetime.now(JST)
    timestamp_iso = timestamp.isoformat(timespec='seconds')
    timestamp_display = timestamp.strftime('%Y年%m月%d日 %H:%M')
    issue_start_iso = period_start.isoformat(timespec='seconds')
    issue_end_iso = period_end.isoformat(timespec='seconds')
    issue_start_display = period_start.strftime('%Y年%m月%d日 %H:%M')
    issue_end_display = period_end.strftime('%Y年%m月%d日 %H:%M')
    instruction_status = '発令中' if timestamp >= period_start else '発令予定'
    updated_instructions = list(instructions)

    for region_id in valid_regions:
        area = areas_by_id[region_id]
        selected_names = [shelter['name'] for shelter in shelters_by_region[region_id]]
        record_id = secrets.token_hex(12)
        content = f'【{reason}】{area["name"]}の住民は、{ "、".join(selected_names) }へ避難してください。'
        if reason_details:
            content += f' 根拠: {reason_details}'
        event = {
            'action': '追加',
            'region_id': region_id,
            'region_name': area['name'],
            'shelters': selected_names,
            'reason': reason,
            'reason_details': reason_details,
            'issue_start_at': issue_start_iso,
            'issue_end_at': issue_end_iso,
            'issue_start_display': issue_start_display,
            'issue_end_display': issue_end_display,
            'timestamp': timestamp_iso,
            'timestamp_display': timestamp_display,
            'operator': session.get('username', '担当者')
        }
        updated_instructions.append({
            'id': record_id,
            'type': 'evacuation',
            'target': '住民',
            'region_id': region_id,
            'region_name': area['name'],
            'shelter_ids': [shelter['id'] for shelter in shelters_by_region[region_id]],
            'shelters': selected_names,
            'reason': reason,
            'reason_details': reason_details,
            'source': '気象庁' if weather_notice else '職員作成',
            'weather_code': weather_code or None,
            'issue_start_at': issue_start_iso,
            'issue_end_at': issue_end_iso,
            'issue_start_display': issue_start_display,
            'issue_end_display': issue_end_display,
            'content': content,
            'status': instruction_status,
            'created_at': timestamp_display,
            'created_at_iso': timestamp_iso,
            'created_by': session.get('username', '担当者'),
            'history': [event]
        })

    try:
        save_instruction_records(updated_instructions)
    except OSError:
        return evacuation_board_context(['保存に失敗しました。選択内容を確認して再度お試しください。']), 500

    instructions[:] = updated_instructions
    session['evacuation_draft'] = {
        'region_ids': valid_regions,
        'shelter_ids': valid_shelters,
        'period_start': period_start_value,
        'period_end': period_end_value,
        'reason': reason,
        'reason_details': reason_details,
        'weather_code': weather_code
    }
    return redirect(url_for('board'))


@app.route('/board/instructions/<instruction_id>/revoke', methods=['POST'])
@login_required
def revoke_evacuation_instruction(instruction_id):
    if not valid_csrf_token(request.form.get('csrf_token')):
        abort(400, description='フォームの有効期限が切れました。ページを再読み込みしてください。')
    if request.form.get('confirm_revoke') != 'yes':
        abort(400)

    instruction_index = next((
        index for index, item in enumerate(instructions)
        if item.get('type') == 'evacuation'
        and str(item.get('id')) == instruction_id
        and instruction_period_status(item) in ('発令中', '発令予定')
    ), None)
    if instruction_index is None:
        abort(404)

    timestamp = datetime.now(JST)
    timestamp_iso = timestamp.isoformat(timespec='seconds')
    timestamp_display = timestamp.strftime('%Y年%m月%d日 %H:%M')
    updated_instructions = list(instructions)
    revoked = dict(updated_instructions[instruction_index])
    revoked['status'] = '解除済み'
    revoked['revoked_at'] = timestamp_display
    revoked['revoked_at_iso'] = timestamp_iso
    revoked['history'] = list(revoked.get('history', [])) + [{
        'action': '解除',
        'region_id': revoked.get('region_id'),
        'region_name': revoked.get('region_name'),
        'shelters': list(revoked.get('shelters', [])),
        'timestamp': timestamp_iso,
        'timestamp_display': timestamp_display,
        'operator': session.get('username', '担当者')
    }]
    updated_instructions[instruction_index] = revoked

    try:
        save_instruction_records(updated_instructions)
    except OSError:
        return evacuation_board_context(['解除の保存に失敗しました。時間をおいて再度お試しください。']), 500

    instructions[:] = updated_instructions
    return redirect(url_for('board'))

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# 住民などから届いた災害通報の一覧
@app.route('/reports')
def report_list():
    location_types = ['道路', '河川', '避難所']
    location_type = request.args.get('location_type', '')
    response_status = request.args.get('response_status', '')
    sort_order = request.args.get('sort', 'newest')

    if location_type not in location_types:
        location_type = ''
    if response_status not in REPORT_STATUSES:
        response_status = ''
    if sort_order not in ('newest', 'oldest'):
        sort_order = 'newest'

    filtered_reports = [
        report for report in reports
        if (not location_type or report.get('response_location_type') == location_type)
        and (not response_status or report.get('response_status', '未対応') == response_status)
    ]
    filtered_reports.sort(
        key=lambda report: report.get('occurred_at', ''),
        reverse=sort_order == 'newest'
    )

    return render_template(
        'report_list.html',
        reports=filtered_reports,
        total_count=len(reports),
        location_types=location_types,
        response_statuses=REPORT_STATUSES,
        status_classes=REPORT_STATUS_CLASSES,
        selected_location_type=location_type,
        selected_response_status=response_status,
        sort_order=sort_order
    )

# 職員用通報詳細
@app.route('/report_detail', defaults={'report_id': None})
@app.route('/report_detail/<int:report_id>')
@app.route('/reports/<int:report_id>')
@login_required
def report_detail(report_id=None):
    ordered_reports = sorted(reports, key=report_id_sort_key)
    if not ordered_reports:
        return render_template(
            'report_detail.html',
            report=None,
            report_count=0,
            report_not_found=False,
            weather_data=None
        )

    if report_id is None:
        current_index = 0
    else:
        current_index = next(
            (index for index, item in enumerate(ordered_reports)
             if str(item.get('id')) == str(report_id)),
            None
        )
        if current_index is None:
            return render_template(
                'report_detail.html',
                report=None,
                report_count=len(ordered_reports),
                report_not_found=True,
                weather_data=None
            ), 404

    report = ordered_reports[current_index]
    try:
        weather_data = get_weather_warnings()
        if not isinstance(weather_data, dict):
            raise ValueError('Invalid weather response')
    except Exception:
        weather_data = {
            'area_name': AREA_NAME,
            'warnings': [],
            'report_time': '不明',
            'last_fetch_time': get_japan_time(),
            'error': True
        }

    return render_template(
        'report_detail.html',
        report=report,
        report_count=len(ordered_reports),
        current_index=current_index,
        previous_report=ordered_reports[current_index - 1] if current_index > 0 else None,
        next_report=ordered_reports[current_index + 1] if current_index + 1 < len(ordered_reports) else None,
        report_not_found=False,
        level_display=report_level_display(report),
        progress_steps=report_progress(report),
        occurred_at_display=format_report_datetime(report.get('occurred_at')),
        response_status_label=report.get('response_status') or '未登録',
        response_status_description=REPORT_STATUSES.get(report.get('response_status')),
        status_classes=REPORT_STATUS_CLASSES,
        weather_data=weather_data,
        photo_url=report_photo_url(report)
    )

# 職員による通報の対応状況更新
@app.route('/reports/<int:report_id>/respond', methods=['GET', 'POST'])
@login_required
def report_respond(report_id):
    report_index = next(
        (index for index, item in enumerate(reports) if item.get('id') == report_id),
        None
    )
    if report_index is None:
        abort(404)

    report = reports[report_index]
    current_status = report.get('response_status', '未対応')
    selected_status = current_status
    comment = ''
    error = None

    if request.method == 'POST':
        selected_status = request.form.get('response_status', '')
        comment = request.form.get('comment', '').strip()

        if not valid_csrf_token(request.form.get('csrf_token')):
            error = 'フォームの有効期限が切れました。ページを再読み込みしてください。'
        elif selected_status not in REPORT_STATUSES:
            error = '有効な対応状況を選択してください。'
        elif selected_status == current_status:
            error = '現在とは異なる対応状況を選択してください。'
        elif not comment:
            error = '変更理由や対応内容をコメントに入力してください。'
        else:
            updated_report = dict(report)
            updated_report['response_status'] = selected_status
            updated_report['response_history'] = list(report.get('response_history', [])) + [{
                'status': selected_status,
                'comment': comment,
                'changed_at': get_japan_time(),
                'changed_by': session.get('username', '職員')
            }]
            updated_reports = list(reports)
            updated_reports[report_index] = updated_report

            try:
                with open(REPORTS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(updated_reports, f, ensure_ascii=False, indent=2)
            except OSError:
                error = '対応状況を保存できませんでした。時間をおいて再度お試しください。'
            else:
                reports[:] = updated_reports
                return redirect(url_for('report_detail', report_id=report_id))

    return render_template(
        'report_respond.html',
        report=report,
        response_statuses=REPORT_STATUSES,
        status_classes=REPORT_STATUS_CLASSES,
        selected_status=selected_status,
        comment=comment,
        error=error
    )

# 気象情報から指示を作成する画面の最小モック
@app.route('/instructions/create', methods=['GET', 'POST'])
@login_required
def instruction_create_mock():
    code = request.values.get('code', '')
    name = request.values.get('name', '').strip()
    warning_status = request.values.get('warning_status', '')
    report_time = request.values.get('report_time', '')

    if code not in WARNING_CODES or not name or not report_time:
        abort(400)

    notice_key = weather_notice_key(code, report_time)
    error = None
    if request.method == 'POST':
        if not valid_csrf_token(request.form.get('csrf_token')):
            error = 'フォームの有効期限が切れました。ページを再読み込みしてください。'
        else:
            updated_statuses = dict(weather_notice_statuses)
            updated_statuses[notice_key] = '勧告済み'
            try:
                with open(WEATHER_STATUS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(updated_statuses, f, ensure_ascii=False, indent=2)
            except OSError:
                error = '状態を保存できませんでした。時間をおいて再度お試しください。'
            else:
                weather_notice_statuses.clear()
                weather_notice_statuses.update(updated_statuses)
                return redirect(url_for('report_list'))

    return render_template(
        'instruction_create_mock.html',
        notice={
            'code': code,
            'name': name,
            'warning_status': warning_status,
            'report_time': report_time
        },
        response_status=weather_notice_statuses.get(notice_key, '未対応'),
        error=error
    )

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    data = get_weather_warnings()
    for warning in data.get('warnings', []):
        key = weather_notice_key(warning.get('code', ''), data.get('report_time', ''))
        warning['response_status'] = weather_notice_statuses.get(key, '未対応')
        warning['instruction_url'] = url_for('board', weather_code=warning.get('code', ''))
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
