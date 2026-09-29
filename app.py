from flask import Flask, abort, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

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

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
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
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template('index.html', resident_notices=resident_notices)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
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


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board')
@login_required
def board():
    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    return render_template('board.html', instructions=resident_instructions)

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

# 通報詳細のモックページ
@app.route('/reports/<int:report_id>')
def report_detail(report_id):
    report = next((item for item in reports if item.get('id') == report_id), None)
    if report is None:
        abort(404)
    return render_template(
        'report_detail.html',
        report=report,
        status_descriptions=REPORT_STATUSES,
        status_classes=REPORT_STATUS_CLASSES
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

        if selected_status not in REPORT_STATUSES:
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
        warning['instruction_url'] = url_for(
            'instruction_create_mock',
            code=warning.get('code', ''),
            name=warning.get('name', ''),
            warning_status=warning.get('status', ''),
            report_time=data.get('report_time', '')
        )
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
