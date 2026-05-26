import numpy as np, joblib, time, json, os, ssl
import paho.mqtt.client as mqtt

import os

BROKER = os.getenv("HIVEMQ_BROKER", "e172211a559b42289548a3dbeb0158ee.s1.eu.hivemq.cloud")
PORT = int(os.getenv("HIVEMQ_PORT", "8883"))
USERNAME = os.getenv("HIVEMQ_USER", "uns-edge-node")
PASSWORD = os.getenv("HIVEMQ_PASS")

SCORE_MEAN = -0.4526
SCORE_MIN = -0.4982
MARGIN = 0.1

MODEL_PATH = os.getenv("MODEL_PATH", "/home/eric/uns/isolation_forest.pkl")
WINDOW_FILE = os.getenv("WINDOW_FILE", "/tmp/uns_fft_window.npy")

if not PASSWORD:
    raise RuntimeError("Set HIVEMQ_PASS env var")
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "anomaly-detector")
client.username_pw_set(USERNAME, PASSWORD)
client.tls_set(tls_version=ssl.PROTOCOL_TLS)
client.connect(BROKER, PORT)
client.loop_start()

last_mtime = 0
while True:
    try:
        if os.path.exists(WINDOW_FILE):
            mtime = os.path.getmtime(WINDOW_FILE)
            if mtime > last_mtime:
                last_mtime = mtime
                window = np.load(WINDOW_FILE)
                fft_mag = np.abs(np.fft.rfft(window))[:len(window) // 2]
                raw = model.score_samples([fft_mag])[0]
                anomaly = max(0.0, min(1.0, (SCORE_MEAN - raw) / (SCORE_MEAN - SCORE_MIN + MARGIN)))
                payload = json.dumps({"AnomalyScore": round(float(anomaly), 4)})
                client.publish("uns/json/Motor1", payload)
                if anomaly > 0.85:
                    client.publish("uns/anomaly", payload)
                tag = "ANOMALY" if anomaly > 0.85 else "Normal"
                print(f"{tag}: {anomaly:.4f}  (raw={raw:.4f})")
    except Exception as e:
        print(f"err: {e}")
    time.sleep(0.2)
