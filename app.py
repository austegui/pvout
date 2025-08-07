from flask import Flask, request, jsonify
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)  # Habilita CORS para permitir peticiones desde otros dominios

# Cargar PVOUT procesado desde JSON
with open("pvout_data.json") as f:
    pvout_data = json.load(f)

def round_coords(lat, lng):
    return f"{round(float(lat), 4)},{round(float(lng), 4)}"

@app.route("/pvout")
def get_pvout():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros inválidos"}), 400

    key = round_coords(lat, lng)

    if key in pvout_data:
        return jsonify({"pvout": pvout_data[key]})

    # Búsqueda cercana si no hay coincidencia exacta
    closest = None
    min_dist = float("inf")
    for k, v in pvout_data.items():
        plat, plng = map(float, k.split(","))
        dist = abs(plat - lat) + abs(plng - lng)
        if dist < min_dist:
            min_dist = dist
            closest = v

    if closest is not None:
        return jsonify({"pvout": closest})
    else:
        return jsonify({"error": "No se encontró PVOUT para esas coordenadas"}), 404

if __name__ == "__main__":
    app.run(debug=True)
