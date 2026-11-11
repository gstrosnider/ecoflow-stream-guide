"""Read live and historical data from an owner-bound EcoFlow STREAM N011."""
import argparse
import base64
import getpass
import json
import math
import os
import re
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone

HOST = 'https://api-a.ecoflow.com'
HISTORY_PREFIX = 'BK620_US-App-HOME-SOLAR-ENERGY-FLOW-solor-line-NOTDISTINGUISH-'
POWER_CODE = 'BK620_US-App-SOLAR-SOLAR-POWER-FLOW-chart_solar_line_p1_p23_p4-DAY-CHART_DATA'
FLOATS = {361: 'powGetPv', 70: 'powGetPv2', 996: 'powGetPv3', 997: 'powGetPv4',
          380: 'plugInInfoPvVol', 381: 'plugInInfoPvAmp',
          442: 'plugInInfoPv2Vol', 71: 'plugInInfoPv2Amp',
          998: 'plugInInfoPv3Vol', 999: 'plugInInfoPv3Amp',
          1000: 'plugInInfoPv4Vol', 1001: 'plugInInfoPv4Amp',
          616: 'gridConnectionPower', 613: 'gridConnectionVol',
          614: 'gridConnectionAmp', 615: 'gridConnectionFreq', 602: 'moduleWifiRssi'}


def fields(raw):
    """Bounded protobuf wire reader. No arbitrary protocol or schema fallback."""
    if len(raw) > 1_000_000:
        raise ValueError('Oversized frame')
    pos, out = 0, []
    def vi():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(raw):
                raise ValueError('Truncated frame')
            b = raw[pos]; pos += 1
            value |= (b & 127) << shift
            if not b & 128:
                if value > 0xffffffffffffffff:
                    raise ValueError('Invalid varint')
                return value
        raise ValueError('Invalid varint')
    while pos < len(raw):
        if len(out) >= 10_000:
            raise ValueError('Too many fields')
        tag = vi(); number, wire = tag >> 3, tag & 7
        if not 0 < number <= 0x1fffffff:
            raise ValueError('Invalid tag')
        if wire == 0:
            value = vi()
        elif wire in (1, 2, 5):
            size = 8 if wire == 1 else 4 if wire == 5 else vi()
            if pos + size > len(raw):
                raise ValueError('Truncated frame')
            value = raw[pos:pos+size]; pos += size
        else:
            raise ValueError('Unsupported wire')
        out.append((number, wire, value))
    return out


def read_frame(serial, seq):
    def vi(n):
        out = bytearray()
        while n > 127:
            out.append((n & 127) | 128); n >>= 7
        out.append(n)
        return bytes(out)
    def scalar(n, value):
        return vi(n << 3) + vi(value)
    def string(n, value):
        b = value.encode()
        return vi((n << 3) | 2) + vi(len(b)) + b
    header = (scalar(2, 32) + scalar(3, 32) + scalar(8, 254) + scalar(9, 21) +
              scalar(10, 0) + scalar(14, seq) + string(23, 'StreamReader') + string(25, serial))
    return b'\x0a' + vi(len(header)) + header


def decode(raw):
    output = []
    for n, w, value in fields(raw):
        if n != 1 or w != 2:
            continue
        h = {n: (w, v) for n, w, v in fields(value)}
        scalar = lambda n: h[n][1] if n in h and h[n][0] == 0 else None
        # Only the observed device display upload is eligible for live readings.
        if (scalar(8), scalar(9), scalar(2), scalar(3), scalar(6)) != (254, 21, 2, 32, 1):
            continue
        if 1 not in h or h[1][0] != 2 or scalar(14) is None:
            continue
        plain = bytes(b ^ (scalar(14) & 255) for b in h[1][1])
        values = {}
        for number, wire, value in fields(plain):
            name = FLOATS.get(number)
            if name is None or wire != 5:
                continue
            f = struct.unpack('<f', value)[0]
            maximum = 0 if name == 'moduleWifiRssi' else 100 if name.endswith('Amp') or name in ('gridConnectionAmp', 'gridConnectionFreq') else 1000 if name.endswith('Vol') else 10000
            minimum = -150 if name == 'moduleWifiRssi' else 0
            if not math.isfinite(f) or not minimum <= f <= maximum:
                raise ValueError('Invalid measurement')
            values[name] = f
        if values:
            output.append(values)
    return output


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(path, token=None, body=None):
    headers = {'Content-Type': 'application/json', 'lang': 'en_US'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(
        HOST + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('EcoFlow response exceeds size limit')
    payload = json.loads(raw)
    if str(payload.get('code')) != '0':
        raise ValueError(f'EcoFlow rejected {path}')
    return payload['data']


def history(token, serial, day):
    def rows(code, kind, units):
        data = request('/app/commonQuery/query', token, {
            'sn': serial,
            'params': {'sn': serial, 'queryType': 0,
                       'beginTime': f'{day} 00:00:00', 'endTime': f'{day} 23:59:59'},
            'code': code,
        })
        return [row for row in data or []
                if row.get('indexName') == kind
                and str(row.get('unit', '')).lower() in units
                and type(row.get('indexValue')) in (int, float)
                and math.isfinite(row['indexValue']) and row['indexValue'] >= 0]

    def kwh(row):
        return row['indexValue'] / 1000 if row['unit'].lower() == 'wh' else row['indexValue']

    daily = rows(HISTORY_PREFIX + 'MASTER_DATA', 'master_data', {'wh', 'kwh'})
    hourly = rows(HISTORY_PREFIX + 'CHART_DATA', 'chart_data', {'wh', 'kwh'})
    power = rows(POWER_CODE, 'chart_data', {'w'})
    return {
        'date': day,
        'dailyKwh': kwh(daily[0]) if len(daily) == 1 else None,
        'hourlyEnergy': [{'localTime': row['time'], 'kwh': kwh(row)} for row in hourly],
        'delayedPower': [{'localTime': row['time'], 'watts': row['indexValue']}
                         for row in power if str(row.get('extra')) == '1'],
    }


def live(token, uid, serial, seconds, client_key):
    import paho.mqtt.client as mqtt

    cert = request('/iot-auth/app/certification', token)
    host, port = cert['url'], int(cert['port'])
    if not re.fullmatch(r'[a-zA-Z0-9.-]+\.ecoflow\.com', host) or port != 8883:
        raise ValueError('Unexpected EcoFlow MQTT broker')
    topic = f'/app/device/property/{serial}'
    get_topic = f'/app/{uid}/{serial}/thing/property/get'
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f'ANDROID_{client_key}_{uid}',
                         protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.username_pw_set(cert['certificateAccount'], cert['certificatePassword'])
    client.tls_set_context(ssl.create_default_context())
    client.connect_timeout = 10
    values = {}
    subscribed = False
    received_at = None

    def on_connect(conn, userdata, flags, reason, properties):
        if reason.is_failure:
            raise ValueError('MQTT connection rejected')
        result, _ = conn.subscribe([(topic, 0), (get_topic + '_reply', 0)])
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise ValueError('MQTT subscription failed')

    def on_subscribe(conn, userdata, mid, reasons, properties):
        nonlocal subscribed
        if len(reasons) != 2 or any(reason.is_failure for reason in reasons):
            raise ValueError('MQTT subscription rejected')
        subscribed = True

    def on_message(conn, userdata, message):
        nonlocal received_at
        if message.retain or message.topic != topic:
            return
        try:
            updates = decode(message.payload)
        except ValueError:
            return
        for update in updates:
            now = time.monotonic()
            values.update({name: (value, now) for name, value in update.items()})
            received_at = datetime.now(timezone.utc).isoformat()

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    deadline = time.monotonic() + seconds
    next_request = 0
    try:
        while time.monotonic() < deadline:
            if client.loop(timeout=1) != mqtt.MQTT_ERR_SUCCESS:
                raise ValueError('MQTT connection interrupted')
            now = time.monotonic()
            if subscribed and now >= next_request:
                seq = max(1, int(time.time() * 1000) & 0x7fffffff)
                result = client.publish(get_topic, read_frame(serial, seq), qos=0, retain=False)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise ValueError('MQTT display request failed')
                next_request = now + 60
    finally:
        client.disconnect()
    if not subscribed:
        raise ValueError('Timed out waiting for MQTT subscriptions')

    def fresh(name):
        value, received = values.get(name, (None, 0))
        return value if time.monotonic() - received <= 120 else None

    def total(*readings):
        return sum(readings) if all(value is not None for value in readings) else None

    ports = [{
        'watts': fresh('powGetPv' + suffix),
        'volts': fresh('plugInInfoPv' + suffix + 'Vol'),
        'amps': fresh('plugInInfoPv' + suffix + 'Amp'),
    } for suffix in ('', '2', '3', '4')]
    pv2, pv3 = ports[1:3]
    shared_voltage = None
    if pv2['volts'] is not None and pv3['volts'] is not None:
        if abs(pv2['volts'] - pv3['volts']) <= 1:
            shared_voltage = pv3['volts']
    return {
        'lastTelemetryAt': received_at,
        'acWatts': fresh('gridConnectionPower'),
        'acVolts': fresh('gridConnectionVol'),
        'acAmps': fresh('gridConnectionAmp'),
        'frequencyHz': fresh('gridConnectionFreq'),
        'wifiDbm': fresh('moduleWifiRssi'),
        'dcWatts': total(*(port['watts'] for port in ports)),
        'inputs': {'PV1': ports[0], 'PV2 + PV3': {
            'watts': total(pv2['watts'], pv3['watts']),
            'amps': total(pv2['amps'], pv3['amps']),
            'volts': shared_voltage,
        }, 'PV4': ports[3]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('live', 'history'))
    parser.add_argument('--serial', required=True, help='Your N011 device serial')
    parser.add_argument('--date', type=date.fromisoformat, help='Device-local date for history')
    parser.add_argument('--seconds', type=int, default=70, help='Live collection window (default 70)')
    parser.add_argument('--client-key', default=uuid.uuid4().hex.upper(),
                        help='Optional persistent 32-character hex ID unique to this collector')
    args = parser.parse_args()
    if not re.fullmatch(r'N011[A-Za-z0-9]{2,36}', args.serial):
        parser.error('Use the serial from a U.S. STREAM N011 device')
    if args.mode == 'history' and args.date is None:
        parser.error('History requires --date YYYY-MM-DD')
    if args.seconds < 5 or not re.fullmatch(r'[0-9a-fA-F]{32}', args.client_key):
        parser.error('Use at least 5 seconds and a 32-character hex client key')
    email = os.environ.get('ECOFLOW_EMAIL') or input('EcoFlow email: ').strip()
    password = os.environ.get('ECOFLOW_PASSWORD') or getpass.getpass('EcoFlow password: ')
    login = request('/auth/login', body={
        'email': email, 'password': base64.b64encode(password.encode()).decode(),
        'scene': 'IOT_APP', 'userType': 'ECOFLOW',
    })
    token, uid = login['token'], str(login['user']['userId'])
    if not re.fullmatch(r'\d{1,30}', uid):
        raise ValueError('Unexpected EcoFlow user ID')
    devices = request('/iot-service/user/device', token)
    if args.serial not in (devices.get('bound') or {}):
        raise ValueError('Device serial was not found among the account’s bound devices')
    result = (history(token, args.serial, args.date.isoformat()) if args.mode == 'history'
              else live(token, uid, args.serial, args.seconds, args.client_key.upper()))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except ImportError:
        sys.exit('Live collection requires paho-mqtt 2.x. See the README.')
    except urllib.error.HTTPError as exc:
        sys.exit(f'EcoFlow HTTP request failed with status {exc.code}')
    except (ValueError, KeyError, TypeError, OSError, AttributeError):
        sys.exit('EcoFlow collection failed. Check the account, device serial, and connection.')
