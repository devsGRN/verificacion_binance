import re
import threading
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ReceiptError(ValueError):
    pass


CURRENCIES = r'USDT|USDC|FDUSD|BNB|BTC|ETH'
# El OCR a veces pega la fecha y la hora ("2025-12-2418:38:39"), por eso el espacio es opcional.
DATE_RE = re.compile(r'(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})\s*([0-9]{2}:[0-9]{2}:[0-9]{2})(?![0-9])')
# "ID de la orden", "ID de orden", "N.º de la orden", "Nro. de orden", "Order ID", "Order No."
ORDER_LABEL = r'(?:(?:ID|N\.?\s*[º°o]?\.?|Nro\.?|N[úu]mero)\s*de\s*(?:la\s*)?orden|Order\s*(?:ID|No\.?))'
ORDER_RE = re.compile(r'\b' + ORDER_LABEL + r'\s*:?\s*([0-9]{12,32})(?![0-9])', re.I)
LONG_NUMBER_RE = re.compile(r'(?<![0-9])([0-9]{15,24})(?![0-9])')
AMOUNT_RE = re.compile(
    r'(?<![0-9.,])([+-]?)\s*([0-9]{1,3}(?:[.,][0-9]{3})+(?:[.,][0-9]+)?|[0-9]+(?:[.,][0-9]+)?)\s*(' + CURRENCIES + r')\b',
    re.I,
)
PAYMENT_RE = re.compile(
    r'\b(?:Detalles?\s*del\s*pago|Payment\s*Details|Pago\s*(?:exitoso|completado|enviado|recibido)'
    r'|Payment\s*(?:successful|completed|sent|received)|Transferencia\s*exitosa'
    r'|' + ORDER_LABEL + r')', re.I,
)


def _normalize_amount(raw):
    """'1,000.50' -> '1000.50'; '10,5' -> '10.5'; '1.000' -> '1000'."""
    if ',' in raw and '.' in raw:
        decimal_sep = ',' if raw.rfind(',') > raw.rfind('.') else '.'
        thousands = '.' if decimal_sep == ',' else ','
        raw = raw.replace(thousands, '').replace(decimal_sep, '.')
    elif ',' in raw or '.' in raw:
        sep = ',' if ',' in raw else '.'
        parts = raw.split(sep)
        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3 and sep == ','):
            raw = raw.replace(sep, '')
        else:
            raw = raw.replace(',', '.')
    return raw


def _find_amounts(text):
    found = [(sign, _normalize_amount(value), currency.upper())
             for sign, value, currency in AMOUNT_RE.findall(text)]
    # Preferir el importe con signo (+10 USDT), que es el monto principal del detalle.
    signed = {(value, currency) for sign, value, currency in found if sign == '+'}
    return signed or {(value, currency) for _sign, value, currency in found}


def _find_orders(text):
    orders = set(ORDER_RE.findall(text))
    if orders:
        return orders
    # Respaldo: el OCR puede desordenar las líneas; usar el único número largo de la captura.
    without_dates = DATE_RE.sub(' ', text)
    return set(LONG_NUMBER_RE.findall(without_dates))


# --- Depósitos por red (retiro de quien paga / depósito de quien recibe) -----------------
DEPOSIT_SCREEN_RE = re.compile(
    r'Detalles?\s*de\s*(?:Dep[oó]sito|Retiro)|Deposit\s*Details|Withdraw(?:al)?\s*Details'
    r'|\bTx\s*id\b|Transferencia\s*fuera\s*de\s*la\s*cadena|Off[\s-]*chain', re.I)
IN_PROCESS_RE = re.compile(r'Retiro\s*en\s*proceso|Withdrawal\s*(?:processing|in\s*progress)', re.I)
OFFCHAIN_RE = re.compile(
    r'(?:fuera\s*de\s*la\s*cadena|off[\s-]*chain\s*transfer|internal\s*transfer)', re.I)


def _offchain_number(text):
    """Número de la "Transferencia fuera de la cadena". Binance a veces lo parte en dos
    líneas ("… cadena 430" / "007444166") y el OCR puede leer el icono de copiar en medio:
    se unen los grupos de dígitos seguidos, hasta la primera palabra o importe."""
    match = OFFCHAIN_RE.search(text)
    if not match:
        return None
    digits = ''
    for token in re.split(r'\s+', text[match.end():match.end() + 80].strip()):
        clean = re.sub(r'[^0-9A-Za-z]', '', token)
        if not clean:
            continue  # icono o signo suelto
        if clean.isdigit():
            digits += clean
            continue
        if len(clean) == 1:
            continue  # el icono de copiar leído como una letra
        if re.fullmatch(CURRENCIES, clean, re.I) and digits:
            return None  # el último número era un importe ("33 USDT"), no parte del Txid
        break
    return digits if 9 <= len(digits) <= 32 else None
HEX_LINE_RE = re.compile(r'^(0x)?[0-9a-f]+$', re.I)
NETWORK_RE = re.compile(
    r'\b(TRX|TRC20|TRON|BSC|BEP20|ETH|ERC20|SOL|MATIC|POLYGON|ARBITRUM|OPBNB|TON|AVAXC|BASE|OPTIMISM)\b', re.I)
NETWORK_ALIASES = {'TRC20': 'TRX', 'TRON': 'TRX', 'BEP20': 'BSC', 'ERC20': 'ETH', 'POLYGON': 'MATIC'}


def normalize_network(name):
    name = (name or '').strip().upper()
    return NETWORK_ALIASES.get(name, name)


def _find_txid(text):
    """Txid de la captura: 'fuera de la cadena 123…' o un hash hexadecimal (que el OCR suele
    partir en varias líneas). La dirección (0x + 40) no se confunde con el hash (0x + 64)."""
    offchain = _offchain_number(text)
    if offchain:
        return 'offchain-%s' % offchain
    runs, current = [], ''
    for line in text.splitlines():
        line = re.sub(r'\s+', '', line)
        if line and HEX_LINE_RE.match(line) and len(line) >= 6:
            if line.lower().startswith('0x') and current:
                runs.append(current)
                current = ''
            current += line
        else:
            if current:
                runs.append(current)
            current = ''
    if current:
        runs.append(current)
    hashes = {run.lower() for run in runs
              if re.fullmatch(r'0x[0-9a-f]{64}', run, re.I) or re.fullmatch(r'[0-9a-f]{64}', run, re.I)}
    return hashes.pop() if len(hashes) == 1 else None


def extract_deposit(text, timezone_name):
    """None si la captura no es de un depósito/retiro por red."""
    if not DEPOSIT_SCREEN_RE.search(text) and not IN_PROCESS_RE.search(text):
        return None
    txid = _find_txid(text)
    if not txid:
        return {'kind': 'deposit_in_process' if IN_PROCESS_RE.search(text) else 'deposit_unreadable'}
    found = [(sign, _normalize_amount(value), currency.upper())
             for sign, value, currency in AMOUNT_RE.findall(text)]
    signed = {(value, currency) for sign, value, currency in found if sign in '+-' and sign}
    amounts = signed or {(value, currency) for _sign, value, currency in found
                         if _normalize_amount(value) not in ('0', '0.0', '0.00')}
    if len(amounts) != 1:
        return {'kind': 'deposit_unreadable'}
    amount, currency = amounts.pop()
    networks = {normalize_network(item) for item in NETWORK_RE.findall(text)}
    operation_day = None
    dates = sorted(set(DATE_RE.findall(text)))
    if dates:
        day, clock_time = dates[0]
        try:
            local_time = datetime.fromisoformat(f'{day}T{clock_time}').replace(tzinfo=ZoneInfo(timezone_name))
            operation_day = local_time.astimezone(timezone.utc).date()
        except (ValueError, ZoneInfoNotFoundError):
            operation_day = None
    return {
        'kind': 'deposit',
        'reference': txid,
        'operation_day': operation_day,
        'currency': currency,
        'expected_amount': amount.lstrip('+-'),
        'network': networks.pop() if len(networks) == 1 else '',
    }


def extract_receipt(text, timezone_name, *, allow_missing_reference=False):
    orders = _find_orders(text)
    dates = set(DATE_RE.findall(text))
    amounts = _find_amounts(text)
    is_payment = PAYMENT_RE.search(text)
    # Binance cambia el texto de sus pantallas; si hay un ID largo o un monto, se trata como pago.
    can_request_reference = allow_missing_reference and (is_payment or orders or amounts)

    if not can_request_reference and (len(dates) != 1 or len(amounts) != 1 or len(orders) != 1):
        raise ReceiptError('No pude leer un único ID de orden, fecha e importe. Usa una captura completa del detalle de Binance Pay.')
    operation_day = None
    if len(dates) == 1:
        day, clock_time = dates.pop()
        try:
            local_time = datetime.fromisoformat(f'{day}T{clock_time}').replace(tzinfo=ZoneInfo(timezone_name))
            operation_day = local_time.astimezone(timezone.utc).date()
        except (ValueError, ZoneInfoNotFoundError) as error:
            if not can_request_reference:
                raise ReceiptError('La fecha o la zona horaria de la captura es inválida.') from error
    amount, currency = amounts.pop() if len(amounts) == 1 else ('', '')
    return {
        'reference': orders.pop() if len(orders) == 1 else False,
        'operation_day': operation_day,
        'currency': currency or None,
        'expected_amount': amount.lstrip('+') or None,
    }


_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """Carga los modelos OCR una sola vez por proceso (cargar en cada captura tarda segundos)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR
                _engine = RapidOCR(params={
                    'EngineConfig.onnxruntime.intra_op_num_threads': 1,
                    'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                    'Global.log_level': 'error',
                })
    return _engine


def read_receipt(image_data, timezone_name, *, allow_missing_reference=False):
    from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

    if not image_data or len(image_data) > 5 * 1024 * 1024:
        raise ReceiptError('Selecciona una imagen JPG o PNG de hasta 5 MB.')
    try:
        with Image.open(BytesIO(image_data), formats=['JPEG', 'PNG']) as image:
            if image.width * image.height > 8_000_000:
                raise ReceiptError('La imagen supera 8 megapíxeles.')
            normalized = ImageOps.exif_transpose(image).convert('L')
            # Modo oscuro: invertir para que el texto quede oscuro sobre fondo claro.
            if ImageStat.Stat(normalized).mean[0] < 110:
                normalized = ImageOps.invert(normalized)
            buffer = BytesIO()
            normalized.save(buffer, format='PNG')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ReceiptError('El archivo no es una imagen JPG o PNG válida.') from error
    try:
        engine = _get_engine()
    except (ImportError, OSError) as error:
        raise ReceiptError('El build necesita RapidOCR y ONNX Runtime para leer imágenes.') from error
    try:
        with _engine_lock:  # varios comprobantes a la vez: el OCR se usa por turnos
            result = engine(buffer.getvalue())
    except Exception as error:
        raise ReceiptError('El motor OCR no pudo leer la imagen. Revisa la instalación y los modelos del build.') from error
    text = '\n'.join(result.txts or ())
    deposit = extract_deposit(text, timezone_name)
    if deposit:
        return deposit
    return extract_receipt(text, timezone_name, allow_missing_reference=allow_missing_reference)
