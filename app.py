from flask import Flask, request, jsonify
from flask_cors import CORS
import json, math, re, time, os
import requests

app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": ["*", "null"]}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    supports_credentials=False,
    max_age=600
)

# --- Carga de configuración y datos ---
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open("pvout_data.json", "r", encoding="utf-8") as f:
    PVOUT_DATA = json.load(f)

EMAIL_RE = re.compile(CONFIG["validacion"]["lead"]["email_regex"])

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def round_coords(lat, lng):
    return f"{round(float(lat), 4)},{round(float(lng), 4)}"

def provincia_to_tarifa_key(provincia_id):
    prov = next((p for p in CONFIG["provincias"] if p["id"] == provincia_id), None)
    if not prov:
        return None, None
    return prov["distribuidora"], prov

def calcular_tarifa_valor(kwh, tarifa):
    total = tarifa["comercializacion"]
    restante = float(kwh)
    if restante > 0:
        total += tarifa["fijo"]; restante -= 10
    if restante > 0:
        b = min(restante, 290); total += b * tarifa["tier1"]; restante -= b
    if restante > 0:
        b = min(restante, 450); total += b * tarifa["tier2"]; restante -= b
    if restante > 0:
        total += restante * tarifa["tier3"]
    return max(total, 0.0)

def send_webhook(event, payload):
    try:
        wh = CONFIG["services"]["webhook"]
        if event not in wh.get("send_on", []):
            return
        requests.post(
            wh["url"],
            json={"event": event, "payload": payload},
            timeout=wh.get("timeout_ms", 8000) / 1000.0
        )
    except Exception:
        # No romper flujo si el webhook falla
        pass

# ----------- Endpoints -----------
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
        "nombre": nombre,
        "email": email,
        "timestamp": now_iso(),
        "utm": {
            "source": utm.get("source", ""),
            "medium": utm.get("medium", ""),
            "campaign": utm.get("campaign", "")
        },
        "lead_id": lead_id
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

    return jsonify({
        "consumo": {
            "mensual_promedio_kwh": round(mensual_promedio, 2),
            "diario_promedio_kwh": round(diario_promedio, 2),
            "meses_cargados": meses_cargados
        },
        "finanzas": {
            "tarifa": tarifa_key,
            "costo_actual_mensual": round(costo_actual_mensual, 2)
        },
        "provincia": {
            "id": provincia["id"],
            "nombre": provincia["nombre"],
            "markup_pct": provincia["markup_pct"]
        },
        "ui": {
            "title": CONFIG["ui"]["rename_labels"]["consumption_monthly_estimate"]
        }
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

    markup_pct = float(provincia["markup_pct"])
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

    # Payback SOLO en años (precio_final / costo_actual_mensual)  **CORRECCIÓN**
    payback_years = 0.0
    if costo_actual_mensual > 0 and precio_final > 0:
        payback_years = (precio_final / costo_actual_mensual) / 12.0

    ahorro_30_anos = (max(ahorro_mensual, 0.0) * 12.0 * 30.0) - precio_final
    reduccion_pct = min(max((max(ahorro_mensual, 0.0) / costo_actual_mensual) * 100.0 if costo_actual_mensual > 0 else 0, 0.0), 100.0)

    dec = CONFIG.get("finance", {}).get("payback_decimals", 1)
    resp = {
        "dimensionamiento": {
            "paneles": paneles,
            "inversores": inversores,
            "capacidad_expansion_paneles": capacidad_expansion
        },
        "produccion": {
            "diaria_kwh": round(kwh_diario, 1),
            "mensual_kwh": round(kwh_mensual),
            "anual_kwh": round(kwh_anual),
            "cobertura_pct": round(cobertura_pct, 1)
        },
        "finanzas": {
            "tarifa": tarifa_key,
            "costo_actual_mensual": round(costo_actual_mensual, 2),
            "ahorro_mensual_estimado": round(max(ahorro_mensual, 0.0), 2),
            "nuevo_costo_mensual": round(max(costo_actual_mensual - ahorro_mensual, 0.0), 2),
            "precio_base_sistema": round(precio_base, 2),
            "markup_pct": markup_pct,
            "precio_final_sistema": round(precio_final, 2),
            "payback_years": round(payback_years, dec),
            "ahorro_30_anos": round(ahorro_30_anos, 2),
            "reduccion_factura_pct": round(reduccion_pct, 0)
        },
        "ui": {
            "labels": {
                "breakdown": CONFIG["ui"]["rename_labels"]["solar_production_breakdown"],
                "monthly_prod": CONFIG["ui"]["rename_labels"]["solar_monthly_production"],
                "payback": "Tiempo de recuperación"
            },
            "display": {
                "payback_text": f"Tiempo de recuperación: {round(payback_years, dec)} años"
            },
            "cotizacion_personalizada": cotizacion_personalizada
        }
    }

    payload = {
        "lead": {
            "lead_id": lead.get("lead_id", ""),
            "nombre": lead.get("nombre", ""),
            "email": lead.get("email", "")
        },
        "ubicacion": {
            "lat": float(data.get("lat") or 0),
            "lng": float(data.get("lng") or 0),
            "provincia_id": provincia["id"],
            "provincia_nombre": provincia["nombre"]
        },
        "pvout": pvout,
        "consumo": {
            "mensual_promedio_kwh": mensual_promedio,
            "diario_promedio_kwh": diario_promedio,
            "meses_cargados": consumo.get("meses_cargados", 0)
        },
        "dimensionamiento": resp["dimensionamiento"],
        "produccion": resp["produccion"],
        "finanzas": resp["finanzas"]
    }
    send_webhook("solar_calculated", payload)

    return jsonify(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
