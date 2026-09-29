"""Input validation helpers for public community reports."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import math

from PIL import Image, ImageOps, UnidentifiedImageError


JST = timezone(timedelta(hours=9))
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 20_000_000
IMAGE_FORMAT_MIME_TYPES = {
    'JPEG': {'image/jpeg', 'image/pjpeg'},
    'PNG': {'image/png'},
    'WEBP': {'image/webp'}
}
REPORT_CATEGORIES = (
    '津波', '河川氾濫', '道路冠水', '道路寸断', '熊', 'その他'
)


class ReportInputError(ValueError):
    pass


def current_report_datetime():
    return datetime.now(JST).strftime('%Y/%m/%d %H:%M')


def parse_report_datetime(value):
    if not value:
        raise ReportInputError('通報日時を入力してください。')
    try:
        if len(value) != 16 or value[4] != '/' or value[7] != '/' or value[10] != ' ' or value[13] != ':':
            raise ValueError
        parsed = datetime.strptime(value, '%Y/%m/%d %H:%M')
    except ValueError:
        raise ReportInputError('通報日時は YYYY/MM/DD HH:MM 形式で入力してください。') from None
    parsed = parsed.replace(tzinfo=JST)
    return parsed.isoformat(timespec='seconds')


def parse_coordinates(latitude, longitude):
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        raise ReportInputError('地図上で場所を選択してください。') from None
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ReportInputError('地図上で有効な場所を選択してください。')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ReportInputError('緯度または経度が有効な範囲ではありません。')
    return lat, lon


def sanitize_uploaded_photo(upload):
    if upload is None or not upload.filename:
        return None

    content = upload.stream.read(MAX_PHOTO_BYTES + 1)
    if len(content) > MAX_PHOTO_BYTES:
        raise ReportInputError('写真は8MB以下にしてください。')
    if not content:
        raise ReportInputError('写真ファイルの内容を読み取れません。')

    claimed_mime_type = (upload.mimetype or '').split(';', 1)[0].strip().lower()
    try:
        with Image.open(BytesIO(content)) as image:
            actual_format = image.format
            width, height = image.size
            if actual_format not in IMAGE_FORMAT_MIME_TYPES:
                raise ReportInputError('JPG/JPEG、PNG、WebP形式の画像を選択してください。')
            if claimed_mime_type not in IMAGE_FORMAT_MIME_TYPES[actual_format]:
                raise ReportInputError('画像の形式を確認できません。別の画像を選択してください。')
            if width <= 0 or height <= 0 or width * height > MAX_PHOTO_PIXELS:
                raise ReportInputError('画像の大きさが上限を超えています。')
            image.verify()

        with Image.open(BytesIO(content)) as image:
            image.seek(0)
            normalized = ImageOps.exif_transpose(image).copy()
            has_alpha = normalized.mode in ('RGBA', 'LA') or 'transparency' in image.info
            normalized = normalized.convert('RGBA' if has_alpha else 'RGB')
            output = BytesIO()
            normalized.save(output, format='PNG', optimize=True)
            normalized.close()
    except ReportInputError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ReportInputError('有効な画像ファイルを選択してください。') from None

    sanitized = output.getvalue()
    if len(sanitized) > MAX_PHOTO_BYTES:
        raise ReportInputError('変換後の写真が8MBを超えています。小さい画像を選択してください。')
    return sanitized