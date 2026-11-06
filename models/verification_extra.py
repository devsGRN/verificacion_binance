from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_round

from ..services.binance import positive_decimal


class BinancePaymentVerification(models.Model):
    _inherit = 'binance.payment.verification'

    received_amount = fields.Float(
        string='Importe recibido', compute='_compute_received_amount', digits=(16, 2), store=True,
    )
    receipt_ids = fields.One2many('binance.whatsapp.receipt', 'verification_id', string='Comprobantes de WhatsApp')
    whatsapp_sender = fields.Char(string='WhatsApp del gerente', compute='_compute_whatsapp_sender')

    @api.depends('state', 'confirmed_amount', 'expected_amount')
    def _compute_received_amount(self):
        for record in self:
            record.received_amount = record._received_amount()

    def _received_amount(self):
        self.ensure_one()
        if self.state != 'confirmed':
            return 0.0
        try:
            return float_round(float(positive_decimal(self.confirmed_amount or self.expected_amount)),
                               precision_digits=2)
        except ValueError:
            return 0.0

    @api.depends('receipt_ids.sender')
    def _compute_whatsapp_sender(self):
        for record in self:
            record.whatsapp_sender = record.receipt_ids[:1].sender

    def action_view_receipt_image(self):
        self.ensure_one()
        receipt = self.receipt_ids.filtered('attachment_id')[:1]
        if not receipt:
            raise ValidationError('Este pago no tiene captura de WhatsApp.')
        return receipt.action_view_image()
