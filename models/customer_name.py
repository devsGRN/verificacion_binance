"""Cliente del pago a partir de un nombre de referencia que manda el gerente por WhatsApp.

Flujo normal: el gerente manda SOLO la captura. Si el bot la valida, después del ✅ pregunta
si es un abono (1) o un pago de contado (2):
  - Contado: solo queda la verificación en Pagos Binance; no se registra en Pagos.
  - Abono (o sin respuesta en 3 minutos): se registra en Pagos y, si respondió "1", le pide
un nombre de referencia (por ejemplo "cañon") y el gerente tiene 3 minutos para responder.
  - Si hay varios (aunque uno se llame exactamente así), el bot los muestra numerados y el
    gerente responde con el número.
  - Siempre (aunque haya uno solo) el bot pide confirmar: "¿Confirmas que este pago es
    para X? Responde SI o NO". Con SI se asigna; con NO pide otro nombre.
  - Si no hay ninguno, el bot pide otro nombre.
Cada pregunta del bot le da otros 3 minutos. Si no responde, queda el "Cliente por defecto"
(POR ASIGNAR) y contabilidad lo asigna a mano. Si el nombre llega junto con la captura,
se usa igual (sin preguntar si hay uno solo). No se crean contactos nuevos y la
verificación del pago no cambia.
"""
import logging
import re
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)

# Tiempo máximo entre la captura y un nombre mandado aparte (WhatsApp a veces los separa).
NAME_WINDOW_MINUTES = 3
# Tiempo que tiene el gerente para responder cada pregunta del bot.
ASK_WINDOW_MINUTES = 3
# Máximo de clientes que se muestran para elegir.
MAX_OPTIONS = 5
ASK_MESSAGE = '👤 ¿A qué cliente corresponde este pago? Escribe un nombre de referencia.'
TYPE_MESSAGE = ('🧾 ¿Este pago es un abono o un pago de contado? Responde con el número:\n'
                '1. Abono\n2. Pago de contado')
VITRINA_MESSAGE = '🛍️ Pago de contado verificado. No se registra en Pagos.'
REFERENCE_PATTERN = re.compile(r'[0-9]{6,32}')
YES_WORDS = {'si', 'sí', 's', 'yes', 'ok', 'okey', 'dale', 'confirmo', 'correcto', 'listo', '👍', '✅'}
NO_WORDS = {'no', 'n', 'nop', 'negativo', '❌', '👎'}
OPTION_PATTERN = re.compile(r'(?:opci[oó]n\s*)?#?\s*([0-9]{1,2})\s*[.)]?')


def _clean_name(text):
    return ' '.join((text or '').split())[:120]


def _like_escape(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class BinanceWhatsAppNote(models.Model):
    """Nombres que llegaron antes que su captura (WhatsApp a veces los separa)."""
    _name = 'binance.whatsapp.note'
    _description = 'Nombre de cliente recibido por WhatsApp'
    _order = 'id desc'

    whatsapp_account_id = fields.Many2one('whatsapp.account', required=True, ondelete='cascade')
    sender = fields.Char(required=True)
    text = fields.Char(required=True)
    used = fields.Boolean(default=False)


class WhatsAppAccount(models.Model):
    _inherit = 'whatsapp.account'

    def _process_messages(self, value):
        result = super()._process_messages(value)
        try:
            with self.env.cr.savepoint():
                self._binance_collect_customer_names(value)
        except Exception:
            # Nunca afectar la verificación por el nombre del cliente.
            _logger.exception('Pagos Binance: no se pudo leer el nombre del cliente')
        return result

    def _binance_collect_customer_names(self, value):
        payload = value if 'messages' in value else value.get('whatsapp_business_api_data', {})
        messages = payload.get('messages', [])
        Receipt = self.env['binance.whatsapp.receipt'].sudo()
        Note = self.env['binance.whatsapp.note'].sudo()
        since = fields.Datetime.now() - timedelta(minutes=NAME_WINDOW_MINUTES)
        asked_since = fields.Datetime.now() - timedelta(minutes=ASK_WINDOW_MINUTES)
        for account in self:
            receivers = self.env['binance.verification.account'].sudo().search([
                ('whatsapp_account_id', '=', account.id), ('whatsapp_mode', '!=', 'disabled'),
            ])
            if not receivers:
                continue
            for message in messages:
                sender = message.get('from')
                if not sender or not receivers.filtered(lambda r: r._accepts_whatsapp_sender(sender)):
                    continue
                if message.get('type') == 'image':
                    receipt = Receipt.search([('message_uid', '=', message.get('id'))], limit=1)
                    if not receipt or receipt.customer_partner_id:
                        continue
                    name = _clean_name(message.get('image', {}).get('caption'))
                    if not name:
                        # Nombre que llegó justo antes que la captura.
                        note = Note.search([
                            ('whatsapp_account_id', '=', account.id), ('sender', '=', sender),
                            ('used', '=', False), ('create_date', '>=', since),
                        ], limit=1)
                        if note:
                            note.used = True
                            name = note.text
                    if name:
                        receipt._handle_customer_text(name)
                elif message.get('type') == 'text':
                    name = _clean_name(message.get('text', {}).get('body'))
                    if not name or REFERENCE_PATTERN.fullmatch(re.sub(r'[\s.\-]', '', name)):
                        continue  # vacío o los dígitos del ID de orden
                    # 1) ¿Abono o vitrina? (se pregunta antes que el cliente)
                    typing = Receipt.search([
                        ('whatsapp_account_id', '=', account.id), ('sender', '=', sender),
                        ('type_done', '=', False), ('type_asked_at', '>=', asked_since),
                    ], order='type_asked_at desc', limit=1)
                    if typing:
                        typing._handle_type_text(name)
                        continue
                    base = [('whatsapp_account_id', '=', account.id), ('sender', '=', sender),
                            ('customer_partner_id', '=', False)]
                    # Primero la captura a la que el bot le preguntó; si no, una reciente.
                    receipt = Receipt.search(base + [('customer_asked_at', '>=', asked_since)],
                                             order='customer_asked_at desc', limit=1) \
                        or Receipt.search(base + [('create_date', '>=', since)], order='id desc', limit=1)
                    if receipt:
                        receipt._handle_customer_text(name)
                    elif not OPTION_PATTERN.fullmatch(name.lower()):
                        Note.create({'whatsapp_account_id': account.id, 'sender': sender, 'text': name})


class BinanceWhatsAppReceipt(models.Model):
    _inherit = 'binance.whatsapp.receipt'

    customer_name = fields.Char(string='Cliente escrito por el gerente', readonly=True, copy=False)
    customer_partner_id = fields.Many2one(
        'res.partner', string='Cliente encontrado', readonly=True, copy=False, ondelete='set null')
    customer_asked_at = fields.Datetime(string='Cliente pedido al gerente', readonly=True, copy=False)
    customer_option_ids = fields.Many2many(
        'res.partner', 'binance_receipt_customer_option_rel', 'receipt_id', 'partner_id',
        string='Opciones mostradas al gerente', readonly=True, copy=False)
    customer_pending_id = fields.Many2one(
        'res.partner', string='Cliente por confirmar', readonly=True, copy=False, ondelete='set null')
    type_asked_at = fields.Datetime(string='Tipo pedido al gerente', readonly=True, copy=False)
    type_done = fields.Boolean(readonly=True, copy=False)

    def _link_confirmed_verification(self, record, values, result=None):
        # Pagos verificados por el bot: el pago en Pagos se crea cuando el gerente dice que es
        # un abono (o si no responde a tiempo), no en el momento de verificar.
        return super()._link_confirmed_verification(
            record.with_context(binance_ask_type=True), values, result=result)

    def _post_reply(self):
        already = self.final_reply
        res = super()._post_reply()
        # Recién se envió el ✅ (o el aviso de pago anterior al corte) y no hay cliente.
        just_answered = (self.final_reply == 'confirmed' and not already) or (
            self.before_cutoff and self.state == 'pending' and self.reply_state == 'posted')
        if just_answered and not self.type_asked_at and not self.type_done:
            try:
                with self.env.cr.savepoint():
                    payment_type = self.verification_id.sudo().payment_type
                    if payment_type:
                        # Ya se sabe el tipo (por ejemplo, otra captura del mismo pago).
                        self.type_done = True
                        if payment_type == 'abono':
                            self._start_customer_flow()
                    elif self._send_to_manager(TYPE_MESSAGE):
                        self.type_asked_at = fields.Datetime.now()
            except Exception:
                _logger.exception('No se pudo preguntar el tipo de pago del comprobante %s', self.id)
        return res

    # --- ¿Abono o pago de contado? -------------------------------------------

    def _handle_type_text(self, text):
        receipt = self.sudo()
        receipt.ensure_one()
        answer = text.lower().strip(' .!¡¿?')
        option = OPTION_PATTERN.fullmatch(answer)
        number = option.group(1) if option else ''
        if number == '1' or answer.startswith('abono'):
            receipt._set_payment_type('abono', notify=True)
        elif number == '2' or 'contado' in answer or 'vitrina' in answer:
            receipt._set_payment_type('vitrina', notify=True)
        elif receipt._send_to_manager('Responde 1 (Abono) o 2 (Pago de contado).'):
            receipt.type_asked_at = fields.Datetime.now()

    def _set_payment_type(self, payment_type, notify=False):
        """Abono: se registra en Pagos (como siempre) y sigue la pregunta del cliente.
        Contado: solo queda la verificación en Pagos Binance; no se registra en Pagos."""
        self.ensure_one()
        verification = self.verification_id.sudo()
        self.write({'type_done': True})
        if not verification or verification.payment_type:
            return
        verification.write({'payment_type': payment_type})
        if payment_type == 'vitrina':
            if notify:
                self._send_to_manager(VITRINA_MESSAGE)
            return
        verification._create_draft_payment()
        if notify:
            self._start_customer_flow()

    @api.model
    def _cron_process_receipts(self):
        res = super()._cron_process_receipts()
        # Sin respuesta en 3 minutos: se toma como abono ("POR ASIGNAR" en Pagos).
        limit = fields.Datetime.now() - timedelta(minutes=ASK_WINDOW_MINUTES)
        for receipt in self.sudo().search([('type_done', '=', False), ('type_asked_at', '<', limit)], limit=50):
            try:
                with self.env.cr.savepoint():
                    receipt._set_payment_type('abono', notify=False)
            except Exception:
                _logger.exception('No se pudo registrar como abono el comprobante %s', receipt.id)
        return res

    def _start_customer_flow(self):
        """Pide el cliente (o muestra la lista / la confirmación si el nombre vino con la captura)."""
        self.ensure_one()
        if self.customer_partner_id or self.customer_asked_at:
            return
        try:
            with self.env.cr.savepoint():
                if self.customer_pending_id:
                    body = self._confirm_message()
                elif self.customer_option_ids:
                    body = self._options_message()
                elif self.customer_name:
                    body = self._not_found_message(self.customer_name)
                else:
                    body = ASK_MESSAGE
                if self._send_to_manager(body):
                    self.customer_asked_at = fields.Datetime.now()
        except Exception:
            _logger.exception('No se pudo pedir el cliente del comprobante %s', self.id)

    def _send_to_manager(self, body):
        """Escribe al gerente en el mismo chat de WhatsApp de la captura."""
        self.ensure_one()
        incoming = self.env['whatsapp.message'].sudo().search([
            ('msg_uid', '=', self.message_uid), ('wa_account_id', '=', self.whatsapp_account_id.id),
        ], order='id desc', limit=1).mail_message_id
        if incoming.model != 'discuss.channel':
            return False
        channel = self.env['discuss.channel'].sudo().browse(incoming.res_id).exists()
        if not channel or channel.channel_type != 'whatsapp':
            return False
        try:
            with self.env.cr.savepoint():
                posted = channel.message_post(body=body, message_type='whatsapp_message',
                                              subtype_xmlid='mail.mt_comment')
                self._send_whatsapp_now(posted)
            return True
        except Exception:
            _logger.exception('No se pudo escribir al gerente del comprobante %s', self.id)
            return False

    # --- Búsqueda de clientes ---------------------------------------------

    def _customer_base_domain(self):
        self.ensure_one()
        domain = [('parent_id', '=', False)]
        default = (self.account_id or self.verification_id.account_id).payment_partner_id
        if default:
            domain.append(('id', '!=', default.id))
        return domain

    def _find_customer(self, name):
        """Un solo contacto con exactamente ese nombre (sin importar mayúsculas)."""
        self.ensure_one()
        partners = self.env['res.partner'].sudo().search(
            self._customer_base_domain() + [('name', '=ilike', _like_escape(name))], limit=2)
        return partners if len(partners) == 1 else self.env['res.partner']

    def _search_customer_options(self, name):
        """Contactos que contienen TODAS las palabras escritas (sin importar mayúsculas)."""
        self.ensure_one()
        words = [word for word in name.split() if len(word) >= 2] or [name]
        domain = self._customer_base_domain() + [('name', 'ilike', word) for word in words]
        return self.env['res.partner'].sudo().search(domain, order='name', limit=MAX_OPTIONS + 1)

    def _options_message(self):
        lines = ['👤 ¿Cuál de estos clientes? Responde con el número:']
        lines += ['%s. %s' % (index, partner.name) for index, partner in enumerate(self.customer_option_ids, 1)]
        if self.env.context.get('binance_more_options'):
            lines.append('Hay más resultados; si no está, escribe el nombre más completo.')
        return '\n'.join(lines)

    def _confirm_message(self):
        return ('👤 ¿Confirmas que este pago es para *%s*? Responde SI o NO.'
                % self.customer_pending_id.name)

    def _not_found_message(self, name):
        return '⚠️ No encontré clientes con "%s". Escribe otro nombre de referencia.' % name

    # --- Conversación con el gerente -----------------------------------------

    def _handle_customer_text(self, text):
        """Procesa lo que escribió el gerente: un número de la lista o un nombre de referencia.
        Solo le responde si el bot ya le había preguntado (si no, lo resuelve en silencio y el
        ✅ le mostrará la lista si hace falta)."""
        receipt = self.sudo()
        receipt.ensure_one()
        if receipt.customer_partner_id:
            return
        talking = bool(receipt.customer_asked_at)
        answer = text.lower().strip(' .!¡¿?')
        if receipt.customer_pending_id:
            # Esperando que el gerente confirme el cliente.
            if answer in YES_WORDS:
                receipt._assign_customer(receipt.customer_pending_id, notify=talking)
                return
            if answer in NO_WORDS:
                receipt.write({'customer_pending_id': False})
                if talking:
                    receipt._ask_again('👤 Entendido. Escribe otro nombre de referencia del cliente.')
                return
            if OPTION_PATTERN.fullmatch(answer):
                if talking:
                    receipt._ask_again('Responde SI o NO.')
                return
            receipt.write({'customer_pending_id': False})  # escribió otro nombre: buscar de nuevo
        option = OPTION_PATTERN.fullmatch(text.lower())
        if option and receipt.customer_option_ids:
            index = int(option.group(1))
            options = receipt.customer_option_ids
            if 1 <= index <= len(options):
                receipt._propose_customer(options[index - 1], notify=talking)
            elif talking:
                receipt._ask_again('Responde con un número del 1 al %s.' % len(options))
            return
        if option:
            return  # un número sin lista: no es un nombre
        receipt.write({'customer_name': text})
        # Siempre mostrar todos los que contienen el nombre (aunque uno se llame exactamente
        # igual); solo se asigna directo si hay uno solo.
        found = receipt._search_customer_options(text)
        if len(found) == 1:
            receipt._propose_customer(found, notify=talking)
            return
        more = len(found) > MAX_OPTIONS
        receipt.write({'customer_option_ids': [(6, 0, found[:MAX_OPTIONS].ids)]})
        if talking:
            if found:
                receipt._ask_again(receipt.with_context(binance_more_options=more)._options_message())
            else:
                receipt._ask_again(receipt._not_found_message(text))

    def _ask_again(self, body):
        if self._send_to_manager(body):
            self.customer_asked_at = fields.Datetime.now()

    def _propose_customer(self, partner, notify=False):
        """El gerente eligió (o solo hay uno): pedirle que confirme antes de asignarlo."""
        self.write({'customer_pending_id': partner.id, 'customer_option_ids': [(5, 0, 0)]})
        if notify:
            self._ask_again(self._confirm_message())

    def _assign_customer(self, partner, notify=False):
        self.write({'customer_partner_id': partner.id, 'customer_pending_id': False,
                    'customer_option_ids': [(5, 0, 0)]})
        self._apply_customer_to_payment()
        if notify:
            self._send_to_manager('👤 Cliente asignado: %s' % partner.name)

    def _set_customer_name(self, name):
        """Compatibilidad: asigna solo si hay coincidencia exacta. Devuelve True si asignó."""
        self._handle_customer_text(name)
        return bool(self.customer_partner_id)

    def _apply_customer_to_payment(self):
        """Pone el cliente encontrado en el pago en borrador, solo si sigue con el cliente
        por defecto (o vacío): nunca pisa un cliente que ya puso contabilidad."""
        for receipt in self.sudo().filtered('customer_partner_id'):
            verification = receipt.verification_id
            payment = verification.payment_id
            if not payment or payment.state != 'draft':
                continue
            default = verification.account_id.payment_partner_id
            if payment.partner_id and payment.partner_id != default:
                continue
            try:
                with self.env.cr.savepoint():
                    payment.with_user(SUPERUSER_ID).write({'partner_id': receipt.customer_partner_id.id})
                    payment.with_user(SUPERUSER_ID).message_post(
                        body='Cliente asignado desde WhatsApp: %s (escrito por +%s).'
                             % (receipt.customer_partner_id.display_name, receipt.sender),
                        message_type='comment', subtype_xmlid='mail.mt_note',
                    )
            except Exception:
                _logger.exception('No se pudo asignar el cliente al pago de la verificación %s', verification.id)
