from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# Cargar PVOUT procesado
with open("pvout_data.json") as f:
    pvout_data = json.load(f)

def round_coords(lat, lng):
    return f"{round(float(lat),4)},{round(float(lng),4)}"

@app.route("/pvout")
def get_pvout():
    lat = float(request.args.get("lat"))
    lng = float(request.args.get("lng"))

    key = f"{round(lat,4)},{round(lng,4)}"
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

    return jsonify({"pvout": closest})
