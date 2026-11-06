# Pagos Binance — Canon 2021

Módulo de Odoo 17 (Odoo.sh) para canon-2021. Es una adaptación del bot de usp-carga
(`devsGRN/binance_payment_verify`), sin Google Sheet ni compañías para distribuir.

## Qué hace
1. Los gerentes autorizados envían la captura del pago de Binance por WhatsApp.
2. El bot lee la captura (ID de orden y monto) y verifica el pago en el historial de
   Binance Pay de la cuenta receptora (API de solo lectura).
3. Si no puede leer el ID, pide los últimos 6 dígitos; si aun así no lo encuentra,
   el pago pasa a revisión de un **Revisor de pagos** (actividad en Odoo, sin correo),
   que decide con ✅ Pago verificado / ❌ Pago errado.
4. Cada pago verificado crea en **Contabilidad → Clientes → Pagos** un pago en
   **borrador**: diario configurado (Binance), tipo Recibir, USD, fecha del pago,
   importe y memo `USDT REF: <referencia>`. El cliente se deja vacío.

## Configuración
- **Pagos Binance → Configuración → Ajustes:** cuenta receptora (UID, API Key y
  Secret de solo lectura) y **Diario de pagos** (Binance).
- **Pagos Binance → Configuración → Números de WhatsApp con el bot:** línea de
  WhatsApp, modo **Gerentes** y los números de los gerentes (uno por línea).
- **Ajustes → Usuarios:** permiso **Pagos Binance → Revisor de pagos** para quien
  revise los pagos que el bot no pudo verificar.
