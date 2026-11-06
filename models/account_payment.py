"""Registro en Contabilidad de cada pago Binance verificado.

Cuando un pago queda Verificado (por el bot o por un revisor), se crea en
Contabilidad → Clientes → Pagos un pago en BORRADOR:
  diario Binance, tipo Recibir, moneda USD, fecha del pago, importe verificado,
  memo "USDT REF: <referencia>" y cliente vacío (lo completa contabilidad).
Se crea una sola vez por verificación. Si falla, el pago queda verificado igual y
el motivo se ve en la verificación, con un botón para reintentarlo.
"""
import logging
from zoneinfo import ZoneInfo

from odoo import SUPERUSER_ID, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEZONE = 'America/Caracas'


class BinanceVerificationAccount(models.Model):
    _inherit = 'binance.verification.account'

    payment_journal_id = fields.Many2one(
        'account.journal', string='Diario de pagos',
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help='Diario donde se crean los pagos en borrador (por ejemplo "Binance").',
    )


class BinancePaymentVerification(models.Model):
    _inherit = 'binance.payment.verification'

    payment_id = fields.Many2one(
        'account.payment', string='Pago en Contabilidad', readonly=True, copy=False,
        groups='account.group_account_invoice,account.group_account_readonly',
    )
    payment_error = fields.Char(string='Error al crear el pago', readonly=True, copy=False)
    has_payment = fields.Boolean(string='Pago creado', compute='_compute_has_payment')

    def _compute_has_payment(self):
        for record in self:
            record.has_payment = bool(record.sudo().payment_id)

    def _verify(self, result=None):
        res = super()._verify(result=result)
        self._create_draft_payment()
        return res

    def _confirm_manually(self, reviewer):
        res = super()._confirm_manually(reviewer)
        self._create_draft_payment()
        return res

    def _payment_date(self):
        self.ensure_one()
        moment = fields.Datetime.to_datetime(self.checked_at or fields.Datetime.now())
        return moment.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(TIMEZONE)).date()

    def _create_draft_payment(self):
        """Crea el pago en borrador. Nunca interrumpe la verificación."""
        for record in self.sudo().filtered(lambda r: r.state == 'confirmed' and not r.payment_id):
            try:
                with self.env.cr.savepoint():
                    record._create_draft_payment_one()
            except Exception as error:
                message = error.args[0] if isinstance(error, UserError) else 'Error inesperado; ver logs.'
                if not isinstance(error, UserError):
                    _logger.exception('No se pudo crear el pago en Contabilidad de la verificación %s', record.id)
                record.sudo().write({'payment_error': message})

    def _create_draft_payment_one(self):
        self.ensure_one()
        journal = self.account_id.payment_journal_id
        if not journal:
            raise UserError('Configura el "Diario de pagos" en la cuenta receptora %s.' % self.account_id.name)
        amount = self._received_amount()
        if not amount:
            raise UserError('El pago verificado no tiene un importe válido.')
        currency = self.env.ref('base.USD')
        payment = self.env['account.payment'].with_user(SUPERUSER_ID).with_company(journal.company_id).create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'journal_id': journal.id,
            'amount': amount,
            'currency_id': currency.id,
            'date': self._payment_date(),
            'ref': 'USDT REF: %s' % self.reference,
        })
        self.sudo().write({'payment_id': payment.id, 'payment_error': False})

    def action_create_payment(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError('Solo un administrador puede crear el pago en Contabilidad.')
        self._create_draft_payment()
        failed = self.sudo().filtered(lambda r: not r.payment_id and r.payment_error)
        if failed:
            raise UserError(failed[0].payment_error)

    def action_open_payment(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.payment',
                'res_id': self.sudo().payment_id.id, 'view_mode': 'form', 'target': 'current'}
