# --- server.py (VERSIÓN ROBUSTA + /ping) ---
import os
import json
import logging
import math
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
app = Flask(__name__)
START_TS = datetime.now(timezone.utc)

# --- 2. CREDENCIALES Y CLIENTE DE BINANCE ---
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not all([API_KEY, API_SECRET, WEBHOOK_SECRET]):
    logging.error("FATAL: Faltan variables de entorno (API_KEY, API_SECRET o WEBHOOK_SECRET).")
    exit()

client = Client(API_KEY, API_SECRET, testnet=True)
logging.info("Cliente de Binance inicializado en modo Testnet.")
symbol_info_cache = {}

# --- 3. FUNCIONES AUXILIARES ---
def get_symbol_info(symbol):
    if symbol not in symbol_info_cache:
        try:
            logging.info(f"Obteniendo información de filtros para {symbol} por primera vez.")
            exchange_info = client.futures_exchange_info()
            for s in exchange_info['symbols']:
                if s['symbol'] == symbol:
                    symbol_info_cache[symbol] = {
                        'tickSize': float(next(f['tickSize'] for f in s['filters'] if f['filterType'] == 'PRICE_FILTER')),
                        'stepSize': float(next(f['stepSize'] for f in s['filters'] if f['filterType'] == 'LOT_SIZE')),
                        'minQty': float(next(f['minQty'] for f in s['filters'] if f['filterType'] == 'LOT_SIZE'))
                    }
                    break
        except BinanceAPIException as e:
            logging.error(f"Error obteniendo info para {symbol}: {e}")
            return None
    return symbol_info_cache[symbol]

def adjust_quantity(quantity, step_size, min_qty):
    precision = int(round(-math.log(step_size, 10), 0))
    formatted_quantity = math.floor(quantity * (10**precision)) / (10**precision)
    if formatted_quantity < min_qty:
        logging.error(f"Cantidad calculada ({formatted_quantity}) es menor que el mínimo permitido ({min_qty}).")
        return 0.0
    return formatted_quantity

def close_position_for_symbol(symbol):
    """Cierra cualquier posición abierta para un símbolo."""
    try:
        positions = client.futures_position_information(symbol=symbol)
        position = next((p for p in positions if p['symbol'] == symbol and float(p['positionAmt']) != 0), None)
        if not position:
            logging.info(f"No hay posición abierta para {symbol}. No se requiere cierre.")
            return True, "No position to close."

        position_amount = float(position['positionAmt'])
        side_to_close = 'SELL' if position_amount > 0 else 'BUY'
        quantity_to_close = abs(position_amount)

        client.futures_cancel_all_open_orders(symbol=symbol)
        logging.info(f"Canceladas todas las órdenes abiertas para {symbol} antes de cerrar.")

        logging.info(f"Cerrando posición existente para {symbol}: Lado={side_to_close}, Cantidad={quantity_to_close}")
        close_order = client.futures_create_order(
            symbol=symbol, side=side_to_close, type='MARKET', quantity=quantity_to_close
        )
        logging.info(f"Posición para {symbol} cerrada exitosamente. ID: {close_order['orderId']}")
        return True, close_order['orderId']

    except BinanceAPIException as e:
        if e.code == -2022:
            logging.warning(f"No se pudo cerrar la posición para {symbol} (probablemente ya cerrada). Error: {e}")
            return True, "Position likely already closed."
        logging.error(f"Error de API al cerrar posición para {symbol}: {e}")
        return False, str(e)
    except Exception as e:
        logging.error(f"Error inesperado al cerrar posición para {symbol}: {e}")
        return False, str(e)

# --- 4. ENDPOINTS DE SALUD / KEEP-ALIVE ---
@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "status": "ok",
        "service": "binance-bot",
        "uptime_seconds": int((datetime.now(timezone.utc) - START_TS).total_seconds())
    }), 200

@app.route('/ping', methods=['GET', 'HEAD'])
def ping():
    return jsonify({
        "status": "alive",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int((datetime.now(timezone.utc) - START_TS).total_seconds())
    }), 200

# --- 5. RUTA DEL WEBHOOK UNIFICADO ---
@app.route('/webhook', methods=['POST'])
def webhook():
    # Acepta JSON aunque no venga con Content-Type application/json (caso TradingView)
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON malformado o vacío"}), 400

    logging.info(f"Webhook recibido: {data}")

    if data.get("secret") != WEBHOOK_SECRET:
        logging.warning("Acceso no autorizado (clave secreta inválida).")
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    try:
        symbol = data['symbol'].upper()
        action = data['side'].upper()
    except KeyError as e:
        return jsonify({"status": "error", "message": f"Dato requerido faltante: {e}"}), 400

    # --- LÓGICA DE ACCIÓN ---
    if action == 'CLOSE':
        success, message = close_position_for_symbol(symbol)
        if success:
            return jsonify({"status": "success", "message": f"Orden de cierre para {symbol} procesada.", "details": message}), 200
        else:
            return jsonify({"status": "error", "message": message}), 500

    elif action in ['LONG', 'BUY', 'SHORT', 'SELL']:
        close_success, _ = close_position_for_symbol(symbol)
        if not close_success:
            logging.error(f"No se pudo cerrar la posición existente para {symbol}. Se aborta la nueva orden.")
            return jsonify({"status": "error", "message": "No se pudo cerrar la posición existente antes de abrir una nueva."}), 500

        try:
            side = 'BUY' if action in ['LONG', 'BUY'] else 'SELL'
            leverage = int(float(data['lev']))
            usdt_amount = float(data['usdt'])
            tsl_percent = float(data.get('tsl', 0))

            info = get_symbol_info(symbol)
            if not info:
                raise ValueError(f"No se pudo obtener info para {symbol}")

            client.futures_change_leverage(symbol=symbol, leverage=leverage)
            mark_price = float(client.futures_mark_price(symbol=symbol)['markPrice'])
            quantity_unformatted = (usdt_amount * leverage) / mark_price
            quantity = adjust_quantity(quantity_unformatted, info['stepSize'], info['minQty'])

            if quantity <= 0:
                msg = f"La cantidad calculada ({quantity_unformatted:.8f}) es demasiado pequeña."
                logging.error(msg)
                return jsonify({"status": "error", "message": msg}), 400

            logging.info(f"Abriendo {side} para {quantity} {symbol} a precio de mercado.")
            order = client.futures_create_order(symbol=symbol, side=side, type='MARKET', quantity=quantity)
            logging.info(f"¡ÉXITO! Orden MARKET enviada. ID: {order['orderId']}")

            if 0.1 <= tsl_percent <= 5:
                tsl_side = 'SELL' if side == 'BUY' else 'BUY'
                tsl_order = client.futures_create_order(
                    symbol=symbol, side=tsl_side, type='TRAILING_STOP_MARKET',
                    quantity=quantity, callbackRate=tsl_percent, workingType='MARK_PRICE'
                )
                logging.info(f"Orden Trailing Stop ({tsl_percent}%) colocada. ID: {tsl_order['orderId']}")

            return jsonify({"status": "success", "orderId": order['orderId'], "message": "Operación completada"}), 200

        except (KeyError, ValueError) as e:
            return jsonify({"status": "error", "message": f"Datos incompletos o inválidos para abrir orden: {e}"}), 400
        except BinanceAPIException as e:
            logging.error(f"Error de la API de Binance al abrir: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
        except Exception as e:
            logging.error(f"Un error inesperado ocurrió al abrir: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    else:
        return jsonify({"status": "error", "message": f"Acción '{action}' no reconocida. Usar LONG, SHORT o CLOSE."}), 400

if __name__ == '__main__':
    # Nota: en producción usá gunicorn:  gunicorn server:app --preload --timeout 120 --workers 1 --threads 4
    app.run(host='0.0.0.0', port=5000, debug=False)
