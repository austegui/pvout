from flask import Flask, request, jsonify
import json

app = Flask(__name__)

with open("pvout_data.json") as f:
    pvout_data = json.load(f)

@app.route("/pvout")
def get_pvout():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except:
        return jsonify({"error": "Invalid or missing lat/lng"}), 400

    closest_key = None
    min_distance = float("inf")

    for k in pvout_data.keys():
        plat, plng = map(float, k.split(","))
        dist = abs(plat - lat) + abs(plng - lng)
        if dist < min_distance:
            min_distance = dist
            closest_key = k

    if closest_key:
        return jsonify({"pvout": pvout_data[closest_key]})
    else:
        return jsonify({"error": "No PVOUT found"}), 404
