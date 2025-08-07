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
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    if not lat or not lng:
        return jsonify({"error": "Missing lat/lng"}), 400
    key = round_coords(lat, lng)
    pvout = pvout_data.get(key)
    return jsonify({"pvout": pvout}) if pvout else ("Not found", 404)
