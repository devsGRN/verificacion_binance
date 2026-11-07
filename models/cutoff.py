"""Fecha de corte del registro automático (adaptado a Canon).

Los pagos hechos en Binance desde la fecha de corte se verifican y registran solos, como
siempre (✅ y pago en borrador en Contabilidad). Los pagos ANTERIORES a esa fecha pueden
haberse registrado ya a mano antes de usar el bot, así que no se registran solos:
  - el bot los busca igual en Binance (la fecha es la real del pago según Binance, no la
    de la captura);
  - el gerente recibe "⚠️ Este pago es del ..., anterior al ... Queda en revisión.";
  - los números de "Avisar pagos anteriores al corte" reciben un WhatsApp con la fecha, el
    importe y el ID según Binance, para revisar que no esté ya registrado;
  - en Odoo se decide con ✅ (se crea el pago en borrador en Contabilidad) o ❌ (no se crea
    nada). En Canon no se crean actividades para estos pagos.
"""
import logging
import re
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .whatsapp_receipt import normalize_number

_logger = logging.getLogger(__name__)

TIMEZONE = 'America/Caracas'


def _local_date(moment):
    """Datetime UTC (con o sin zona) -> fecha en Venezuela."""
    if not moment:
        return None
    moment = fields.Datetime.to_datetime(moment) if isinstance(moment, str) else moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo('UTC'))
    return moment.astimezone(ZoneInfo(TIMEZONE)).date()


def _format(day):
    return day.strftime('%d/%m/%Y') if day else ''


class BinanceVerificationAccount(models.Model):
    _inherit = 'binance.verification.account'

    auto_register_from = fields.Date(
        string='Registro automático desde',
        help='Los pagos hechos en Binance desde esta fecha (hora de Venezuela) se verifican y '
             'registran solos. Los anteriores quedan pendientes y se avisa por WhatsApp para '
             'revisar que no estuvieran ya registrados. Vacío: todos los pagos se registran solos.',
    )
    cutoff_notify_numbers = fields.Text(
        string='Avisar pagos anteriores al corte',
        help='Números de WhatsApp que reciben el aviso de cada pago anterior a la fecha de corte, '
             'uno por línea (por ejemplo +58 414-1234567).',
    )

    def _cutoff_notify_set(self):
        self.ensure_one()
        numbers = re.split(r'[\n,;]+', self.cutoff_notify_numbers or '')
        return [number for number in dict.fromkeys(normalize_number(item) for item in numbers) if number]

    @api.constrains('cutoff_notify_numbers')
    def _check_cutoff_notify_numbers(self):
        for account in self:
            for number in account._cutoff_notify_set():
                if not re.fullmatch(r'[1-9][0-9]{6,14}', number):
                    raise ValidationError('Número de WhatsApp inválido para el aviso: %s' % number)


class BinancePaymentVerification(models.Model):
    _inherit = 'binance.payment.verification'

    def _payment_local_date(self):
        self.ensure_one()
        return _local_date(self.paid_at) or self.operation_day


class BinanceWhatsAppReceipt(models.Model):
    _inherit = 'binance.whatsapp.receipt'

    before_cutoff = fields.Boolean(
        string='Pago anterior a la fecha de corte', readonly=True, copy=False,
        help='Binance confirmó el pago, pero es anterior a la fecha de registro automático: '
             'hay que revisar que no esté ya registrado.',
    )
    cutoff_notice = fields.Char(string='Aviso por WhatsApp', readonly=True, copy=False)

    def _link_confirmed_verification(self, record, values, result=None):
        if result is not None and record.state != 'confirmed':
            cutoff = record.account_id.auto_register_from
            paid_day = _local_date(result.paid_at) or fields.Date.to_date(values.get('operation_day'))
            if cutoff and paid_day and paid_day < cutoff:
                return self._hold_before_cutoff(record, result, paid_day, cutoff)
        return super()._link_confirmed_verification(record, values, result=result)

    def _hold_before_cutoff(self, record, result, paid_day, cutoff):
        message = ('Pago del %s según Binance, anterior a la fecha de corte (%s). '
                   'Pendiente de revisión: confirmar que no esté ya registrado.'
                   % (_format(paid_day), _format(cutoff)))
        if not record._hold_for_review(result, message):
            # Otro comprobante lo acaba de verificar: responder como pago ya validado.
            return super()._link_confirmed_verification(record, {
                'currency': record.currency, 'expected_amount': record.expected_amount,
                'operation_day': record.operation_day,
            })
        self.write({
            'account_id': record.account_id.id,
            'verification_id': record.id,
            'before_cutoff': True,
            'state': 'pending',
            'detail': message,
            'awaiting_reference': False,
            'reply_state': 'queued',
        })
        return None

    def _in_review_message(self):
        if self.before_cutoff and self.verification_id:
            cutoff = self.verification_id.account_id.auto_register_from
            return ('⚠️ Este pago es del %s, anterior al %s. Queda en revisión.'
                    % (_format(self.verification_id._payment_local_date()), _format(cutoff)))
        return super()._in_review_message()

    # --- Aviso por WhatsApp (en lugar de actividades) -----------------------

    def _sync_review_activities(self):
        held = self.filtered('before_cutoff')
        if held:
            # En Canon estos pagos se avisan por WhatsApp, no con actividades.
            held._review_activities().unlink()
        return super(BinanceWhatsAppReceipt, self - held)._sync_review_activities()

    def _post_reply(self):
        res = super()._post_reply()
        if (self.before_cutoff and self.reply_state == 'posted' and not self.cutoff_notice
                and self.state == 'pending'):
            try:
                with self.env.cr.savepoint():
                    self._notify_cutoff_reviewers()
            except Exception as error:
                # Nunca deshacer la respuesta ya enviada al gerente por un fallo del aviso.
                _logger.exception('Pagos Binance: falló el aviso del pago anterior al corte %s', self.id)
                self.cutoff_notice = ('No se pudo enviar el aviso: %s' % error)[:250]
        return res

    def _cutoff_notice_body(self):
        verification = self.verification_id.sudo()
        return (
            '⚠️Pago Binance anterior a la fecha de corte\n'
            'Gerente: +%s\n'
            'Fecha del pago (Binance): %s\n'
            'ID de orden: %s\n'
            'Revisar que no esté registrado y decidir en Odoo'
        ) % (self.sender, _format(verification._payment_local_date()), verification.reference)

    def _notify_cutoff_reviewers(self):
        """Envía el aviso a cada número configurado. Nunca interrumpe la respuesta al gerente.

        WhatsApp solo deja enviar mensajes libres a quien escribió a la línea en las últimas
        24 h; si no, Meta lo rechaza y el motivo queda en "Aviso por WhatsApp"."""
        self.ensure_one()
        account = self.verification_id.account_id
        numbers = account._cutoff_notify_set()
        if not numbers:
            self.cutoff_notice = 'No hay números configurados para el aviso.'
            return
        body = self._cutoff_notice_body()
        results = []
        for number in numbers:
            try:
                with self.env.cr.savepoint():
                    channel = self.env['discuss.channel'].sudo()._get_whatsapp_channel(
                        whatsapp_number=number, wa_account_id=self.whatsapp_account_id,
                        sender_name=number, create_if_not_found=True,
                    )
                    posted = channel.message_post(
                        body=body, message_type='whatsapp_message', subtype_xmlid='mail.mt_comment',
                    )
                    self._send_whatsapp_now(posted)
                outgoing = self.env['whatsapp.message'].sudo().search(
                    [('mail_message_id', '=', posted.id)], limit=1)
                if outgoing.state in ('error', 'bounced'):
                    results.append('+%s: no enviado (%s)' % (
                        number, outgoing.failure_reason or 'WhatsApp lo rechazó'))
                else:
                    results.append('+%s: enviado' % number)
            except Exception as error:
                _logger.exception('Pagos Binance: no se pudo avisar a %s del pago anterior al corte', number)
                results.append('+%s: no enviado (%s)' % (number, error))
        self.cutoff_notice = '; '.join(results)[:250]


class BinanceManualVerifyWizard(models.TransientModel):
    _inherit = 'binance.manual.verify.wizard'

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        receipt = self.env['binance.whatsapp.receipt'].browse(values.get('receipt_id')).sudo()
        verification = receipt.verification_id
        if receipt.before_cutoff and verification and verification.state != 'confirmed':
            # Los datos reales que encontró Binance: así quien revisa solo tiene que aprobar.
            prefill = {
                'account_id': verification.account_id.id,
                'reference': verification.reference,
                'operation_day': verification.operation_day,
                'currency': verification.currency,
                'expected_amount': verification.expected_amount,
            }
            values.update({key: value for key, value in prefill.items() if key in fields_list})
        return values
