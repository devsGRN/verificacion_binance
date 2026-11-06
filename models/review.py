"""Revisión manual por personas (por ejemplo RRHH) de los pagos que el bot no pudo verificar.

Cuando un comprobante queda pendiente de revisión, cada usuario con el permiso
"Revisor de pagos Binance" recibe una actividad en Odoo (sin correo). La persona revisa
la captura, comprueba el pago en su correo de Binance y decide con ✅ o ❌. Al decidir,
la actividad desaparece para todos.
"""
from zoneinfo import ZoneInfo

from odoo import api, fields, models

REVIEWER_GROUP = 'binance_payment_verify.group_binance_reviewer'
ACTIVITY_TYPE = 'binance_payment_verify.activity_type_binance_review'
TIMEZONE = 'America/Caracas'


class BinanceWhatsAppReceipt(models.Model):
    _name = 'binance.whatsapp.receipt'
    # mail.thread es necesario para las actividades de Odoo (seguidores del revisor).
    _inherit = ['binance.whatsapp.receipt', 'mail.thread', 'mail.activity.mixin']

    read_reference = fields.Char(string='ID leído de la captura', readonly=True, copy=False)
    read_amount = fields.Char(string='Monto leído', readonly=True, copy=False)
    read_currency = fields.Char(string='Moneda leída', readonly=True, copy=False)
    received_utc = fields.Char(string='Recibido (UTC)', compute='_compute_received_times')
    received_local = fields.Char(string='Recibido (Venezuela)', compute='_compute_received_times')
    image = fields.Binary(string='Captura', related='attachment_id.datas', readonly=True)
    needs_review = fields.Boolean(string='Por revisar', compute='_compute_needs_review', store=True)

    @api.depends('create_date')
    def _compute_received_times(self):
        for receipt in self:
            if not receipt.create_date:
                receipt.received_utc = receipt.received_local = False
                continue
            moment = receipt.create_date.replace(tzinfo=ZoneInfo('UTC'))
            receipt.received_utc = moment.strftime('%Y-%m-%d %H:%M:%S (UTC)')
            receipt.received_local = moment.astimezone(ZoneInfo(TIMEZONE)).strftime('%d/%m/%Y %I:%M:%S %p')

    @api.depends('state', 'awaiting_reference', 'needs_new_image', 'final_reply')
    def _compute_needs_review(self):
        for receipt in self:
            receipt.needs_review = (
                receipt.state in ('pending', 'error') and not receipt.final_reply
                and not receipt.awaiting_reference and not receipt.needs_new_image
            )

    # --- Actividades ------------------------------------------------------

    def _review_activities(self):
        activity_type = self.env.ref(ACTIVITY_TYPE, raise_if_not_found=False)
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name), ('res_id', 'in', self.ids),
            ('activity_type_id', '=', activity_type.id if activity_type else False),
        ])

    def _sync_review_activities(self):
        """Crea la actividad para cada revisor cuando hace falta y la quita cuando ya no."""
        activity_type = self.env.ref(ACTIVITY_TYPE, raise_if_not_found=False)
        group = self.env.ref(REVIEWER_GROUP, raise_if_not_found=False)
        if not activity_type or not group:
            return
        existing = self._review_activities()
        done = self.filtered(lambda r: not r.needs_review)
        existing.filtered(lambda a: a.res_id in done.ids).unlink()
        reviewers = group.sudo().users.filtered(lambda u: u.active and not u.share)
        if not reviewers:
            return
        Activity = self.env['mail.activity'].sudo().with_context(mail_activity_quick_update=True)
        model_id = self.env['ir.model']._get_id(self._name)
        for receipt in self.filtered('needs_review'):
            assigned = existing.filtered(lambda a: a.res_id == receipt.id).mapped('user_id')
            for user in reviewers - assigned:
                Activity.create({
                    'activity_type_id': activity_type.id,
                    'res_model_id': model_id,
                    'res_id': receipt.id,
                    'user_id': user.id,
                    'summary': 'Revisar pago Binance',
                    'note': receipt._review_note(),
                    'date_deadline': fields.Date.context_today(receipt),
                })

    def _review_note(self):
        self.ensure_one()
        amount = ('%s %s' % (self.read_amount, self.read_currency or '')).strip() if self.read_amount else 'no leído'
        rows = [
            ('Monto en la captura', amount),
            ('ID en la captura', self.read_reference or self.supplied_reference or 'no leído'),
            ('Recibido (Venezuela)', self.received_local),
            ('Recibido (UTC, como en el correo de Binance)', self.received_utc),
            ('WhatsApp del gerente', self.sender),
            ('Motivo', self.detail or ''),
        ]
        return '<ul>%s</ul>' % ''.join('<li><b>%s:</b> %s</li>' % row for row in rows)

    @api.model_create_multi
    def create(self, vals_list):
        # Sin seguidores ni mensajes de "creado" en cada comprobante: solo la actividad.
        receipts = super(BinanceWhatsAppReceipt, self.with_context(
            mail_create_nosubscribe=True, mail_create_nolog=True, mail_notrack=True,
        )).create(vals_list).with_env(self.env)
        receipts._sync_review_activities()
        return receipts

    def write(self, vals):
        res = super().write(vals)
        if vals.keys() & {'state', 'awaiting_reference', 'needs_new_image', 'final_reply'}:
            self._sync_review_activities()
        return res

    def _remember_reading(self, values):
        self.ensure_one()
        self.write({
            'read_reference': values.get('reference') or False,
            'read_amount': values.get('expected_amount') or False,
            'read_currency': values.get('currency') or False,
        })

    # --- Revisores sin ser administradores ---------------------------------

    def _user_can_review(self):
        user = self.env.user
        return user.has_group('base.group_system') or user.has_group(REVIEWER_GROUP)

    def action_view_image(self):
        self.ensure_one()
        if not self.attachment_id:
            return super().action_view_image()
        # La captura pertenece al chat de WhatsApp; un enlace con token deja verla a quien
        # puede revisar el comprobante, sin darle acceso al chat.
        attachment = self.attachment_id.sudo()
        token = attachment.access_token or attachment.generate_access_token()[0]
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': '/web/content/%s?access_token=%s' % (attachment.id, token)}

    def action_open_receipt(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'current'}


class BinanceManualVerifyWizard(models.TransientModel):
    _inherit = 'binance.manual.verify.wizard'

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        receipt = self.env['binance.whatsapp.receipt'].browse(values.get('receipt_id')).sudo()
        if receipt:
            if not values.get('reference'):
                values['reference'] = receipt.read_reference or False
            if receipt.read_amount and 'expected_amount' in fields_list:
                values['expected_amount'] = receipt.read_amount
            if receipt.read_currency and 'currency' in fields_list:
                values['currency'] = receipt.read_currency
        return values
