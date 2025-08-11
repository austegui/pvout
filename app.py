import os
import json
import math
import re
import time
import logging
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# ------------------------------------------------------------------------------
# App & CORS
# ------------------------------------------------------------------------------
app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": ["*", "null"]}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    supports_credentials=False,
    max_age=600,
)

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

# ------------------------------------------------------------------------------
# Webhook (no depende de config.json)
# ------------------------------------------------------------------------------
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://n8n.soldeia.com/webhook/9dd2c57b-40e9-40bf-97ad-fd45d74acc30",
)

def send_webhook(event: str, payload: dict):
    """Envía SIEMPRE al webhook; loguea éxito/fracaso sin romper el flujo."""
    try:
        logging.info(f"[webhook] POST -> {WEBHOOK_URL} event={event}")
        resp = requests.post(
            WEBHOOK_URL,
            json={"event": event, "payload": payload},
            headers={"User-Agent": "solar-solution-backend/1.0"},
            timeout=10,
        )
        logging.info(f"[webhook] status={resp.status_code} body={resp.text[:300]}")
        if resp.status_code >= 400:
            logging.error(f"[webhook] HTTP {resp.status_code} al enviar a n8n")
    except Exception:
        logging.exception("[webhook] Error enviando a n8n")

# ------------------------------------------------------------------------------
# Carga de configuración y datos
# ------------------------------------------------------------------------------
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("pvout_data.json", "r", encoding="utf-8") as f:
    PVOUT_DATA = json.load(f)

EMAIL_RE = re.compile(CONFIG["validacion"]["lead"]["email_regex"])

# ------------------------------------------------------------------------------
# Utilitarios
# ------------------------------------------------------------------------------
def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def round_coords(lat, lng):
    return f"{round(float(lat), 4)},{round(float(lng), 4)}"

def provincia_to_tarifa_key(provincia_id: str):
    prov = next((p for p in CONFIG["provincias"] if p["id"] == provincia_id), None)
    if not prov:
        return None, None
    return prov["distribuidora"], prov

def get_base_charge(tarifa: dict) -> float:
    """
    Regla de negocio: usar SOLO uno como base. Preferimos 'fijo' si existe; si no, 'comercializacion'.
    """
    fijo = float(tarifa.get("fijo", 0) or 0)
    comercializacion = float(tarifa.get("comercializacion", 0) or 0)
    if fijo > 0:
        return fijo
    return comercializacion

def calcular_tarifa_valor(kwh: float, tarifa: dict) -> float:
    """
    Calcula el total facturado para un consumo en kWh con tramos y base única (sin duplicación).
    Regla histórica: al aplicar base, se descuentan 10 kWh del restante.
    """
    restante = float(kwh)
    total = 0.0
    base = get_base_charge(tarifa)
    if restante > 0 and base > 0:
        total += base
        restante -= 10
    if restante > 0:
        b = min(restante, 290)
        total += b * float(tarifa["tier1"])
        restante -= b
    if restante > 0:
        b = min(restante, 450)
        total += b * float(tarifa["tier2"])
        restante -= b
    if restante > 0:
        total += restante * float(tarifa["tier3"])
    return round(max(total, 0.0), 2)

def calcular_tarifa_detalle(kwh: float, tarifa: dict) -> dict:
    """
    Devuelve desglose por tramos para un kWh dado. Muestra 'fijo' como base y deja
    'comercializacion' en 0 para evitar líneas duplicadas en UI.
    """
    restante = float(kwh)
    detalle = {
        "fijo": 0.0,                 # mostramos SOLO fijo como base
        "comercializacion": 0.0,     # 0 para no duplicar
        "tramos": [],
        "total": 0.0,
    }
    total = 0.0
    base = get_base_charge(tarifa)
    if restante > 0 and base > 0:
        detalle["fijo"] = float(base)
        total += base
        restante -= 10

    if restante > 0:
        tramo1 = min(restante, 290.0)
        subtotal1 = tramo1 * float(tarifa["tier1"])
        detalle["tramos"].append({
            "label": "11-300",
            "kwh": round(tramo1, 2),
            "precio_unit": float(tarifa["tier1"]),
            "subtotal": round(subtotal1, 2),
        })
        total += subtotal1
        restante -= tramo1

    if restante > 0:
        tramo2 = min(restante, 450.0)
        subtotal2 = tramo2 * float(tarifa["tier2"])
        detalle["tramos"].append({
            "label": "301-750",
            "kwh": round(tramo2, 2),
            "precio_unit": float(tarifa["tier2"]),
            "subtotal": round(subtotal2, 2),
        })
        total += subtotal2
        restante -= tramo2

    if restante > 0:
        subtotal3 = restante * float(tarifa["tier3"])
        detalle["tramos"].append({
            "label": "750+",
            "kwh": round(restante, 2),
            "precio_unit": float(tarifa["tier3"]),
            "subtotal": round(subtotal3, 2),
        })
        total += subtotal3

    detalle["total"] = round(total, 2)
    return detalle

# ------------------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------------------
@app.get("/config")
def get_config():
    safe_keys = ["ui", "provincias", "tarifas", "sistemas", "precios_sistema_por_paneles", "formatos", "finance"]
    safe = {k: CONFIG[k] for k in safe_keys if k in CONFIG}
    return jsonify(safe)

@app.post("/lead")
def create_lead():
    data = request.get_json(force=True)
    nombre = (data.get("nombre") or "").strip()
    email = (data.get("email") or "").strip()
    utm = data.get("utm") or {}

    if not nombre or len(nombre) < CONFIG["validacion"]["lead"]["nombre_min_len"]:
        return jsonify({"error": "Nombre inválido"}), 400
    if not EMAIL_RE.match(email or ""):
        return jsonify({"error": "Email inválido"}), 400

    lead_id = f"ld_{int(time.time())}"

    payload = {
        "lead_id": lead_id,
        "nombre": nombre,
        "email": email,
        "timestamp": now_iso(),
        "utm": {
            "source": utm.get("source", ""),
            "medium": utm.get("medium", ""),
            "campaign": utm.get("campaign", ""),
        },
    }
    send_webhook("lead_created", payload)
    return jsonify({"status": "ok", "lead_id": lead_id}), 201

@app.post("/calculate/consumption")
def calculate_consumption():
    data = request.get_json(force=True)
    provincia_id = data.get("provincia_id")
    consumos = data.get("consumos_kwh")

    if not isinstance(consumos, list) or len(consumos) != 12:
        return jsonify({"error": "Debes enviar 12 valores (enero..diciembre)"}), 400
    if all((v or 0) <= 0 for v in consumos):
        return jsonify({"error": "Ingresa al menos un mes > 0"}), 400

    tarifa_key, provincia = provincia_to_tarifa_key(provincia_id)
    if not tarifa_key or tarifa_key not in CONFIG["tarifas"]:
        return jsonify({"error": "Provincia/tarifa inválida"}), 400

    tarifa = CONFIG["tarifas"][tarifa_key]
    meses = [float(v or 0) for v in consumos]
    meses_cargados = sum(1 for v in meses if v > 0)
    total = sum(meses)
    mensual_promedio = total / meses_cargados
    diario_promedio = mensual_promedio / 30.0

    costo_actual_mensual = calcular_tarifa_valor(mensual_promedio, tarifa)
    detalle_actual = calcular_tarifa_detalle(mensual_promedio, tarifa)

    return jsonify({
        "consumo": {
            "mensual_promedio_kwh": round(mensual_promedio, 2),
            "diario_promedio_kwh": round(diario_promedio, 2),
            "meses_cargados": meses_cargados,
        },
        "finanzas": {
            "tarifa": tarifa_key,
            "costo_actual_mensual": round(costo_actual_mensual, 2),
        },
        "tarifa_desglose": detalle_actual,
        "provincia": {
            "id": provincia["id"],
            "nombre": provincia["nombre"],
            "markup_pct": provincia["markup_pct"],  # operativo; no UI
        },
        "ui": {
            "title": CONFIG["ui"]["rename_labels"]["consumption_monthly_estimate"],
        },
    })

@app.get("/pvout")
def get_pvout():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros inválidos"}), 400

    key = round_coords(lat, lng)
    if key in PVOUT_DATA:
        return jsonify({"pvout": PVOUT_DATA[key]})

    # Búsqueda cercana si no hay match exacto
    closest_val = None
    min_dist = float("inf")
    for k, v in PVOUT_DATA.items():
        plat, plng = map(float, k.split(","))
        dist = abs(plat - lat) + abs(plng - lng)
        if dist < min_dist:
            min_dist = dist
            closest_val = v

    if closest_val is not None:
        return jsonify({"pvout": closest_val})
    return jsonify({"error": "No se encontró PVOUT para esas coordenadas"}), 404

@app.post("/calculate/solar")
def calculate_solar():
    data = request.get_json(force=True)

    lead = data.get("lead") or {}
    provincia_id = data.get("provincia_id")
    pvout = float(data.get("pvout") or 0)
    consumo = data.get("consumo") or {}
    mensual_promedio = float(consumo.get("mensual_promedio_kwh") or 0)
    diario_promedio = float(consumo.get("diario_promedio_kwh") or 0)
    lat = float(data.get("lat") or 0)
    lng = float(data.get("lng") or 0)

    if not provincia_id or pvout <= 0 or mensual_promedio <= 0 or diario_promedio <= 0:
        return jsonify({"error": "Datos insuficientes para cálculo solar"}), 400

    tarifa_key, provincia = provincia_to_tarifa_key(provincia_id)
    if not tarifa_key or tarifa_key not in CONFIG["tarifas"]:
        return jsonify({"error": "Provincia/tarifa inválida"}), 400
    tarifa = CONFIG["tarifas"][tarifa_key]

    inv = next((i for i in CONFIG["sistemas"]["inversores"] if i.get("recomendado")), CONFIG["sistemas"]["inversores"][0])
    panel = next((p for p in CONFIG["sistemas"]["paneles"] if p.get("default")), CONFIG["sistemas"]["paneles"][0])
    reglas = CONFIG["sistemas"]["reglas_dimensionamiento"]

    eficiencia = float(inv["eficiencia"])
    potencia_kw = float(panel["potencia_kw"])
    mult = int(reglas["paneles_multiplo"])
    por_inv = int(reglas["paneles_por_inversor"])
    max_precio_paneles = int(reglas["max_paneles_precio"])

    # Dimensionamiento
    paneles_necesarios = diario_promedio / (pvout * potencia_kw * eficiencia)
    paneles = int(math.ceil(paneles_necesarios / mult) * mult)
    inversores = int(math.ceil(paneles / por_inv))
    capacidad_expansion = inversores * por_inv - paneles

    # Producción
    kwh_diario = paneles * pvout * potencia_kw * eficiencia
    kwh_mensual = kwh_diario * (365.0 / 12.0)
    kwh_anual = kwh_diario * 365.0
    cobertura_pct = (kwh_mensual / mensual_promedio) * 100.0

    # Precios
    precios = CONFIG["precios_sistema_por_paneles"]
    precio_base = float(precios.get(str(paneles))) if str(paneles) in precios else 0.0
    cotizacion_personalizada = (precio_base == 0.0 or paneles > max_precio_paneles)
    markup_pct = float(provincia["markup_pct"])  # operativo; no UI
    precio_final = precio_base * (1 + markup_pct / 100.0) if precio_base > 0 else 0.0

    # Finanzas
    costo_actual_mensual = calcular_tarifa_valor(mensual_promedio, tarifa)

    ahorro_method = (CONFIG.get("finance", {}).get("ahorro_metodo") or "remanente").lower()
    if ahorro_method == "remanente":
        remanente = max(mensual_promedio - kwh_mensual, 0.0)
        costo_con_solar = calcular_tarifa_valor(remanente, tarifa)
        ahorro_mensual = costo_actual_mensual - costo_con_solar
    else:
        ahorro_mensual = calcular_tarifa_valor(kwh_mensual, tarifa)
        costo_con_solar = max(costo_actual_mensual - ahorro_mensual, 0.0)

    # Payback en años (precio_final / costo_actual_mensual)
    dec = CONFIG.get("finance", {}).get("payback_decimals", 1)
    payback_years = (precio_final / costo_actual_mensual) / 12.0 if (precio_final > 0 and costo_actual_mensual > 0) else 0.0
    ahorro_30_anos = (max(ahorro_mensual, 0.0) * 12.0 * 30.0) - precio_final
    reduccion_pct = min(max((max(ahorro_mensual, 0.0) / costo_actual_mensual) * 100.0 if costo_actual_mensual > 0 else 0, 0.0), 100.0)

    # Desgloses (para UI y webhook):
    desglose_actual = calcular_tarifa_detalle(mensual_promedio, tarifa)
    desglose_produccion = calcular_tarifa_detalle(kwh_mensual, tarifa)

    resp = {
        "dimensionamiento": {
            "paneles": paneles,
            "inversores": inversores,
            "capacidad_expansion_paneles": capacidad_expansion,
        },
        "produccion": {
            "diaria_kwh": round(kwh_diario, 1),
            "mensual_kwh": round(kwh_mensual),
            "anual_kwh": round(kwh_anual),
            "cobertura_pct": round(cobertura_pct, 1),
        },
        "finanzas": {
            "tarifa": tarifa_key,
            "costo_actual_mensual": round(costo_actual_mensual, 2),
            "ahorro_mensual_estimado": round(max(ahorro_mensual, 0.0), 2),
            "nuevo_costo_mensual": round(max(costo_con_solar, 0.0), 2),
            "precio_base_sistema": round(precio_base, 2),
            "markup_pct": markup_pct,
            "precio_final_sistema": round(precio_final, 2),
            "payback_years": round(payback_years, dec),
            "ahorro_30_anos": round(ahorro_30_anos, 2),
            "reduccion_factura_pct": round(reduccion_pct, 0),
        },
        "ui": {
            "labels": {
                "breakdown": CONFIG["ui"]["rename_labels"]["solar_production_breakdown"],
                "monthly_prod": CONFIG["ui"]["rename_labels"]["solar_monthly_production"],
                "payback": "Tiempo de recuperación",
            },
            "cotizacion_personalizada": cotizacion_personalizada,
        },
        "tarifa_desglose_actual": desglose_actual,
        "tarifa_desglose_produccion": desglose_produccion,
    }

    # ---------- Webhook con snake_case + coordenadas ----------
    webhook_payload = {
        "lead_id": lead.get("lead_id", ""),
        "nombre": lead.get("nombre", ""),
        "email": lead.get("email", ""),
        "utm_source": lead.get("utm", {}).get("source", ""),
        "utm_medium": lead.get("utm", {}).get("medium", ""),
        "utm_campaign": lead.get("utm", {}).get("campaign", ""),
        "provincia": provincia["nombre"],
        "kwh_mensuales": round(mensual_promedio, 2),
        "kwh_diarios": round(diario_promedio, 2),
        "costo_mensual": round(costo_actual_mensual, 2),
        "costo_anual": round(costo_actual_mensual * 12.0, 2),
        "desglose_costo_actual": desglose_actual,
        "pvout": pvout,
        "paneles_solares": paneles,
        "microinversores": inversores,
        "produccion_mensual_kwh": round(kwh_mensual),
        "desglose_produccion": desglose_produccion,
        "precio_sistema": round(precio_final, 2),
        "tiempo_recuperacion_anios": round(payback_years, dec),
        "ahorro_30_anios": round(ahorro_30_anos, 2),
        "coords_lat": lat,
        "coords_lng": lng,
    }
    send_webhook("solar_calculated", webhook_payload)

    return jsonify(resp)

# ------------------------------------------------------------------------------
# Health (opcional)
# ------------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")

# ------------------------------------------------------------------------------
# Run
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
