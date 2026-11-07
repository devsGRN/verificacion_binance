import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from psycopg2.errors import UniqueViolation

from odoo import SUPERUSER_ID, api, fields, models
from odoo.modules.registry import Registry
from odoo.exceptions import UserError, ValidationError

from ..services.binance import VerificationUnavailable, positive_decimal
from ..services.receipt_ocr import ReceiptError, read_receipt


_logger = logging.getLogger(__name__)

# Con los últimos 6 dígitos del ID, buscar pagos hasta 30 días atrás (pagos viejos incluidos).
SUFFIX_SEARCH_DAYS = 30


def normalize_number(raw):
    """'+58 414-3556989' / '0414 3556989' / '584143556989' -> '584143556989'."""
    digits = re.sub(r'[^0-9]', '', raw or '')
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith('0'):  # número venezolano local
        digits = '58' + digits[1:]
    return digits


def _process_in_background(dbname, receipt_ids):
    """Lee la captura, consulta Binance y responde fuera de la petición del webhook."""
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['binance.whatsapp.receipt'].browse(receipt_ids).exists()._process_now(commit=True)
    except Exception:
        _logger.exception('Pagos Binance: procesamiento en segundo plano falló; lo retoma el cron')


class BinanceVerificationAccount(models.Model):
    _inherit = 'binance.verification.account'

    whatsapp_account_id = fields.Many2one('whatsapp.account', string='Cuenta de WhatsApp', ondelete='set null')
    whatsapp_mode = fields.Selection([
        ('disabled', 'Desactivado'), ('test', 'Pruebas'), ('customers', 'Gerentes'),
    ], string='Modo del bot', required=True, default='test')
    manager_numbers = fields.Text(
        string='WhatsApp de los gerentes',
        help='Números que pueden enviar comprobantes en modo Gerentes, uno por línea '
             '(por ejemplo +58 412-1234567). Vacío: se acepta cualquier número.',
    )

    def _manager_set(self):
        self.ensure_one()
        return {normalize_number(item) for item in re.split(r'[\n,;]+', self.manager_numbers or '')} - {''}
    whatsapp_test_sender = fields.Char(
        string='Remitente de prueba',
        help='Número internacional con código de país, sin + ni espacios. Vacío desactiva la recepción.',
    )


    def _accepts_whatsapp_sender(self, sender):
        self.ensure_one()
        return bool(
            self.whatsapp_account_id
            and re.fullmatch(r'[1-9][0-9]{6,14}', sender or '')
            and ((self.whatsapp_mode == 'customers'
                  and (not self._manager_set() or normalize_number(sender) in self._manager_set()))
                 or (self.whatsapp_mode == 'test' and sender == self.whatsapp_test_sender))
        )

    @api.constrains('whatsapp_mode', 'whatsapp_account_id', 'whatsapp_test_sender')
    def _check_whatsapp_test_sender(self):
        for record in self:
            if record.whatsapp_mode == 'customers' and not record.whatsapp_account_id:
                raise ValidationError('Selecciona la línea receptora de WhatsApp para activar el modo Gerentes.')
            if record.whatsapp_test_sender and not re.fullmatch(r'[1-9][0-9]{6,14}', record.whatsapp_test_sender):
                raise ValidationError('Introduce el número internacional del remitente sin + ni espacios.')


class WhatsAppAccount(models.Model):
    _inherit = 'whatsapp.account'

    def _process_messages(self, value):
        result = super()._process_messages(value)
        payload = value if 'messages' in value else value.get('whatsapp_business_api_data', {})
        for account in self:
            receivers = self.env['binance.verification.account'].sudo().search([
                ('whatsapp_account_id', '=', account.id), ('whatsapp_mode', '!=', 'disabled'),
            ])
            if not receivers:
                continue
            messages = payload.get('messages', [])
            image_senders = {message.get('from') for message in messages if message.get('type') == 'image'}
            _logger.info('Pagos Binance: webhook con %s mensajes (%s imágenes)', len(messages),
                         sum(1 for message in messages if message.get('type') == 'image'))
            ready = self.env['binance.whatsapp.receipt'].sudo()
            for message in messages:
                sender = message.get('from')
                candidates = receivers.filtered(lambda receiver: receiver._accepts_whatsapp_sender(sender))
                if not candidates or not message.get('id'):
                    continue
                receipts = self.env['binance.whatsapp.receipt'].sudo()
                try:
                    with self.env.cr.savepoint():
                        if (message.get('type') == 'image'
                                and message.get('image', {}).get('mime_type') in ('image/jpeg', 'image/png')):
                            ready |= receipts._enqueue(account, candidates, message['id'], sender)
                        elif message.get('type') == 'text' and sender not in image_senders:
                            ready |= receipts._accept_reference(account, sender, message)
                except Exception:
                    _logger.exception('No se pudo procesar la entrada de WhatsApp de Pagos Binance')
            # Responder a WhatsApp de inmediato y verificar en segundo plano, cada comprobante
            # en su propio hilo: así un mensaje no bloquea al siguiente.
            if ready:
                dbname = self.env.cr.dbname
                for receipt_id in ready.ids:
                    self.env.cr.postcommit.add(
                        lambda rid=receipt_id: threading.Thread(
                            target=_process_in_background, args=(dbname, [rid]),
                            name='binance-receipt-%s' % rid, daemon=True,
                        ).start()
                    )
        return result


class BinanceWhatsAppReceipt(models.Model):
    _name = 'binance.whatsapp.receipt'
    _description = 'Comprobante recibido por WhatsApp'

    message_uid = fields.Char(required=True, readonly=True)
    account_id = fields.Many2one('binance.verification.account', readonly=True, ondelete='restrict')
    whatsapp_account_id = fields.Many2one('whatsapp.account', readonly=True, ondelete='set null')
    attachment_id = fields.Many2one('ir.attachment', readonly=True, ondelete='set null')
    sender = fields.Char(required=True, readonly=True)
    state = fields.Selection([
        ('queued', 'En cola'), ('processed', 'Verificado'), ('pending', 'Pendiente'),
        ('duplicate', 'Ingreso ya registrado'), ('rejected', 'Pago errado'), ('error', 'Error'),
    ], default='queued', required=True, readonly=True)
    reviewed_by_id = fields.Many2one('res.users', string='Revisado por', readonly=True, copy=False)
    final_reply = fields.Selection([
        ('confirmed', '✅ Pago verificado'), ('rejected', '❌ Pago errado'), ('duplicate', 'Pago ya validado'),
    ], string='Veredicto enviado', readonly=True, copy=False,
        help='Último veredicto enviado al cliente. Un comprobante solo recibe un veredicto.')
    detail = fields.Char(readonly=True)
    awaiting_reference = fields.Boolean(readonly=True, copy=False)
    needs_new_image = fields.Boolean(readonly=True, copy=False)
    reference_requested_at = fields.Datetime(readonly=True, copy=False)
    reference_message_uid = fields.Char(readonly=True, copy=False)
    supplied_reference = fields.Char(readonly=True, copy=False)
    reply_state = fields.Selection([
        ('queued', 'Pendiente de envío'), ('sending', 'Enviando'), ('posted', 'En chat'),
        ('error', 'Revisar envío'),
    ], readonly=True, copy=False)
    verification_id = fields.Many2one('binance.payment.verification', readonly=True, ondelete='set null')
    attempts = fields.Integer(default=0, readonly=True)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True)

    _sql_constraints = [
        ('message_unique', 'unique(message_uid)', 'El mensaje ya fue recibido.'),
        ('reference_message_unique', 'unique(reference_message_uid)', 'El mensaje de referencia ya fue recibido.'),
    ]

    @api.model
    def _enqueue(self, line, candidates, message_uid, sender):
        message = self.env['whatsapp.message'].search([
            ('msg_uid', '=', message_uid), ('wa_account_id', '=', line.id),
            ('state', '=', 'received'),
        ], order='id desc', limit=1)
        attachment = message.mail_message_id.attachment_ids.filtered(
            lambda item: item.mimetype in ('image/jpeg', 'image/png')
        )[:1]
        values = {
            'message_uid': message_uid, 'account_id': candidates.id if len(candidates) == 1 else False,
            'sender': sender, 'attachment_id': attachment.id or False,
            'whatsapp_account_id': line.id,
        }
        if not attachment:
            # No descartar la captura en silencio: dejar la fila visible para revisión.
            _logger.warning('Comprobante WhatsApp %s de %s sin imagen descargada (mensaje %s)',
                            message_uid, sender, message.id or 'no encontrado')
            values.update(state='pending', detail='Odoo no descargó la imagen de WhatsApp. Pida reenviar la captura.')
        try:
            with self.env.cr.savepoint():
                receipt = self.create(values)
            self._trigger_processing()
            return receipt
        except UniqueViolation as error:
            if error.diag.constraint_name != 'binance_whatsapp_receipt_message_unique':
                raise
        return self.browse()

    @api.model
    def _accept_reference(self, line, sender, message):
        reference = re.sub(r'[\s.\-]', '', message.get('text', {}).get('body') or '')
        # Últimos 6 dígitos (o más) del ID de orden, o el ID completo.
        if not re.fullmatch(r'[0-9]{6,32}', reference):
            return self.browse()
        try:
            sent_at = datetime.fromtimestamp(int(message['timestamp']), timezone.utc).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return self.browse()
        self.env.cr.execute("""
            SELECT id FROM binance_whatsapp_receipt
            WHERE whatsapp_account_id = %s AND sender = %s
              AND state = 'pending' AND awaiting_reference
              AND COALESCE(reference_requested_at, create_date) < %s
              AND COALESCE(reference_requested_at, create_date) >= %s
            ORDER BY id LIMIT 2 FOR UPDATE
        """, [line.id, sender, sent_at,
              fields.Datetime.now() - timedelta(hours=24)])
        receipt = self.browse([row[0] for row in self.env.cr.fetchall()])
        if len(receipt) != 1 or self.search_count([
            ('reference_message_uid', '=', message['id']),
        ]):
            return self.browse()
        if self.search_count([
            ('whatsapp_account_id', '=', line.id),
            ('sender', '=', sender), ('state', '=', 'queued'),
        ]):
            return self.browse()
        receipt.write({
            'supplied_reference': reference,
            'reference_message_uid': message['id'],
            'awaiting_reference': False,
            'reply_state': False,
            'state': 'queued',
            'attempts': 0,
            'next_attempt_at': fields.Datetime.now(),
            'detail': 'Referencia recibida por WhatsApp. Pendiente de consultar.',
        })
        self._trigger_processing()
        return receipt

    def _process_now(self, commit=False):
        """Verifica y responde; si algo falla, el cron lo retoma.
        Con commit=True (hilo en segundo plano) guarda el resultado antes de enviar el WhatsApp."""
        for receipt in self:
            # Bloquear la fila: si el cron u otro hilo ya la está procesando, se salta.
            self.env.cr.execute(
                "SELECT id FROM binance_whatsapp_receipt WHERE id = %s AND state = 'queued' FOR UPDATE SKIP LOCKED",
                [receipt.id],
            )
            if not self.env.cr.fetchone():
                continue
            receipt.invalidate_recordset()
            try:
                with self.env.cr.savepoint():
                    receipt._process()
            except Exception:
                _logger.exception('Verificación inmediata falló para el comprobante %s; queda para el cron', receipt.id)
                continue
            if commit:
                self.env.cr.commit()  # el resultado queda guardado aunque falle el envío
            receipt._deliver_reply(commit=commit)

    def _deliver_reply(self, commit=False):
        """Envía la respuesta pendiente como máximo una vez.

        Primero "reserva" el envío (Pendiente de envío -> Enviando) y lo guarda; solo quien
        logra reservarlo envía. Así, si algo falla después de enviar el WhatsApp, nadie lo
        reenvía (antes, un fallo al guardar dejaba la respuesta en cola y salía dos veces)."""
        for receipt in self:
            self.env.cr.execute(
                "UPDATE binance_whatsapp_receipt SET reply_state = 'sending' "
                "WHERE id = %s AND reply_state = 'queued' RETURNING id", [receipt.id],
            )
            if not self.env.cr.fetchone():
                continue
            if commit:
                self.env.cr.commit()
            receipt.invalidate_recordset()
            try:
                with self.env.cr.savepoint():
                    receipt._post_reply()
            except Exception:
                _logger.exception('No se pudo responder al comprobante WhatsApp %s', receipt.id)
                receipt._mark_reply_error('Odoo devolvió un error al publicar la respuesta. Consulta los logs.')
            if commit:
                self.env.cr.commit()

    @api.model
    def _trigger_processing(self):
        # Despierta el cron al confirmar la transacción en lugar de esperar al siguiente minuto.
        cron = self.env.ref('binance_payment_verify.cron_binance_whatsapp_receipt', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_process_receipts(self):
        self._cron_post_replies()
        self.env.cr.execute("""
            SELECT id FROM binance_whatsapp_receipt
            WHERE state = 'queued' AND next_attempt_at <= %s
            ORDER BY next_attempt_at, id LIMIT 3 FOR UPDATE SKIP LOCKED
        """, [fields.Datetime.now()])
        for receipt in self.sudo().browse([row[0] for row in self.env.cr.fetchall()]):
            try:
                with self.env.cr.savepoint():
                    receipt._process()
            except UniqueViolation as error:
                if error.diag.constraint_name in (
                    'binance_payment_verification_canonical_unique',
                    'binance_payment_verification_request_unique',
                ):
                    receipt._retry('Otra solicitud está registrando este ingreso.')
                else:
                    raise
            except Exception:
                _logger.exception('Error procesando comprobante WhatsApp %s', receipt.id)
                receipt._retry('Error procesando el comprobante. Revisa los logs de Odoo.')
        # Respuestas recién encoladas o más comprobantes en cola: siguiente pasada inmediata.
        self.env.cr.execute("""
            SELECT 1 FROM binance_whatsapp_receipt
            WHERE reply_state = 'queued' OR (state = 'queued' AND next_attempt_at <= %s)
            LIMIT 1
        """, [fields.Datetime.now()])
        if self.env.cr.fetchone():
            self._trigger_processing()

    def _retry(self, detail):
        attempts = self.attempts + 1
        self.write({
            'attempts': attempts, 'detail': detail,
            'state': 'queued' if attempts < 3 else 'error',
            'next_attempt_at': fields.Datetime.now() + timedelta(minutes=2 ** attempts),
        })

    def _eligible_receivers(self):
        self.ensure_one()
        if not self.whatsapp_account_id:
            return self.env['binance.verification.account']
        return self.env['binance.verification.account'].search([
            ('whatsapp_account_id', '=', self.whatsapp_account_id.id),
            ('whatsapp_mode', '!=', 'disabled'),
        ]).filtered(lambda account: account._accepts_whatsapp_sender(self.sender))

    def _find_receiving_account(self, candidates, values):
        matches = []
        incomplete = False
        for account in candidates:
            try:
                result = account._query_binance(
                    'pay', values['reference'], values['currency'],
                    values['expected_amount'], values['operation_day'],
                )
            except VerificationUnavailable:
                incomplete = True
                continue
            if result.state == 'confirmed':
                matches.append((account, result))
            incomplete = incomplete or result.incomplete

        if len(matches) == 1:
            # El ID de orden es único en Binance: si una cuenta lo confirma, es esa,
            # aunque otra cuenta no haya podido responder por completo.
            return matches[0]
        detail = 'No se encontró un ingreso coincidente en las cuentas habilitadas.'
        if len(matches) > 1:
            detail = 'La referencia coincide en varias cuentas. Requiere revisión.'
        elif incomplete:
            detail = 'No se pudo completar la búsqueda en todas las cuentas. Requiere revisión.'
        # Primer fallo: pedir los 6 dígitos (el OCR pudo leer mal un dígito). Si ya los dio: revisión.
        request_reference = not matches and not self.supplied_reference
        self.write({
            'account_id': False,
            'state': 'pending',
            'detail': detail,
            'awaiting_reference': request_reference,
            # Con ID del cliente y sin resultado: avisar que pasa a revisión de un agente.
            'reply_state': 'queued',
        })
        return False, None

    def _process(self):
        candidates = self._eligible_receivers()
        if not candidates:
            self.write({'state': 'pending', 'detail': 'La línea o el remitente dejaron de estar habilitados.'})
            return
        if not self.attachment_id or self.attachment_id.file_size > 5 * 1024 * 1024:
            self.write({'state': 'pending', 'detail': 'Se necesita una imagen JPG o PNG de hasta 5 MB.'})
            return
        try:
            values = read_receipt(
                self.attachment_id.with_context(bin_size=False).raw, 'America/Caracas',
                allow_missing_reference=True,
            )
            self._remember_reading(values)  # para mostrárselo a quien revise
        except ReceiptError as error:
            if not self.supplied_reference:
                # Captura ilegible: pedir el ID de orden por WhatsApp sobre esta misma fila.
                self.write({
                    'state': 'pending', 'awaiting_reference': True, 'reply_state': 'queued',
                    'detail': '%s Se solicitará el ID de orden por WhatsApp.' % error,
                })
                return
            # Con ID suministrado por el cliente, Binance aporta moneda, importe y fecha.
            values = {'reference': False, 'operation_day': None, 'currency': None, 'expected_amount': None}
        supplied = self.supplied_reference
        if supplied and len(supplied) < 12:
            supplied = self._resolve_reference_suffix(candidates, supplied, values)
            if not supplied:
                return
        if supplied:
            if values.get('reference') and values['reference'] != supplied:
                # El ID escrito por el cliente es de otro pago: la fecha y el importe de la
                # captura no le corresponden. Binance aporta los datos reales de ese ID.
                values.update(operation_day=None, currency=None, expected_amount=None)
            values['reference'] = supplied
            binance_day = values.pop('_binance_day', None)
            if binance_day:
                values['operation_day'] = binance_day
        if not values.get('reference'):
            # Sin ID legible: pedir los últimos 6 dígitos. La fecha y el importe son opcionales
            # (la pantalla "Pago exitoso" no muestra fecha); Binance aporta los datos reales.
            self.write({
                'state': 'pending', 'awaiting_reference': True, 'reply_state': 'queued',
                'detail': 'No se leyó el ID de orden. Se solicitarán los últimos 6 dígitos por WhatsApp.',
            })
            return
        verifications = self.env['binance.payment.verification']
        for candidate in candidates:
            short = verifications._confirmed_by_short_reference(candidate, [values['reference']])
            if short:
                self._mark_short_duplicate(short)
                return
        account = candidates[:1]
        if len(candidates) > 1:
            account, result = self._find_receiving_account(candidates, values)
            if not account:
                return
        else:
            existing = verifications.search([
                ('account_id', '=', account.id), ('kind', '=', 'pay'),
                ('reference', '=', values['reference']), ('state', '=', 'confirmed'),
            ], limit=1)
            if existing:
                self._link_confirmed_verification(existing, {
                    'currency': values['currency'] or existing.currency,
                    'expected_amount': values['expected_amount'] or existing.confirmed_amount,
                    'operation_day': values['operation_day'] or existing.operation_day,
                })
                return
            try:
                result = account._query_binance(
                    'pay', values['reference'], values['currency'],
                    values['expected_amount'], values['operation_day'],
                )
            except VerificationUnavailable as error:
                self._retry(str(error))
                return
            if result.state != 'confirmed':
                request_reference = result.state == 'pending' and not self.supplied_reference
                self.write({
                    'state': 'pending', 'detail': result.message,
                    'awaiting_reference': request_reference,
                    'reply_state': 'queued',  # pedir 6 dígitos o avisar que pasa a revisión
                })
                return

        values['currency'] = values['currency'] or result.currency
        values['expected_amount'] = values['expected_amount'] or result.amount
        values['operation_day'] = values['operation_day'] or result.operation_day
        existing = verifications.search([
            ('account_id', '=', account.id), ('kind', '=', 'pay'),
            ('canonical_id', '=', result.canonical_id), ('state', '=', 'confirmed'),
        ], limit=1)
        if existing:
            self._link_confirmed_verification(existing, values)
            return
        short = verifications._confirmed_by_short_reference(
            account, [values['reference'], result.canonical_id])
        if short:
            self._mark_short_duplicate(short)
            return

        record = verifications._get_receipt_verification(account, values, account.company_id)
        self._link_confirmed_verification(record, values, result=result)

    def _mark_short_duplicate(self, short):
        """El pago ya se había aprobado a mano con solo los últimos dígitos del ID."""
        self.write({
            'account_id': short.account_id.id, 'verification_id': short.id, 'state': 'duplicate',
            'detail': 'El pago ya estaba verificado con el ID abreviado %s.' % short.reference,
            'awaiting_reference': False, 'reply_state': 'queued',
        })

    def _resolve_reference_suffix(self, candidates, suffix, values):
        """Convierte los últimos dígitos del cliente en el ID completo de un pago reciente."""
        ocr_reference = values.get('reference')
        found, incomplete = {}, False
        for account in candidates:
            try:
                references, truncated = account._find_pay_by_suffix(
                    suffix, values.get('expected_amount'), days=SUFFIX_SEARCH_DAYS)
                if not references and values.get('expected_amount'):
                    # El importe leído puede estar mal: repetir solo por los dígitos.
                    references, truncated = account._find_pay_by_suffix(suffix, days=SUFFIX_SEARCH_DAYS)
            except VerificationUnavailable:
                incomplete = True
                continue
            found.update(references)
            incomplete = incomplete or truncated
        if ocr_reference in found:
            found = {ocr_reference: found[ocr_reference]}
        if not found and ocr_reference and ocr_reference.endswith(suffix):
            # Binance no lo encontró por los dígitos: seguir con el ID leído de la captura.
            return ocr_reference
        if len(found) == 1:
            reference, day = found.popitem()
            if day:
                # Fecha real del pago según Binance: así la consulta va directo a ese día
                # (pagos viejos incluidos) y se aplica la fecha de corte.
                values['_binance_day'] = day
            return reference
        detail = ('Ningún pago de los últimos %s días termina en %s.' % (SUFFIX_SEARCH_DAYS, suffix)
                  if not found else 'Varios pagos terminan en %s.' % suffix)
        if incomplete:
            detail += ' La búsqueda en Binance no fue completa.'
        self.write({
            'state': 'pending', 'awaiting_reference': False, 'reply_state': 'queued',
            'detail': detail + ' Requiere revisión manual.',
        })
        return False

    def _link_confirmed_verification(self, record, values, result=None):
        if (record.currency != values['currency']
                or positive_decimal(record.expected_amount) != positive_decimal(values['expected_amount'])
                or record.operation_day != fields.Date.to_date(values['operation_day'])):
            self.write({'state': 'pending', 'reply_state': 'queued',
                        'detail': 'Los datos difieren de la verificación existente.'})
            return
        newly_confirmed = result is not None and record._verify(result=result)
        self.write({
            'account_id': record.account_id.id,
            'verification_id': record.id,
            'state': 'processed' if newly_confirmed else 'duplicate',
            'detail': record.result_message if newly_confirmed else (
                'El ingreso ya estaba confirmado. Se enlazó la verificación original.'
            ),
            'awaiting_reference': False,
            # También se avisa si el pago ya se había usado, para que no se repita.
            'reply_state': 'queued',
        })

    @api.model
    def _cron_post_replies(self):
        # Only post replies for results committed by a previous run.
        self.env.cr.execute("""
            SELECT id FROM binance_whatsapp_receipt
            WHERE reply_state = 'queued'
            ORDER BY id LIMIT 3 FOR UPDATE SKIP LOCKED
        """)
        # commit=True: cada respuesta queda marcada como enviada al instante, para que un
        # error posterior del cron no la deshaga y la vuelva a enviar.
        self.sudo().browse([row[0] for row in self.env.cr.fetchall()])._deliver_reply(commit=True)

    def _mark_reply_error(self, reason):
        self.write({
            'reply_state': 'error',
            'detail': ' '.join(filter(None, [self.detail, reason])),
        })

    def _post_reply(self):
        incoming = self.env['whatsapp.message'].search([
            ('msg_uid', '=', self.message_uid), ('wa_account_id', '=', self.whatsapp_account_id.id),
            ('state', '=', 'received'),
        ], order='id desc', limit=1).mail_message_id
        channel = self.env['discuss.channel'].browse(incoming.res_id).exists() if incoming.model == 'discuss.channel' else False
        rejected = self.state == 'rejected'
        request_reference = self.state == 'pending' and self.awaiting_reference
        request_image = self.state == 'pending' and self.needs_new_image
        in_review = self.state == 'pending' and not request_reference and not request_image
        confirmed = self.state == 'processed' and self.verification_id.state == 'confirmed'
        duplicate = self.state == 'duplicate'
        eligible = self._eligible_receivers()
        verdict = 'confirmed' if confirmed else 'rejected' if rejected else 'duplicate' if duplicate else False
        if self.final_reply:
            # Este comprobante ya recibió su veredicto: nunca enviar un segundo.
            self._mark_reply_error('Ya se había enviado "%s" al cliente; no se envía otro mensaje.'
                                   % dict(self._fields['final_reply'].selection)[self.final_reply])
            return
        if (not (request_reference or request_image or confirmed or rejected or in_review or duplicate)
                or not eligible or (confirmed and self.account_id not in eligible)):
            self._mark_reply_error('La configuración actual no permite enviar esta respuesta.')
            return
        if not channel or channel.channel_type != 'whatsapp':
            self._mark_reply_error('No se encontró el chat de WhatsApp del comprobante.')
            return
        channel_number = re.sub(r'[^0-9]', '', channel.whatsapp_number or '')
        if channel.wa_account_id != self.whatsapp_account_id or channel_number != self.sender:
            self._mark_reply_error('El número o la línea del chat no coincide con el comprobante.')
            return
        if not incoming.date or incoming.date <= fields.Datetime.now() - timedelta(hours=24):
            self._mark_reply_error('El comprobante quedó fuera de la ventana de respuesta.')
            return
        body = '✅ Pago verificado !'
        if rejected:
            body = '❌ Pago errado 🚨🚨'
        elif duplicate:
            body = '⚠️ Este pago ya fue validado anteriormente. Si es un pago nuevo, envíe su comprobante.'
        elif request_reference:
            body = (
                '⚠️ No hemos podido validar su pago. Por favor escriba los últimos 6 dígitos '
                'del ID de orden en un mensaje de texto.'
            )
        elif in_review:
            body = self._in_review_message()
        elif request_image:
            body = (
                'No hemos podido leer la fecha o el importe del comprobante. '
                'Envíe una nueva captura completa y legible de Detalles del pago de Binance Pay.'
            )
        posted = channel.message_post(
            body=body,
            message_type='whatsapp_message',
            subtype_xmlid='mail.mt_comment',
        )
        self._send_whatsapp_now(posted)
        self.reply_state = 'posted'
        if verdict:
            self.final_reply = verdict
        if request_reference:
            self.reference_requested_at = fields.Datetime.now()

    def _in_review_message(self):
        return '🕐 No pudimos validar su pago automáticamente. Un agente lo revisará en breve.'

    def _send_whatsapp_now(self, mail_message):
        """Si Odoo dejó el WhatsApp en cola, enviarlo ya en lugar de esperar a su cron."""
        outgoing = self.env['whatsapp.message'].search([
            ('mail_message_id', '=', mail_message.id), ('state', '=', 'outgoing'),
        ])
        if outgoing and hasattr(outgoing, '_send_message'):
            try:
                with self.env.cr.savepoint():
                    outgoing._send_message()
            except Exception:
                _logger.exception('Envío inmediato de WhatsApp falló; lo enviará la cola de Odoo')

    # --- Revisión manual -------------------------------------------------

    def _check_reviewable(self):
        self.ensure_one()
        if not self._user_can_review():
            raise UserError('Solo un administrador o un Revisor de pagos Binance puede revisar comprobantes.')
        # Bloquear la fila y leerla de nuevo: evita que dos clics (✅ y ❌) se crucen.
        self.env.cr.execute('SELECT id FROM binance_whatsapp_receipt WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.final_reply or self.state not in ('pending', 'error'):
            raise UserError('Este comprobante ya fue resuelto (%s). Recarga la página.'
                            % dict(self._fields['state'].selection)[self.state])

    def action_view_image(self):
        self.ensure_one()
        if not self.attachment_id:
            raise UserError('Este comprobante no tiene imagen.')
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s' % self.attachment_id.id, 'target': 'new'}

    def action_manual_confirm(self):
        self._check_reviewable()
        return {
            'type': 'ir.actions.act_window', 'name': 'Confirmar pago manualmente',
            'res_model': 'binance.manual.verify.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_receipt_id': self.id},
        }

    def action_manual_reject(self):
        self._check_reviewable()
        self.sudo().write({
            'state': 'rejected', 'awaiting_reference': False, 'reply_state': 'queued',
            'reviewed_by_id': self.env.user.id,
            'detail': 'Pago errado. Revisado por %s.' % self.env.user.name,
        })
        self._trigger_processing()


class BinanceManualVerifyWizard(models.TransientModel):
    _name = 'binance.manual.verify.wizard'
    _description = 'Confirmar pago de WhatsApp manualmente'

    receipt_id = fields.Many2one('binance.whatsapp.receipt', required=True, readonly=True)
    account_id = fields.Many2one('binance.verification.account', string='Cuenta receptora', required=True)
    allowed_account_ids = fields.Many2many('binance.verification.account', compute='_compute_allowed_account_ids')
    reference = fields.Char(string='ID de la orden', required=True)
    operation_day = fields.Date(string='Fecha del pago', required=True, default=fields.Date.context_today)
    currency = fields.Char(string='Moneda', required=True, default='USDT')
    expected_amount = fields.Char(string='Importe', required=True, help='Decimal con punto: 10.50')

    @api.depends('receipt_id')
    def _compute_allowed_account_ids(self):
        for wizard in self:
            wizard.allowed_account_ids = wizard.receipt_id.sudo()._eligible_receivers()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        receipt = self.env['binance.whatsapp.receipt'].browse(values.get('receipt_id')).sudo()
        if receipt:
            values.setdefault('reference', receipt.supplied_reference or False)
            accounts = receipt._eligible_receivers()
            account = receipt.account_id if receipt.account_id in accounts else accounts[:1] if len(accounts) == 1 else False
            if account:
                values['account_id'] = account.id
        return values

    def action_confirm(self):
        self.ensure_one()
        receipt = self.receipt_id.sudo()
        receipt.with_env(self.env)._check_reviewable()
        reference = (self.reference or '').strip()
        currency = (self.currency or '').strip().upper()
        amount = (self.expected_amount or '').strip().replace(',', '.')
        try:
            positive_decimal(amount)
        except ValueError as error:
            raise UserError(str(error)) from None
        if self.account_id not in receipt._eligible_receivers():
            raise UserError('La cuenta receptora no está habilitada para este remitente de WhatsApp.')
        verifications = self.env['binance.payment.verification'].sudo()
        if re.fullmatch(r'[0-9]{1,11}', reference):
            reference = self._full_reference(reference)
        short = verifications._confirmed_by_short_reference(self.account_id, [reference])
        if short:
            raise UserError('Este pago ya está verificado (con el ID abreviado %s). No se puede usar dos veces.'
                            % short.reference)
        existing = verifications.search([
            ('account_id', '=', self.account_id.id), ('kind', '=', 'pay'),
            '|', ('reference', '=', reference), ('canonical_id', '=', reference),
        ], limit=1)
        if existing.state == 'confirmed':
            raise UserError('Este ID de orden ya está verificado (%s). No se puede usar dos veces.' % existing.reference)
        if existing and (existing.currency != currency or positive_decimal(existing.expected_amount) != positive_decimal(amount)
                         or existing.operation_day != self.operation_day):
            raise UserError('Ya existe una consulta con ese ID y datos distintos. Revísala en Verificaciones.')
        record = existing or verifications.create({
            'account_id': self.account_id.id, 'company_id': self.account_id.company_id.id or self.env.company.id,
            'kind': 'pay', 'reference': reference, 'operation_day': self.operation_day,
            'currency': currency, 'expected_amount': amount,
        })
        record._confirm_manually(self.env.user)
        receipt.write({
            'state': 'processed', 'account_id': self.account_id.id, 'verification_id': record.id,
            'awaiting_reference': False, 'reply_state': 'queued', 'reviewed_by_id': self.env.user.id,
            'detail': 'Pago verificado manualmente por %s.' % self.env.user.name,
        })
        receipt._trigger_processing()
        return {'type': 'ir.actions.act_window_close'}

    def _full_reference(self, suffix):
        """Convierte los últimos dígitos en el ID de orden completo consultando Binance, para
        que el mismo pago no pueda registrarse otra vez con su ID completo."""
        if len(suffix) < 6:
            raise UserError('Escribe al menos los últimos 6 dígitos del ID de orden, o el ID completo.')
        try:
            found, truncated = self.account_id.sudo()._find_pay_by_suffix(suffix, days=SUFFIX_SEARCH_DAYS)
        except VerificationUnavailable as error:
            raise UserError('No se pudo consultar Binance para completar el ID (%s). '
                            'Escribe el ID de orden completo.' % error) from None
        if len(found) == 1:
            return next(iter(found))
        if found:
            raise UserError('Varios pagos terminan en %s. Escribe el ID de orden completo.' % suffix)
        raise UserError('No hay pagos en Binance de los últimos %s días cuyo ID termine en %s%s. '
                        'Revisa los dígitos o escribe el ID de orden completo.'
                        % (SUFFIX_SEARCH_DAYS, suffix, ' (la búsqueda no fue completa)' if truncated else ''))
