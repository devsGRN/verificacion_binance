"""Cliente del pago a partir del nombre que manda el gerente por WhatsApp.

Versión sencilla: si junto a la captura llega un nombre (como texto de la foto o como
mensaje aparte, antes o después, dentro de unos minutos) y en Contactos hay UN solo
contacto con ese mismo nombre, el pago en borrador se crea / actualiza con ese cliente
en lugar del "Cliente por defecto". No se crean contactos nuevos: si no hay coincidencia
exacta (sin importar mayúsculas ni espacios de más) o hay varias, queda como está y
contabilidad lo asigna a mano. La verificación del pago no cambia.
"""
import logging
import re
from datetime import timedelta

from odoo import SUPERUSER_ID, fields, models

_logger = logging.getLogger(__name__)

# Tiempo máximo entre la captura y el mensaje con el nombre.
NAME_WINDOW_MINUTES = 5
REFERENCE_PATTERN = re.compile(r'[0-9]{6,32}')


def _clean_name(text):
    return ' '.join((text or '').split())[:120]


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
                    caption = _clean_name(message.get('image', {}).get('caption'))
                    if caption:
                        receipt._set_customer_name(caption)
                        continue
                    # Nombres que llegaron antes que la captura: el más reciente que coincida.
                    for note in Note.search([
                        ('whatsapp_account_id', '=', account.id), ('sender', '=', sender),
                        ('used', '=', False), ('create_date', '>=', since),
                    ]):
                        note.used = True
                        if receipt._set_customer_name(note.text):
                            break
                elif message.get('type') == 'text':
                    name = _clean_name(message.get('text', {}).get('body'))
                    if not name or REFERENCE_PATTERN.fullmatch(re.sub(r'[\s.\-]', '', name)):
                        continue  # vacío o los dígitos del ID de orden
                    receipt = Receipt.search([
                        ('whatsapp_account_id', '=', account.id), ('sender', '=', sender),
                        ('customer_partner_id', '=', False), ('create_date', '>=', since),
                    ], order='id desc', limit=1)
                    if receipt:
                        receipt._set_customer_name(name)
                    else:
                        Note.create({'whatsapp_account_id': account.id, 'sender': sender, 'text': name})


class BinanceWhatsAppReceipt(models.Model):
    _inherit = 'binance.whatsapp.receipt'

    customer_name = fields.Char(string='Cliente escrito por el gerente', readonly=True, copy=False)
    customer_partner_id = fields.Many2one(
        'res.partner', string='Cliente encontrado', readonly=True, copy=False, ondelete='set null')

    def _find_customer(self, name):
        """Un solo contacto con exactamente ese nombre (sin importar mayúsculas)."""
        self.ensure_one()
        escaped = name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        domain = [('name', '=ilike', escaped)]
        default = (self.account_id or self.verification_id.account_id).payment_partner_id
        if default:
            domain.append(('id', '!=', default.id))
        partners = self.env['res.partner'].sudo().with_context(active_test=True).search(domain, limit=2)
        return partners if len(partners) == 1 else self.env['res.partner']

    def _set_customer_name(self, name):
        """Guarda lo que escribió el gerente. Devuelve True si encontró el contacto.
        Si no lo encuentra (por ejemplo un "gracias"), un mensaje posterior con el nombre
        correcto todavía puede asignarlo; una vez encontrado, ya no cambia."""
        receipt = self.sudo()
        receipt.ensure_one()
        if receipt.customer_partner_id:
            return False
        partner = receipt._find_customer(name)
        receipt.write({'customer_name': name, 'customer_partner_id': partner.id or False})
        receipt._apply_customer_to_payment()
        return bool(partner)

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
