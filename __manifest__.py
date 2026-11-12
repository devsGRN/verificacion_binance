{
    'name': 'Pagos Binance',
    'version': '17.0.1.0.19',
    'summary': 'Bot de WhatsApp que verifica pagos de Binance y registra el pago en borrador en Contabilidad',
    'license': 'LGPL-3',
    'depends': ['base', 'whatsapp', 'account'],
    'data': [
        'security/binance_security.xml',
        'security/ir.model.access.csv',
        'views/verification_views.xml',
        'views/receipt_wizard_views.xml',
        'views/whatsapp_receipt_views.xml',
        'data/review_activity.xml',
    ],
    'application': True,
    'installable': True,
}
