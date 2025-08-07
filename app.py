from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# Cargar datos desde JSON
with open("pvout_data.json") as f:
    pvout_data = json.load(f)

# Formateo para clave: redondea sin ceros innecesarios
def clean(val):
    return str(round(val, 4)).rstrip("0").rstrip(".")

@app.route("/pvout")
def get_pvout():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except:
        return jsonify({"error": "Missing or invalid lat/lng"}), 400

    key = f"{clean(lat)},{clean(lng)}"

    if key in pvout_data:
        return jsonify({"pvout": pvout_data[key]})

    # Buscar el punto más cercano
    closest_key = None
    min_dist = float("inf")
    for k in pvout_data:
        plat, plng = map(float, k.split(","))
        dist = abs(plat - lat) + abs(plng - lng)
        if dist < min_dist:
            min_dist = dist
            closest_key = k

    if closest_key:
        return jsonify({"pvout": pvout_data[closest_key]})
    else:
        return jsonify({"error": "No PVOUT found"}), 404
