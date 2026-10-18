"""Run the API and MQTT subscriber together on the always-on pilot host."""

from time import sleep

from waitress import serve

from operations_app import app, init_storage, start_mqtt


if __name__ == "__main__":
    for attempt in range(30):
        try:
            init_storage()
            break
        except Exception as exc:
            if attempt == 29:
                raise
            print(f"Waiting for databases: {exc}", flush=True)
            sleep(2)
    start_mqtt()
    serve(app, host="0.0.0.0", port=8765, threads=8)
