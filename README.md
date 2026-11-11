# Reading EcoFlow STREAM Microinverter data

This guide describes the app-account connection tested with a U.S. EcoFlow STREAM Microinverter (N011 series). It uses the owner's EcoFlow account to read live MQTT telemetry and cloud history. These are observed app interfaces. Compatibility with other models, regions, and firmware versions is unverified.

| Data | Source | Units |
| --- | --- | --- |
| Inverter AC output, voltage, current, frequency | MQTT | W, V, A, Hz |
| PV1, PV2 + PV3, PV4 input | MQTT | W, V, A |
| Daily generation and hourly generation | Cloud history | Wh, converted to kWh |
| Five-minute solar power samples | Cloud chart | W, with a publication delay |

## Run the Python example

Download [read_stream.py](read_stream.py) and run the commands below from the directory where you saved it. Use Python 3.9 or later. You’ll need the serial number of a U.S. STREAM N011 bound to your EcoFlow account.

For a live snapshot, install Paho and run the collector.

```bash
python3 -m pip install 'paho-mqtt>=2,<3'
python3 read_stream.py live --serial YOUR_N011_SERIAL
```

For a day of historical readings, use the device's local date. This mode uses Python’s standard library.

```bash
python3 read_stream.py history --serial YOUR_N011_SERIAL --date 2026-10-23
```

The script prompts for your EcoFlow email and password. For unattended use, set `ECOFLOW_EMAIL` and `ECOFLOW_PASSWORD` in the environment. Both modes print JSON and represent missing readings as `null`.

Live mode returns AC readings and grouped PV inputs after 70 seconds by default. Set `--seconds` to change the collection window. History mode returns `dailyKwh`, `hourlyEnergy`, and `delayedPower` with the original device-local time labels. The account flow below is handled by the script.

## Authenticate with your own account

1. Bind your STREAM to your EcoFlow account in the mobile app. Obtain the serial from your own device.
2. Send `POST /auth/login` to `https://api-a.ecoflow.com` with JSON fields `email`, `password` (base64 of the UTF-8 password), `scene: "IOT_APP"`, and `userType: "ECOFLOW"`.
3. Require application response code `0`. Read `data.token` and `data.user.userId`. Use `Authorization: Bearer <token>` for subsequent HTTP requests.
4. Send `GET /iot-service/user/device` and confirm the serial belongs to your account. On the tested account, devices appear under `data.bound`, keyed by serial.

Keep the password, token, and broker credentials in a protected local configuration or secret store. Base64 is a transport encoding. Use HTTPS with certificate verification and redact identifiers and credentials from logs and example captures.

## Subscribe to live telemetry

Send `GET /iot-auth/app/certification` with the bearer token. Use the returned `data.url`, `data.port`, `data.certificateAccount`, and `data.certificatePassword` to connect with MQTT 3.1.1 over TLS. The tested response used `mqtt-a.ecoflow.com:8883`. Validate the returned host and keep TLS verification enabled.

Use a stable client ID unique to your collector. The tested format was `ANDROID_<collector-specific-32-hex-id>_<userId>`. Subscribe at QoS 0 to both topics and wait for the subscription acknowledgments.

```text
/app/device/property/<serial>
/app/<userId>/<serial>/thing/property/get_reply
```

Passive updates can pause even while the broker connection is healthy. The tested collector sends a display-data GET at startup after subscription and every 60 seconds thereafter. Back off on authentication and subscription failures. Keep the connection open to receive pushes as they arrive.

Publish the request at QoS 0 with retain disabled to `/app/<userId>/<serial>/thing/property/get`. Serialize a `StreamACSendHeaderMsg` whose `msg` has `src=32`, `dest=32`, `cmd_func=254`, `cmd_id=21`, `data_len=0`, a positive `seq`, your `device_sn`, and a client label in `from`. Leave `pdata` absent. This request reads display data. The guide covers read-only collection.

## Decode the pushes

1. Parse the property message as a `StreamACSendHeaderMsg` protobuf envelope.
2. Select display uploads with `cmd_func=254`, `cmd_id=21`, `src=2`, `dest=32`, and `enc_type=1`.
3. XOR each byte of `pdata` with `seq & 0xff`, then parse the result as `BkSeriesDisplayPropertyUpload`.
4. Merge only fields actually present in the upload. Update each field's own timestamp and expire it after 120 seconds without a fresh sample. Ignore retained messages for live readings.

| Reading | Protobuf field numbers |
| --- | --- |
| AC watts, volts, amps, Hz | 616, 613, 614, 615 |
| PV1 watts, volts, amps | 361, 380, 381 |
| PV2 watts, volts, amps | 70, 442, 71 |
| PV3 watts, volts, amps | 996, 998, 999 |
| PV4 watts, volts, amps | 997, 1000, 1001 |
| Wi-Fi RSSI in dBm | 602 |

The listed fields are protobuf floats. PV2 and PV3 share an MPPT. Sum their reported watts and currents when both readings are fresh, and display a representative reported voltage when the readings are consistent. Sum all four fresh power fields for total DC input. AC output is a separate reading.

The GET reply can contain multiple protobuf headers and may omit an explicit success code. Count replies separately. Use fresh display uploads to establish measurement freshness. Power factor, inverter temperature, and output limit were not validated in this app-account decoder.

## Query stored generation

Send an authenticated `POST /app/commonQuery/query` request. Use the device's local calendar date for both range endpoints.

```json
{
  "sn": "<YOUR_SERIAL>",
  "params": {
    "sn": "<YOUR_SERIAL>",
    "queryType": 0,
    "beginTime": "2026-10-23 00:00:00",
    "endTime": "2026-10-23 23:59:59"
  },
  "code": "BK620_US-App-HOME-SOLAR-ENERGY-FLOW-solor-line-NOTDISTINGUISH-MASTER_DATA"
}
```

- **Daily energy.** Read the `master_data` row's `indexValue` and `unit`. Divide Wh by 1,000 to get kWh. Keep kWh values as returned.
- **Hourly energy.** Replace the final `MASTER_DATA` with `CHART_DATA`. Read `chart_data` rows and their local `time` values. The tested installation matched hour-start labels. Verify that convention for your device and reconcile the chart with the daily total. Skip ambiguous or nonexistent clock-change labels while retaining other hours and the daily total.
- **Five-minute power.** Use `BK620_US-App-SOLAR-SOLAR-POWER-FLOW-chart_solar_line_p1_p23_p4-DAY-CHART_DATA`. Rows with `extra: "1"` represent total watts. The other series are components. Keep the sample timestamp and label this data as delayed.

Keep missing readings as unavailable. Prefer the cloud daily total when it exists. If you integrate locally recorded DC power for days without cloud data, label the result as an estimate and exclude recording gaps. Keep that estimate separate from the cloud total for the same day.

## Verify your setup

With the inverter generating and the phone app closed, confirm the GET count advances once per minute and fresh property messages continue to arrive. Compare the AC output and PV readings with the app, then compare a completed day's cloud energy. Test after sunset and again the following morning. The inverter can stop sending live data when panel power disappears, while saved cloud history remains readable.

## Protocol references

- [STREAM envelope and observed display uploads](https://github.com/tolwi/hassio-ecoflow-cloud/blob/main/custom_components/ecoflow_cloud/devices/internal/stream_microinverter.py)
- [BK-series protobuf schema](https://github.com/rabits/ha-ef-ble/blob/main/proto/bk_series.proto)
