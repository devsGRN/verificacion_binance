"""¿El pago en borrador se ve en el aplicativo "Pagos"?

El aplicativo "Pagos" es un menú (hecho con Studio) sobre el mismo modelo account.payment,
pero con un filtro propio (por ejemplo, solo clientes con ciertas etiquetas). En Ajustes se
elige ese menú en "Menú de Pagos" y la verificación muestra si su pago cumple ese mismo
filtro: ✅ se ve / ❌ no se ve (con el motivo y un botón para abrir el contacto).
Solo informa: no cambia la verificación ni el pago.
"""
import logging

from odoo import api, fields, models
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


class BinanceVerificationAccount(models.Model):
    _inherit = 'binance.verification.account'

    pagos_action_id = fields.Many2one(
        'ir.actions.act_window', string='Menú de Pagos', ondelete='set null',
        domain=[('res_model', '=', 'account.payment')],
        help='El menú del aplicativo "Pagos". Se usa su mismo filtro para saber si cada '
             'pago en borrador se ve ahí. Vacío: no se revisa.',
    )

    def _pagos_domain(self):
        """Filtro del menú de Pagos ya evaluado (lista vacía si no hay menú)."""
        self.ensure_one()
        action = self.sudo().pagos_action_id
        if not action:
            return None
        context = dict(self.env.context, allowed_company_ids=self.env.companies.ids)
        eval_context = self.env['ir.actions.actions']._get_eval_context()
        eval_context.update({'context': context, 'allowed_company_ids': self.env.companies.ids,
                             'active_id': False, 'active_ids': []})
        return safe_eval(action.domain or '[]', eval_context)


class BinancePaymentVerification(models.Model):
    _inherit = 'binance.payment.verification'

    pagos_state = fields.Selection(
        [('visible', '✅ Sí'), ('hidden', '❌ No')], string='En Pagos',
        compute='_compute_pagos_state', search='_search_pagos_state',
        help='Si el pago en borrador aparece en el aplicativo "Pagos" (según el "Menú de '
             'Pagos" de Ajustes). Vacío: sin pago o sin menú configurado.',
    )
    pagos_message = fields.Char(string='Motivo', compute='_compute_pagos_state')
    payment_partner_id = fields.Many2one(
        'res.partner', string='Cliente del pago', related='payment_id.partner_id')

    @api.depends('payment_id', 'payment_id.partner_id', 'payment_id.partner_id.category_id',
                 'payment_id.state', 'account_id.pagos_action_id')
    def _compute_pagos_state(self):
        for record in self:
            record.pagos_state = False
            record.pagos_message = False
        Payment = self.env['account.payment'].sudo().with_context(active_test=False)
        for account in self.mapped('account_id'):
            records = self.filtered(lambda r: r.account_id == account and r.payment_id)
            if not records:
                continue
            try:
                domain = account._pagos_domain()
            except Exception as error:
                _logger.exception('Pagos Binance: no se pudo leer el filtro del menú de Pagos')
                for record in records:
                    record.pagos_message = 'No se pudo leer el filtro del menú de Pagos: %s' % error
                continue
            if domain is None:
                continue
            visible = Payment.search(domain + [('id', 'in', records.sudo().payment_id.ids)])
            for record in records:
                payment = record.sudo().payment_id
                if payment in visible:
                    record.pagos_state = 'visible'
                    continue
                record.pagos_state = 'hidden'
                partner = payment.partner_id
                if not partner:
                    record.pagos_message = 'El pago no tiene cliente, así que no aparece en Pagos.'
                else:
                    record.pagos_message = (
                        'El cliente %s no cumple el filtro del menú de Pagos (por ejemplo, no '
                        'tiene la etiqueta necesaria), así que el pago no aparece ahí.' % partner.display_name)

    def _search_pagos_state(self, operator, value):
        records = self.search([('payment_id', '!=', False)])
        wanted = {value} if operator == '=' else set(value) if operator == 'in' else None
        if wanted is None:
            raise NotImplementedError('Operador no soportado para "En Pagos": %s' % operator)
        matching = records.filtered(lambda r: r.pagos_state in wanted)
        return [('id', 'in', matching.ids)]

    def action_open_payment_partner(self):
        self.ensure_one()
        partner = self.sudo().payment_id.partner_id
        if not partner:
            return False
        return {
            'type': 'ir.actions.act_window', 'name': partner.display_name,
            'res_model': 'res.partner', 'res_id': partner.id, 'view_mode': 'form', 'target': 'current',
        }
