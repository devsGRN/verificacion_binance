"""Read-only SAPI adapter. Deliberately no transfers, withdrawals or screenshots."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import time as clock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class VerificationUnavailable(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Response:
    def __init__(self, status, body=b''):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class _Transport:
    def get(self, url, headers, timeout, allow_redirects=False):
        request = Request(url, headers=headers, method='GET')
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError('Respuesta demasiado grande.')
                return _Response(response.status, body)
        except HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(64 * 1024)  # Binance explica el error en {"code", "msg"}
            except OSError:
                body = b''
            exc.close()
            return _Response(status, body)


def positive_decimal(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Importe decimal inválido.') from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError('El importe debe ser finito y mayor que cero.')
    return amount


@dataclass(frozen=True)
class Result:
    state: str
    message: str
    canonical_id: str = ''
    amount: str = ''
    incomplete: bool = False
    currency: str = ''
    operation_day: date | None = None


def evaluate(kind, records, reference, currency, expected_amount, receiver_uid,
             network='', truncated=False, require_date=False):
    """Input must come from an authenticated Binance response, never a user payload."""
    expected = positive_decimal(expected_amount) if expected_amount is not None else None
    id_fields = ('transactionId', 'orderId') if kind == 'pay' else ('txId',)
    matches = [
        record for record in records
        if any(str(record.get(field) or '') == reference for field in id_fields)
    ]
    if len(matches) != 1:
        message = ('Referencia ambigua; requiere revisión.' if len(matches) > 1 else
                   'Ingreso no localizado en la consulta; no equivale a pago rechazado.')
        if truncated:
            message += ' La respuesta alcanzó el límite de registros.'
        return Result('pending', message, incomplete=truncated or len(matches) > 1)
    record = matches[0]
    actual_currency = record.get('currency' if kind == 'pay' else 'coin')
    if currency and actual_currency != currency:
        return Result('mismatch', 'La moneda no coincide.')
    try:
        actual = positive_decimal(record.get('amount'))
    except ValueError:
        return Result('pending', 'El registro no acredita un ingreso positivo válido.')
    if expected is not None and actual != expected:
        return Result('mismatch', 'El importe recibido no coincide con el solicitado.')
    if kind == 'pay':
        if record.get('orderType') not in ('PAY', 'C2C'):
            return Result('pending', 'Tipo de operación no admitido en este piloto.')
        receiver = record.get('receiverInfo')
        if not isinstance(receiver, dict) or str(receiver.get('binanceId', '')) != receiver_uid:
            return Result('pending', 'No se pudo comprobar la cuenta receptora esperada.')
        canonical = str(record.get('transactionId') or '')
    else:
        if record.get('network') != network:
            return Result('mismatch', 'La red no coincide.')
        # Status 6 (credited but cannot withdraw) is intentionally not accepted.
        if str(record.get('status')) != '1' or str(record.get('travelRuleStatus')) != '0':
            return Result('pending', 'Depósito sin estado final utilizable confirmado.')
        canonical = str(record.get('id', ''))
    if not canonical:
        return Result('pending', 'La respuesta no incluye un identificador canónico.')
    operation_day = None
    if kind == 'pay' and require_date:
        try:
            operation_day = datetime.fromtimestamp(int(record['transactionTime']) / 1000, timezone.utc).date()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return Result('pending', 'Binance no devolvió una fecha válida para la orden.')
    if not isinstance(actual_currency, str) or not actual_currency:
        return Result('pending', 'Binance no devolvió una moneda válida para la orden.')
    return Result('confirmed', 'Ingreso verificado en Binance',
                  canonical, format(actual, 'f'), currency=actual_currency, operation_day=operation_day)


# Explicación en español de los errores más comunes de Binance.
BINANCE_ERRORS = {
    -1003: 'demasiadas consultas seguidas; se reintentará',
    -1021: 'el reloj del servidor no coincide con el de Binance',
    -1022: 'la firma no es válida: revisa que la Secret Key esté bien copiada',
    -2014: 'la API Key no tiene el formato correcto: revisa que esté bien copiada',
    -2015: 'API Key inválida, sin permiso de lectura, o la IP del servidor no está autorizada en Binance',
    -1100: 'Binance rechazó un dato de la consulta',
    -1101: 'Binance rechazó un dato de la consulta',
    -1102: 'falta un dato obligatorio en la consulta',
    -1104: 'Binance rechazó un dato de la consulta',
    -1106: 'Binance rechazó un dato de la consulta',
    -1128: 'Binance rechazó la combinación de datos de la consulta',
}


def _binance_error(response):
    """Texto con el código y la explicación del error que devolvió Binance."""
    try:
        body = response.json()
        code = int(body.get('code'))
        message = str(body.get('msg') or '')[:150]
    except (ValueError, TypeError, AttributeError):
        return 'Binance no permitió completar la consulta (HTTP %s).' % response.status_code, None
    hint = BINANCE_ERRORS.get(code, 'error de Binance')
    return ('Binance rechazó la consulta (HTTP %s, código %s: %s). Detalle de Binance: %s'
            % (response.status_code, code, hint, message)), code


class BinanceReader:
    BASE_URL = 'https://api.binance.com'
    _clock_offset_ms = 0  # diferencia con el reloj de Binance, por proceso

    def __init__(self, api_key, secret, session=None):
        if not api_key or not secret:
            raise VerificationUnavailable('Faltan credenciales en el servidor.')
        self.api_key = api_key
        self.secret = secret
        self.session = session or _Transport()

    def _sync_clock(self):
        """Ajusta la hora con la de Binance (consulta pública, sin claves)."""
        try:
            response = self.session.get(self.BASE_URL + '/api/v3/time', headers={}, timeout=10,
                                        allow_redirects=False)
            server = int(response.json()['serverTime'])
        except (OSError, URLError, ValueError, KeyError, TypeError):
            return False
        BinanceReader._clock_offset_ms = server - int(clock.time() * 1000)
        return True

    def _get(self, path, params, _retry=True):
        timestamp = int(clock.time() * 1000) + BinanceReader._clock_offset_ms
        signed = dict(params, timestamp=timestamp, recvWindow=10000)
        query = urlencode(signed)
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            response = self.session.get(
                self.BASE_URL + path + '?' + query + '&signature=' + signature,
                headers={'X-MBX-APIKEY': self.api_key}, timeout=15,
                allow_redirects=False,
            )
            if response.status_code != 200:
                # Nunca mostrar la URL: contiene la consulta firmada.
                message, code = _binance_error(response)
                if code == -1021 and _retry and self._sync_clock():
                    return self._get(path, params, _retry=False)
                raise VerificationUnavailable(message)
            body = response.json()
        except (OSError, URLError, ValueError):
            raise VerificationUnavailable('Consulta no disponible; conexión o respuesta inválida.') from None
        if isinstance(body, dict) and 'code' in body and str(body['code']) not in ('0', '000000'):
            raise VerificationUnavailable('Binance devolvió un error de API (código %s: %s).' % (
                body.get('code'), str(body.get('msg') or body.get('message') or '')[:150]))
        return body

    def _pay_rows(self, params):
        params = dict(params, limit=100)
        body = self._get('/sapi/v1/pay/transactions', params)
        if not isinstance(body, dict) or str(body.get('code')) != '000000' or body.get('success') is not True:
            raise VerificationUnavailable('Respuesta Pay sin confirmación válida de la consulta.')
        rows = body.get('data')
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise VerificationUnavailable('Formato de registros no reconocido.')
        return rows

    PAGE = 100
    MIN_SPAN_MS = 2 * 60 * 1000
    MAX_CALLS = 16

    def _rows_between(self, start, end, budget):
        """Pagos entre start y end (ms). Binance devuelve como máximo 100 por consulta:
        si se llena, se parte el rango en dos hasta que cada tramo quepa."""
        budget[0] -= 1
        rows = self._pay_rows({'startTime': start, 'endTime': end})
        if len(rows) < self.PAGE:
            return rows, False
        if end - start <= self.MIN_SPAN_MS or budget[0] < 2:
            return rows, True
        middle = (start + end) // 2
        newer, newer_cut = self._rows_between(middle + 1, end, budget)
        older, older_cut = self._rows_between(start, middle, budget)
        return newer + older, newer_cut or older_cut

    @staticmethod
    def _now_ms():
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def _recent_windows(self, hours=48):
        """Tramos del más reciente al más antiguo: 2 h, 24 h, 48 h."""
        now, hour = self._now_ms(), 3600 * 1000
        edges = [0, 2, 24, hours]
        return [(now - b * hour, now - a * hour - (1 if a else 0)) for a, b in zip(edges, edges[1:])]

    def find_pay_by_suffix(self, suffix, receiver_uid, amount=None, hours=48):
        """IDs de pagos recientes recibidos por receiver_uid cuyo ID de orden termina en suffix."""
        expected = positive_decimal(amount) if amount else None
        found, truncated, budget = set(), False, [self.MAX_CALLS]
        for start, end in self._recent_windows(hours):
            rows, cut = self._rows_between(start, end, budget)
            truncated = truncated or cut
            for record in rows:
                receiver = record.get('receiverInfo')
                if not isinstance(receiver, dict) or str(receiver.get('binanceId', '')) != receiver_uid:
                    continue
                try:
                    if expected is not None and positive_decimal(record.get('amount')) != expected:
                        continue
                except ValueError:
                    continue
                for field in ('orderId', 'transactionId'):
                    value = str(record.get(field) or '')
                    if value.endswith(suffix):
                        found.add(value)
                        break
            if budget[0] <= 0:
                truncated = True
                break
        return found, truncated

    def verify(self, kind, reference, currency, amount, receiver_uid, operation_day, network=''):
        if kind not in ('pay', 'deposit'):
            raise VerificationUnavailable('Modalidad no implementada.')
        if kind == 'pay':
            budget, truncated = [self.MAX_CALLS], False
            if operation_day:
                day = datetime.combine(operation_day, time.min, timezone.utc)
                start = int(day.timestamp() * 1000)
                windows = [(start, start + 24 * 3600 * 1000 - 1)]
            else:
                # Captura sin fecha ("Pago exitoso"): buscar de lo más reciente a lo más antiguo.
                windows = self._recent_windows(48)
            for start, end in windows:
                rows, cut = self._rows_between(start, end, budget)
                truncated = truncated or cut
                result = evaluate(kind, rows, reference, currency, amount, receiver_uid,
                                  truncated=truncated, require_date=not operation_day)
                if not result.message.startswith('Ingreso no localizado'):
                    return result  # encontrado (verificado, discrepancia o ambiguo)
                if budget[0] <= 0:
                    break
            if operation_day:
                return result
            # Último intento: rango por defecto de Binance (90 días, máx. 100 registros).
            rows = self._pay_rows({})
            older = evaluate(kind, rows, reference, currency, amount, receiver_uid,
                             truncated=len(rows) >= self.PAGE, require_date=True)
            if not older.message.startswith('Ingreso no localizado'):
                return older
            return result
        params = {}
        if operation_day:
            start = datetime.combine(operation_day, time.min, timezone.utc)
            params = {'startTime': int(start.timestamp() * 1000),
                      'endTime': int((start + timedelta(days=1)).timestamp() * 1000) - 1}
        if kind == 'pay':
            rows = self._pay_rows(params)
            limit = 100
        else:
            params.update(coin=currency, txId=reference, limit=1000)
            rows = self._get('/sapi/v1/capital/deposit/hisrec', params)
            limit = 1000
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise VerificationUnavailable('Formato de registros no reconocido.')
        return evaluate(kind, rows, reference, currency, amount, receiver_uid,
                        network=network, truncated=len(rows) >= limit, require_date=not operation_day)
