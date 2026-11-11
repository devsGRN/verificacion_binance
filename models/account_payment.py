"""Registro en Contabilidad de cada pago Binance verificado.

Cuando un pago queda Verificado (por el bot o por un revisor), se crea en
Contabilidad → Clientes → Pagos un pago en BORRADOR:
  diario Binance, tipo Recibir, moneda USD, fecha real del pago según Binance, importe verificado,
  memo "USDT REF: <últimos 6 dígitos>" y el "Cliente por defecto" de la cuenta
  receptora (por ejemplo "BINANCE - POR ASIGNAR"); si no hay, el cliente queda vacío.
  Contabilidad reemplaza ese cliente por el real antes de confirmar.
Solo para ABONOS: los pagos de contado (el gerente responde "2" en WhatsApp) solo se
verifican y no se registran. Se crea una sola vez por verificación. Si falla, el pago queda verificado igual y
el motivo se ve en la verificación, con un botón para reintentarlo.
La captura que mandó el gerente por WhatsApp se adjunta al pago y se muestra en su chatter.
"""
import logging
from zoneinfo import ZoneInfo

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

TIMEZONE = 'America/Caracas'


class BinanceVerificationAccount(models.Model):
    _inherit = 'binance.verification.account'

    payment_journal_id = fields.Many2one(
        'account.journal', string='Diario de pagos',
        domain="[('type', 'in', ('bank', 'cash'))]",
        help='Diario donde se crean los pagos en borrador (por ejemplo "Binance").',
    )
    payment_partner_id = fields.Many2one(
        'res.partner', string='Cliente por defecto',
        help='Cliente provisional de los pagos en borrador (por ejemplo "BINANCE - POR ASIGNAR"). '
             'Contabilidad lo reemplaza por el cliente real antes de confirmar. '
             'Si se deja vacío, el pago se crea sin cliente.',
    )

    @api.constrains('payment_journal_id', 'company_id')
    def _check_payment_journal_company(self):
        for account in self:
            journal = account.payment_journal_id
            if journal and account.company_id and journal.company_id != account.company_id:
                raise ValidationError('El diario "%s" es de otra compañía (%s).'
                                      % (journal.name, journal.company_id.name))


class BinancePaymentVerification(models.Model):
    _inherit = 'binance.payment.verification'

    payment_id = fields.Many2one(
        'account.payment', string='Pago en Contabilidad', readonly=True, copy=False,
        groups='account.group_account_invoice,account.group_account_readonly',
    )
    payment_error = fields.Char(string='Error al crear el pago', readonly=True, copy=False)
    payment_capture_attached = fields.Boolean(readonly=True, copy=False)
    has_payment = fields.Boolean(string='Pago creado', compute='_compute_has_payment')
    payment_type = fields.Selection(
        [('abono', 'Abono'), ('vitrina', 'Pago de contado')], string='Tipo', readonly=True, copy=False,
        help='Abono: se registra en Pagos. Pago de contado: solo se verifica; no se registra en Pagos.')

    def _compute_has_payment(self):
        for record in self:
            record.has_payment = bool(record.sudo().payment_id)

    def _default_to_abono(self):
        """Verificaciones que no pasan por la pregunta del bot (revisor o administrador): abono."""
        self.sudo().filtered(lambda r: r.state == 'confirmed' and not r.payment_type).write(
            {'payment_type': 'abono'})

    def _verify(self, result=None):
        res = super()._verify(result=result)
        if not self.env.context.get('binance_ask_type'):
            self._default_to_abono()
        self._create_draft_payment()
        return res

    def _confirm_manually(self, reviewer):
        res = super()._confirm_manually(reviewer)
        self._default_to_abono()
        self._create_draft_payment()
        return res

    def _payment_date(self):
        """Fecha real del pago según Binance (hora de Caracas). Así un pago viejo aprobado
        después por el revisor queda con su fecha y no con la de la aprobación."""
        self.ensure_one()
        if self.paid_at:
            moment = fields.Datetime.to_datetime(self.paid_at)
        elif self.operation_day:
            return self.operation_day
        else:
            moment = fields.Datetime.to_datetime(self.checked_at or fields.Datetime.now())
        return moment.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(TIMEZONE)).date()

    def _create_draft_payment(self):
        """Crea el pago en borrador. Nunca interrumpe la verificación."""
        # Solo los abonos se registran en Pagos; los pagos de vitrina solo se verifican.
        for record in self.sudo().filtered(
                lambda r: r.state == 'confirmed' and not r.payment_id and r.payment_type == 'abono'):
            try:
                with self.env.cr.savepoint():
                    record._create_draft_payment_one()
            except Exception as error:
                message = error.args[0] if isinstance(error, UserError) else 'Error inesperado; ver logs.'
                if not isinstance(error, UserError):
                    _logger.exception('No se pudo crear el pago en Contabilidad de la verificación %s', record.id)
                record.sudo().write({'payment_error': message})

    def _short_reference(self):
        """Últimos 6 dígitos del ID de orden (o la referencia completa si es más corta)."""
        self.ensure_one()
        reference = (self.reference or '').strip()
        if self.kind == 'deposit' and not reference.startswith('offchain-'):
            return reference[-6:].upper()  # hash del depósito: últimos 6 caracteres
        digits = ''.join(ch for ch in reference if ch.isdigit())
        return (digits or reference)[-6:]

    def _create_draft_payment_one(self):
        self.ensure_one()
        journal = self.account_id.payment_journal_id
        if not journal:
            raise UserError('Configura el "Diario de pagos" en la cuenta receptora %s.' % self.account_id.name)
        amount = self._received_amount()
        if not amount:
            raise UserError('El pago verificado no tiene un importe válido.')
        currency = self.env.ref('base.USD')
        values = {
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'journal_id': journal.id,
            'amount': amount,
            'currency_id': currency.id,
            'date': self._payment_date(),
            'ref': 'USDT REFF %s' % self._short_reference(),
        }
        partner = self.account_id.payment_partner_id
        if partner:
            values['partner_id'] = partner.id
        payment = self.env['account.payment'].with_user(SUPERUSER_ID).with_company(journal.company_id).create(values)
        self.sudo().write({'payment_id': payment.id, 'payment_error': False})
        self._attach_capture_to_payment()
        self.receipt_ids._apply_customer_to_payment()

    def _attach_capture_to_payment(self):
        """Adjunta la captura de WhatsApp al pago en borrador y la muestra en su chatter.

        Se llama al crear el pago y también cuando el comprobante se enlaza después a la
        verificación (en el bot el pago se crea un instante antes de ese enlace).
        Nunca interrumpe nada: si falla, solo queda en el log."""
        for record in self.sudo().filtered(lambda r: r.payment_id and not r.payment_capture_attached):
            receipt = record.receipt_ids.filtered('attachment_id')[:1]
            if not receipt:
                continue
            try:
                with self.env.cr.savepoint():
                    source = receipt.attachment_id.sudo()
                    capture = source.copy({
                        'res_model': 'account.payment', 'res_id': record.payment_id.id,
                        'name': source.name or 'captura_binance.jpg',
                    })
                    record.payment_id.with_user(SUPERUSER_ID).message_post(
                        body='Captura del pago Binance enviada por WhatsApp (+%s).' % receipt.sender,
                        attachment_ids=capture.ids,
                        message_type='comment', subtype_xmlid='mail.mt_note',
                    )
                    record.write({'payment_capture_attached': True})
            except Exception:
                _logger.exception('No se pudo adjuntar la captura al pago de la verificación %s', record.id)

    def action_create_payment(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError('Solo un administrador puede crear el pago en Contabilidad.')
        if self.sudo().filtered(lambda r: r.payment_type == 'vitrina'):
            raise UserError('Es un pago de contado: solo se verifica y no se registra en Pagos.')
        self._default_to_abono()
        self._create_draft_payment()
        failed = self.sudo().filtered(lambda r: not r.payment_id and r.payment_error)
        if failed:
            raise UserError(failed[0].payment_error)

    def action_open_payment(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.payment',
                'res_id': self.sudo().payment_id.id, 'view_mode': 'form', 'target': 'current'}


class BinanceWhatsAppReceipt(models.Model):
    _inherit = 'binance.whatsapp.receipt'

    def write(self, vals):
        res = super().write(vals)
        if vals.get('verification_id'):
            self.sudo().verification_id._attach_capture_to_payment()
            self._apply_customer_to_payment()
        return res
