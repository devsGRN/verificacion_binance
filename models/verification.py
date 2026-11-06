import os
import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.binance import BinanceReader, VerificationUnavailable, positive_decimal


class BinanceVerificationAccount(models.Model):
    _name = 'binance.verification.account'
    _description = 'Cuenta receptora Binance - piloto'
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        'res.company', required=False,
        help='Deja este campo vacío para usar la cuenta receptora en todas las compañías.',
    )
    receiver_uid = fields.Char(required=True, help='UID de la cuenta receptora, no del cliente.')
    credential_prefix = fields.Char(required=True, help='Ejemplo: BINANCE_MAIN. No introducir claves aquí.')
    api_key = fields.Char(
        string='API Key', groups='base.group_system', copy=False,
    )
    api_secret = fields.Char(
        string='Secret Key', groups='base.group_system', copy=False,
    )


    _sql_constraints = [('receiver_unique', 'unique(receiver_uid)',
                         'Esta cuenta receptora ya está configurada.')]

    @api.constrains('credential_prefix', 'receiver_uid')
    def _check_configuration(self):
        for rec in self:
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', rec.credential_prefix):
                raise ValidationError('El prefijo debe usar letras mayúsculas, números y guiones bajos.')
            if not re.fullmatch(r'[0-9]+', rec.receiver_uid):
                raise ValidationError('El UID receptor debe contener solo dígitos.')

    def _binance_reader(self):
        self.ensure_one()
        api_key = (self.api_key or '').strip()
        api_secret = (self.api_secret or '').strip()
        if api_key or api_secret:
            if not api_key or not api_secret:
                raise VerificationUnavailable('Complete API Key y Secret Key en la cuenta receptora.')
        else:
            api_key = os.environ.get(self.credential_prefix + '_API_KEY', '').strip()
            api_secret = os.environ.get(self.credential_prefix + '_API_SECRET', '').strip()
        return BinanceReader(api_key, api_secret)

    def _find_pay_by_suffix(self, suffix, amount=None):
        self.ensure_one()
        return self._binance_reader().find_pay_by_suffix(suffix, self.receiver_uid, amount)

    def _query_binance(self, kind, reference, currency, amount, operation_day, network=''):
        self.ensure_one()
        return self._binance_reader().verify(
            kind, reference, currency, amount, self.receiver_uid, operation_day, network,
        )

    def write(self, vals):
        protected = self.filtered(
            lambda account: {'receiver_uid', 'credential_prefix'} & vals.keys()
            or (vals.get('company_id') and vals['company_id'] != account.company_id.id)
        )
        if protected:
            if self.env['binance.payment.verification'].search_count([('account_id', 'in', protected.ids)]):
                raise UserError('No se puede cambiar la identidad de una cuenta con verificaciones.')
        return super().write(vals)


class BinancePaymentVerification(models.Model):
    _name = 'binance.payment.verification'
    _description = 'Verificación de ingreso Binance - piloto'
    _rec_name = 'reference'
    _order = 'id desc'
    _check_company_auto = True

    account_id = fields.Many2one('binance.verification.account', required=True, ondelete='restrict', check_company=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía',
        default=lambda self: self.env.company,
    )
    kind = fields.Selection([('pay', 'Historial Binance Pay'), ('deposit', 'Depósito por red')], required=True)
    reference = fields.Char(required=True, help='ID de la orden o transactionId para Binance Pay. txId para depósitos por red.')
    operation_day = fields.Date(required=True, help='Día UTC del ingreso. La búsqueda del piloto se limita a este día.')
    currency = fields.Char(required=True, default='USDT')
    expected_amount = fields.Char(required=True, help='Decimal con punto, sin separador de miles: 1000.00')
    network = fields.Char(help='Nombre exacto de red devuelto por Binance; obligatorio en depósitos.')
    state = fields.Selection([
        ('draft', 'Sin consultar'), ('pending', 'Pendiente'), ('confirmed', 'Verificado'),
        ('mismatch', 'Discrepancia'), ('unavailable', 'Consulta no disponible'),
    ], default='draft', required=True, readonly=True)
    result_message = fields.Text(readonly=True)
    canonical_id = fields.Char(readonly=True, copy=False)
    confirmed_amount = fields.Char(readonly=True, copy=False)
    checked_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ('request_unique', 'unique(account_id, kind, reference)', 'La referencia ya está registrada en esta cuenta.'),
        ('canonical_unique', 'unique(account_id, kind, canonical_id)', 'El ingreso ya tiene un registro.'),
    ]
    _RESULT_FIELDS = {'state', 'result_message', 'canonical_id', 'confirmed_amount', 'checked_at'}

    @api.onchange('company_id')
    def _onchange_company_id(self):
        if self.account_id.company_id and self.account_id.company_id != self.company_id:
            self.account_id = False


    @api.constrains('expected_amount', 'kind', 'network', 'reference', 'currency', 'operation_day')
    def _check_input(self):
        for rec in self:
            try:
                positive_decimal(rec.expected_amount)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            if rec.kind == 'deposit' and not rec.network:
                raise ValidationError('Especifique la red del depósito.')
            if rec.reference != rec.reference.strip() or not re.fullmatch(r'[A-Za-z0-9_:\-]{1,256}', rec.reference):
                raise ValidationError('Referencia inválida o con espacios.')
            if not re.fullmatch(r'[A-Z0-9]{1,24}', rec.currency):
                raise ValidationError('La moneda debe ser un código en mayúsculas.')
            if rec.operation_day > fields.Date.today():
                raise ValidationError('No se puede consultar una fecha futura.')

    @api.model_create_multi
    def create(self, vals_list):
        if any(self._RESULT_FIELDS & vals.keys() for vals in vals_list):
            raise UserError('Los resultados solo pueden generarse mediante la consulta a Binance.')
        # Explicit defaults also prevent default_* context keys from forging results.
        safe = [dict(vals, state='draft', result_message=False, canonical_id=False,
                     confirmed_amount=False, checked_at=False) for vals in vals_list]
        return super().create(safe)

    # Campos del sistema que se pueden escribir en un pago ya verificado (no tocan el resultado).
    _SYSTEM_FIELDS = {'payment_id', 'payment_error'}

    def write(self, vals):
        if vals and vals.keys() <= self._SYSTEM_FIELDS:
            return super().write(vals)
        if self._RESULT_FIELDS & vals.keys():
            raise UserError('No se permite modificar manualmente un resultado.')
        if any(rec.state != 'draft' for rec in self):
            raise UserError('No se puede modificar una solicitud ya consultada.')
        return super().write(vals)

    def copy(self, default=None):
        raise UserError('Cree una nueva solicitud con su referencia única.')

    @api.model
    def _get_receipt_verification(self, account, values, company):
        existing = self.search([
            ('account_id', '=', account.id), ('kind', '=', 'pay'),
            ('reference', '=', values['reference']),
        ], limit=1)
        return existing or self.create(dict(values, account_id=account.id,
                                            company_id=company.id, kind='pay'))

    def action_verify(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise UserError('El piloto solo está disponible para administradores.')
        self.check_access_rights('write')
        self.check_access_rule('write')
        return self._verify()

    def _confirm_manually(self, reviewer):
        """Confirmación hecha por una persona tras revisar el pago; no consulta Binance."""
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM binance_payment_verification WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.state == 'confirmed':
            return False
        super(BinancePaymentVerification, self).write({
            'state': 'confirmed',
            'result_message': 'Verificado manualmente por %s' % reviewer.name,
            'canonical_id': self.reference,
            'confirmed_amount': self.expected_amount,
            'checked_at': fields.Datetime.now(),
        })
        return True

    def _verify(self, result=None):
        self.ensure_one()
        # Serialize attempts on the same row; constraints also protect distinct rows.
        self.env.cr.execute('SELECT id FROM binance_payment_verification WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.state == 'confirmed':
            return False
        try:
            result = result or self.account_id._query_binance(
                self.kind, self.reference, self.currency, self.expected_amount, self.operation_day, self.network or '',
            )
            vals = {'state': result.state, 'result_message': result.message,
                    'canonical_id': result.canonical_id or False,
                    'confirmed_amount': result.amount or False}
        except VerificationUnavailable as exc:
            vals = {'state': 'unavailable', 'result_message': str(exc)}
        vals['checked_at'] = fields.Datetime.now()
        super(BinancePaymentVerification, self).write(vals)
        return True
