import time
import ssl
import paho.mqtt.client as mqtt
import os

HIVEMQ_BROKER = os.getenv("HIVEMQ_BROKER", "e172211a559b42289548a3dbeb0158ee.s1.eu.hivemq.cloud")
HIVEMQ_PORT = int(os.getenv("HIVEMQ_PORT", "8883"))
HIVEMQ_USER = os.getenv("HIVEMQ_USER", "uns-edge-node")
HIVEMQ_PASS = os.getenv("HIVEMQ_PASS")

AWS_BROKER = os.getenv("AWS_IOT_ENDPOINT", "a3028lngo7foab-ats.iot.us-east-1.amazonaws.com")
AWS_PORT = 8883
AWS_CA = os.getenv("AWS_CA", "C:/Users/Eric/uns-project/certs/AmazonRootCA1.pem")
AWS_CERT = os.getenv("AWS_CERT", "C:/Users/Eric/uns-project/certs/aws-iot.crt")
AWS_KEY = os.getenv("AWS_KEY", "C:/Users/Eric/uns-project/certs/aws-iot-private.key")

ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))

if not HIVEMQ_PASS:
    raise RuntimeError("Set HIVEMQ_PASS env var")

last_forward_time = 0


aws = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "uns-bridge-aws")
aws.tls_set(
    ca_certs=AWS_CA,
    certfile=AWS_CERT,
    keyfile=AWS_KEY,
    tls_version=ssl.PROTOCOL_TLS,
)
aws.connect(AWS_BROKER, AWS_PORT)
aws.loop_start()
print(f"connected to AWS IoT: {AWS_BROKER}")


def on_message(c, u, msg):
    global last_forward_time

    now = time.time()
    payload_text = msg.payload.decode(errors="replace")

    if now - last_forward_time < ALERT_COOLDOWN_SECONDS:
        remaining = int(ALERT_COOLDOWN_SECONDS - (now - last_forward_time))
        print(f"skipped AWS alert due to cooldown ({remaining}s left): {payload_text}")
        return

    last_forward_time = now
    aws.publish("uns/anomaly", msg.payload)
    print(f"forwarded to AWS: {payload_text}")


hive = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "uns-bridge-hive")
hive.username_pw_set(HIVEMQ_USER, HIVEMQ_PASS)
hive.tls_set(tls_version=ssl.PROTOCOL_TLS)
hive.on_message = on_message
hive.connect(HIVEMQ_BROKER, HIVEMQ_PORT)
hive.subscribe("uns/anomaly")
print("subscribed to HiveMQ: uns/anomaly")
print(f"AWS alert cooldown: {ALERT_COOLDOWN_SECONDS} seconds")
hive.loop_forever()