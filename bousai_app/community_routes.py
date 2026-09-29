"""Resident disaster-report routes and staff-only report view."""

from datetime import datetime
import hmac
import os
import uuid

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, session, url_for
)
from werkzeug.exceptions import RequestEntityTooLarge

from bousai_app import community_storage
from bousai_app.community_reports import (
    JST, REPORT_CATEGORIES, ReportInputError, current_report_datetime,
    parse_coordinates, parse_report_datetime, sanitize_uploaded_photo
)
from bousai_app.community_storage import (
    StorageConfigurationError, StorageOperationError
)


community = Blueprint('community', __name__)


@community.app_errorhandler(RequestEntityTooLarge)
def community_upload_too_large(_error):
    return _render_report_form(
        {'reported_at': current_report_datetime()},
        ['送信データが大きすぎます。写真は8MB以下にしてください。'],
        413
    )


def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        session['_csrf_token'] = token
    return token


def valid_csrf_token(value):
    expected = session.get('_csrf_token', '')
    return bool(expected and value and hmac.compare_digest(expected, value))


def _form_data():
    return {
        'reported_at': request.form.get('reported_at', ''),
        'category': request.form.get('category', ''),
        'place': '地図で選択した地点',
        'latitude': request.form.get('latitude', '').strip(),
        'longitude': request.form.get('longitude', '').strip(),
        'reporter': '',
        'comment': request.form.get('comment', '').strip()
    }


def _render_report_form(form_data, errors, status=200):
    return render_template(
        'community_report_form.html',
        form_data=form_data,
        categories=REPORT_CATEGORIES,
        errors=errors
    ), status


def _photo_url(photo_key):
    if not photo_key:
        return None
    remote_url = community_storage.photo_public_url(photo_key)
    if remote_url:
        return remote_url
    return url_for('.community_report_photo', photo_key=photo_key)


def _public_report(record):
    return {
        'id': record['id'],
        'reported_at': record['reported_at'],
        'category': record['category'],
        'place': record['place'],
        'latitude': record.get('latitude'),
        'longitude': record.get('longitude'),
        'comment': record.get('comment', ''),
        'photo_url': _photo_url(record.get('photo_key'))
    }


@community.route('/community-report/new', methods=['GET', 'POST'])
def community_report_new():
    if request.method == 'GET':
        return _render_report_form({'reported_at': current_report_datetime()}, [])

    form_data = _form_data()
    errors = []
    if not valid_csrf_token(request.form.get('csrf_token')):
        abort(400, description='フォームの有効期限が切れました。ページを再読み込みしてください。')

    try:
        reported_at = parse_report_datetime(form_data['reported_at'])
    except ReportInputError as error:
        errors.append(str(error))

    category = form_data['category']
    if category not in REPORT_CATEGORIES:
        errors.append('災害の種類を選択してください。')

    comment = form_data['comment']
    if len(comment) > 4000:
        errors.append('コメントは4000文字以内で入力してください。')

    try:
        latitude, longitude = parse_coordinates(
            form_data['latitude'], form_data['longitude']
        )
    except ReportInputError as error:
        errors.append(str(error))

    photo_upload = request.files.get('photo')
    try:
        photo_content = sanitize_uploaded_photo(photo_upload)
    except ReportInputError as error:
        errors.append(str(error))
        photo_content = None
    finally:
        if photo_upload is not None:
            photo_upload.close()

    if errors:
        return _render_report_form(form_data, errors, 400)

    now = datetime.now(JST).isoformat(timespec='seconds')
    report = {
        'id': uuid.uuid4().hex,
        'reported_at': reported_at,
        'category': category,
        'place': form_data['place'],
        'latitude': latitude,
        'longitude': longitude,
        'reporter': '',
        'comment': comment,
        'photo_key': None,
        'created_at': now
    }
    saved_photo_key = None
    try:
        if photo_content is not None:
            saved_photo_key = f"{uuid.uuid4().hex}.png"
            community_storage.save_photo(saved_photo_key, photo_content)
            report['photo_key'] = saved_photo_key
        community_storage.create_report(report)
    except (StorageConfigurationError, StorageOperationError, OSError):
        if saved_photo_key:
            try:
                community_storage.delete_photo(saved_photo_key)
            except (StorageConfigurationError, StorageOperationError, OSError):
                current_app.logger.warning('Unable to clean up an unreferenced report photo')
        return _render_report_form(
            form_data,
            ['現在、通報を保存できません。ストレージ設定を確認して再度お試しください。'],
            503
        )

    flash('通報を受け付けました。ご協力ありがとうございます。', 'success')
    return redirect(url_for('community.community_reports'))


@community.route('/community-reports')
def community_reports():
    try:
        records = community_storage.list_reports(include_private=False)
        public_reports = [_public_report(record) for record in records]
    except (StorageConfigurationError, StorageOperationError, OSError):
        return render_template(
            'community_reports.html', reports=[], storage_error=True
        ), 503

    return render_template(
        'community_reports.html',
        reports=public_reports,
        storage_error=False
    )


@community.route('/community-reports/photos/<photo_key>')
def community_report_photo(photo_key):
    path = community_storage.local_photo_path(photo_key)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='image/png', conditional=True, max_age=3600)


@community.route('/staff/community-reports')
def staff_community_reports():
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.url))
    try:
        records = community_storage.list_reports(include_private=True)
    except (StorageConfigurationError, StorageOperationError, OSError):
        return render_template('staff_community_reports.html', reports=[], error=True), 503
    return render_template('staff_community_reports.html', reports=records, error=False)