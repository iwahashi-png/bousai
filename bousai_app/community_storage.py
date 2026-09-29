"""Persistent storage adapters for resident-submitted disaster reports."""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request


class StorageConfigurationError(RuntimeError):
    pass


class StorageOperationError(RuntimeError):
    pass


REPORT_FIELDS = (
    'id', 'reported_at', 'category', 'place', 'latitude', 'longitude',
    'reporter', 'comment', 'photo_key', 'created_at'
)
PUBLIC_REPORT_FIELDS = tuple(field for field in REPORT_FIELDS if field != 'reporter')
PHOTO_KEY_PATTERN = re.compile(r'^[a-f0-9]{32}\.png$')


def storage_mode():
    if os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV'):
        return 'supabase'
    mode = os.environ.get('COMMUNITY_STORAGE_MODE', 'sqlite').lower()
    if mode not in ('sqlite', 'supabase'):
        raise StorageConfigurationError('COMMUNITY_STORAGE_MODE must be sqlite or supabase')
    return mode


def _sqlite_database_path():
    return os.environ.get(
        'COMMUNITY_REPORTS_DB_PATH',
        os.path.join(os.path.dirname(__file__), 'data', 'community_reports.sqlite3')
    )


def _sqlite_upload_directory():
    return os.environ.get(
        'COMMUNITY_REPORTS_UPLOAD_DIR',
        os.path.join(os.path.dirname(__file__), 'uploads', 'community-reports')
    )


def _supabase_config():
    base_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    service_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base_url or not service_key:
        raise StorageConfigurationError(
            'SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for persistent report storage'
        )
    return {
        'base_url': base_url,
        'service_key': service_key,
        'table': os.environ.get('SUPABASE_REPORTS_TABLE', 'community_reports'),
        'bucket': os.environ.get('SUPABASE_REPORT_PHOTO_BUCKET', 'community-report-photos')
    }


@contextmanager
def _sqlite_connection():
    if os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV'):
        raise StorageConfigurationError('SQLite is local-development storage only; configure Supabase on Vercel')

    database_path = os.path.abspath(_sqlite_database_path())
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode = WAL')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS community_reports (
            id TEXT PRIMARY KEY,
            reported_at TEXT NOT NULL,
            category TEXT NOT NULL,
            place TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            reporter TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            photo_key TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    connection.commit()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _supabase_request(method, resource, payload=None, query=None, prefer=None, content_type='application/json'):
    config = _supabase_config()
    url = f"{config['base_url']}{resource}"
    if query:
        url = f'{url}?{urllib.parse.urlencode(query)}'

    headers = {
        'apikey': config['service_key'],
        'Authorization': f"Bearer {config['service_key']}",
        'Accept': 'application/json'
    }
    body = None
    if payload is not None:
        headers['Content-Type'] = content_type
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if prefer:
        headers['Prefer'] = prefer

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = response.read()
            if not response_body:
                return None
            return json.loads(response_body.decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise StorageOperationError(f'Supabase request failed with HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise StorageOperationError('Supabase storage is temporarily unavailable') from None


def create_report(report):
    values = {field: report.get(field) for field in REPORT_FIELDS}
    if storage_mode() == 'supabase':
        config = _supabase_config()
        rows = _supabase_request(
            'POST',
            f"/rest/v1/{urllib.parse.quote(config['table'], safe='')}",
            payload=values,
            prefer='return=representation'
        )
        if not isinstance(rows, list) or not rows:
            raise StorageOperationError('The report was not returned after insertion')
        return rows[0]

    with _sqlite_connection() as connection:
        connection.execute(
            '''INSERT INTO community_reports
               (id, reported_at, category, place, latitude, longitude, reporter, comment, photo_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            tuple(values[field] for field in REPORT_FIELDS)
        )
    return values


def list_reports(include_private=False):
    fields = REPORT_FIELDS if include_private else PUBLIC_REPORT_FIELDS
    if storage_mode() == 'supabase':
        config = _supabase_config()
        result = _supabase_request(
            'GET',
            f"/rest/v1/{urllib.parse.quote(config['table'], safe='')}",
            query={'select': ','.join(fields), 'order': 'reported_at.desc,created_at.desc'}
        )
        if not isinstance(result, list):
            raise StorageOperationError('Unexpected report data from Supabase')
        return result

    columns = ', '.join(fields)
    with _sqlite_connection() as connection:
        rows = connection.execute(
            f'SELECT {columns} FROM community_reports ORDER BY reported_at DESC, created_at DESC'
        ).fetchall()
    return [dict(row) for row in rows]


def save_photo(object_key, content):
    if not PHOTO_KEY_PATTERN.fullmatch(object_key):
        raise StorageOperationError('Invalid generated photo key')

    if storage_mode() == 'supabase':
        config = _supabase_config()
        bucket = urllib.parse.quote(config['bucket'], safe='')
        key = urllib.parse.quote(object_key, safe='')
        _supabase_request(
            'POST',
            f'/storage/v1/object/{bucket}/{key}',
            payload=content,
            content_type='image/png'
        )
        return object_key

    upload_directory = os.path.abspath(_sqlite_upload_directory())
    os.makedirs(upload_directory, mode=0o700, exist_ok=True)
    path = os.path.join(upload_directory, object_key)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise StorageOperationError('Unable to save the report photo') from None
    return object_key


def delete_photo(object_key):
    if not PHOTO_KEY_PATTERN.fullmatch(object_key):
        return
    if storage_mode() == 'supabase':
        config = _supabase_config()
        bucket = urllib.parse.quote(config['bucket'], safe='')
        _supabase_request(
            'POST',
            f'/storage/v1/object/{bucket}',
            payload={'prefixes': [object_key]}
        )
        return
    try:
        os.unlink(os.path.join(os.path.abspath(_sqlite_upload_directory()), object_key))
    except FileNotFoundError:
        pass


def photo_public_url(object_key):
    if not PHOTO_KEY_PATTERN.fullmatch(object_key):
        raise StorageOperationError('Invalid stored photo key')
    if storage_mode() == 'supabase':
        config = _supabase_config()
        bucket = urllib.parse.quote(config['bucket'], safe='')
        key = urllib.parse.quote(object_key, safe='')
        return f"{config['base_url']}/storage/v1/object/public/{bucket}/{key}"
    return None


def local_photo_path(object_key):
    if storage_mode() != 'sqlite' or not PHOTO_KEY_PATTERN.fullmatch(object_key):
        return None
    return os.path.join(os.path.abspath(_sqlite_upload_directory()), object_key)