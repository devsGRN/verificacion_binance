import base64
from binascii import Error as Base64Error

from odoo import fields, models
from odoo.exceptions import UserError

from ..services.receipt_ocr import ReceiptError, read_receipt


class BinanceReceiptWizard(models.TransientModel):
    _name = 'binance.receipt.wizard'
    _description = 'Leer comprobante de Binance Pay'
    _check_company_auto = True

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    account_id = fields.Many2one('binance.verification.account', required=True, check_company=True)
    image = fields.Binary(string='Captura', required=True, attachment=False)
    filename = fields.Char()
    screenshot_timezone = fields.Char(string='Zona horaria de la captura', required=True, default='America/Caracas')

    def action_read_receipt(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise UserError('Solo un administrador puede utilizar esta prueba.')
        self.check_access_rights('read')
        self.check_access_rule('read')
        self._check_company()
        if self.company_id not in self.env.companies:
            raise UserError('Selecciona una compañía activa en tu sesión.')
        encoded = self.with_context(bin_size=False).image
        if not encoded or len(encoded) > 7 * 1024 * 1024:
            raise UserError('Selecciona una imagen JPG o PNG de hasta 5 MB.')
        try:
            values = read_receipt(base64.b64decode(encoded, validate=True), self.screenshot_timezone)
        except (Base64Error, ReceiptError) as error:
            raise UserError(str(error)) from None
        record = self.env['binance.payment.verification']._get_receipt_verification(
            self.account_id, values, self.company_id,
        )
        record.action_verify()
        return {
            'type': 'ir.actions.act_window', 'name': 'Verificación de ingreso',
            'res_model': record._name, 'res_id': record.id, 'view_mode': 'form', 'target': 'current',
        }
