import argparse
import flask
import json
import requests
import os
import sqlite3
import zlib

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import request, Response, jsonify, render_template
from waitress import serve
from iniparse import RawConfigParser


# ============================================================
# Flask
# ============================================================

app = flask.Flask(__name__)

dirname = os.path.dirname(os.path.realpath(__file__))

APP_HOST = "127.0.0.1"
APP_PORT = 8889

DEBUG_MODE = False


# ============================================================
# Chargement fichiers
# ============================================================

with open(os.path.join(dirname, "AirSend.json"), "r") as f:
    datajson = json.load(f)["devices"]

with open(os.path.join(dirname, "groupes.json"), "r") as f:
    groupes_volet = json.load(f)


dict_groupes = {}

for g in groupes_volet:
    dict_groupes[g["name"]] = g


# ============================================================
# Configuration
# ============================================================

inifile = RawConfigParser()
inifile.read(os.path.join(dirname, "config.ini"))

ip_airsend = inifile.get(
    "global",
    "ip_airsend"
)

password_airsend = inifile.get(
    "global",
    "password_airsend"
)

airsendwebservice = inifile.get(
    "global",
    "airsendwebservice"
).rstrip("/")


# ============================================================
# Sondes connues
#
# Exemple config.ini :
#
# [temperature_sensors]
# 11910489 = Salon
# 3798813 = Étage
# ============================================================

temperature_sensors = {}

if inifile.has_section("temperature_sensors"):

    for source, name in inifile.items(
        "temperature_sensors"
    ):

        temperature_sensors[
            int(source)
        ] = name


# ============================================================
# Température extérieure Open-Meteo
#
# AUCUNE donnée Open-Meteo n'est stockée dans SQLite.
# Les données sont demandées à Open-Meteo uniquement quand
# l'utilisateur consulte ou modifie la période d'historique.
# ============================================================

OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
)

OPEN_METEO_ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
)



if inifile.has_section("open_meteo"):

    if inifile.has_option(
        "open_meteo",
        "enabled"
    ):
        OPEN_METEO_ENABLED = inifile.getboolean(
            "open_meteo",
            "enabled"
        )

    if inifile.has_option(
        "open_meteo",
        "name"
    ):
        OPEN_METEO_NAME = inifile.get(
            "open_meteo",
            "name"
        )

    if inifile.has_option(
        "open_meteo",
        "latitude"
    ):
        OPEN_METEO_LATITUDE = inifile.getfloat(
            "open_meteo",
            "latitude"
        )

    if inifile.has_option(
        "open_meteo",
        "longitude"
    ):
        OPEN_METEO_LONGITUDE = inifile.getfloat(
            "open_meteo",
            "longitude"
        )

    if inifile.has_option(
        "open_meteo",
        "timezone"
    ):
        OPEN_METEO_TIMEZONE = inifile.get(
            "open_meteo",
            "timezone"
        )

    if inifile.has_option(
        "open_meteo",
        "forecast_url"
    ):
        OPEN_METEO_FORECAST_URL = inifile.get(
            "open_meteo",
            "forecast_url"
        ).rstrip("/")

    if inifile.has_option(
        "open_meteo",
        "history_url"
    ):
        OPEN_METEO_ARCHIVE_URL = inifile.get(
            "open_meteo",
            "history_url"
        ).rstrip("/")


print("Sondes configurées :")

for source, name in temperature_sensors.items():

    print(
        f"  {source} -> {name}"
    )


# ============================================================
# SQLite
# ============================================================

if inifile.has_option(
    "temperature",
    "database"
):

    db_config = inifile.get(
        "temperature",
        "database"
    )

else:

    db_config = "temperatures.db"


if os.path.isabs(db_config):

    DB_FILE = db_config

else:

    DB_FILE = os.path.join(
        dirname,
        db_config
    )


def get_db():

    db = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    db.row_factory = sqlite3.Row

    return db


def init_db():

    with get_db() as db:

        # ----------------------------------------------------
        # Dernier état connu
        # ----------------------------------------------------

        db.execute("""
            CREATE TABLE IF NOT EXISTS temperature_state (
                source INTEGER PRIMARY KEY,
                name TEXT NOT NULL,

                ambient REAL,
                ambient_updated TEXT,

                setpoint REAL,
                setpoint_updated TEXT
            )
        """)


        # ----------------------------------------------------
        # Historique
        #
        # Une ligne est enregistrée uniquement quand
        # la valeur change.
        #
        # Aucune purge automatique.
        # ----------------------------------------------------

        db.execute("""
            CREATE TABLE IF NOT EXISTS temperature_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp TEXT NOT NULL,

                source INTEGER NOT NULL,
                name TEXT NOT NULL,

                type TEXT NOT NULL,
                value REAL NOT NULL,

                raw TEXT
            )
        """)


        # ----------------------------------------------------
        # Index historique
        # ----------------------------------------------------

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_temperature_history_source_timestamp
            ON temperature_history(source, timestamp)
        """)


        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_temperature_history_source_type_timestamp
            ON temperature_history(source, type, timestamp)
        """)


        db.commit()


init_db()


# ============================================================
# Décodage IOU
# ============================================================

contexts = {}


# ------------------------------------------------------------
# data_id identifiés
#
# Le data_id se trouve ici :
#
# 48 00 0C 00 10 01 XX XX 00 02
#                   ^^^^^
#
# ou :
#
# 46 00 0C 00 10 02 XX XX 00 00
#                   ^^^^^
#
# Observé :
#
# 0x0089 -> température demandée
# 0x008A -> température demandée
#
# 0x018A -> température ambiante
# 0x018B -> température ambiante
# ------------------------------------------------------------

TEMPERATURE_DATA_IDS = {

    0x0089: "setpoint",
    0x008A: "setpoint",

    0x018A: "ambient",
    0x018B: "ambient",
}


def get_context(source):

    if source not in contexts:

        contexts[source] = {
            "mode": None,
            "data_id": None,
            "last_header": None
        }

    return contexts[source]


# ============================================================
# Normalisation valeur AirSend
# ============================================================

def parse_note_value(value, bits):

    if isinstance(value, int):

        return (
            value,
            f"0x{value:0{bits // 4}X}"
        )


    if isinstance(value, str):

        text = value.strip()

        if text.lower().startswith("0x"):

            intval = int(
                text,
                16
            )

            return (
                intval,
                f"0x{intval:0{bits // 4}X}"
            )

        return (
            None,
            text
        )


    return (
        None,
        str(value)
    )


# ============================================================
# Stockage température
# ============================================================

def save_temperature(
    source,
    temp_type,
    value,
    raw=None
):

    if source not in temperature_sensors:
        return


    name = temperature_sensors[source]

    now = datetime.now().isoformat(
        timespec="milliseconds"
    )


    with get_db() as db:

        old = db.execute(
            """
            SELECT
                ambient,
                setpoint

            FROM temperature_state

            WHERE source = ?
            """,
            (source,)
        ).fetchone()


        # ----------------------------------------------------
        # Température ambiante
        # ----------------------------------------------------

        if temp_type == "ambient":

            old_value = (
                old["ambient"]
                if old is not None
                else None
            )


            db.execute(
                """
                INSERT INTO temperature_state (
                    source,
                    name,
                    ambient,
                    ambient_updated
                )

                VALUES (?, ?, ?, ?)

                ON CONFLICT(source) DO UPDATE SET
                    name = excluded.name,
                    ambient = excluded.ambient,
                    ambient_updated = excluded.ambient_updated
                """,
                (
                    source,
                    name,
                    value,
                    now
                )
            )


        # ----------------------------------------------------
        # Température demandée
        # ----------------------------------------------------

        elif temp_type == "setpoint":

            old_value = (
                old["setpoint"]
                if old is not None
                else None
            )


            db.execute(
                """
                INSERT INTO temperature_state (
                    source,
                    name,
                    setpoint,
                    setpoint_updated
                )

                VALUES (?, ?, ?, ?)

                ON CONFLICT(source) DO UPDATE SET
                    name = excluded.name,
                    setpoint = excluded.setpoint,
                    setpoint_updated = excluded.setpoint_updated
                """,
                (
                    source,
                    name,
                    value,
                    now
                )
            )


        else:

            return


        # ----------------------------------------------------
        # Historique
        #
        # On enregistre uniquement si la valeur change.
        #
        # L'historique n'est jamais supprimé.
        # ----------------------------------------------------

        if old_value is None or old_value != value:

            db.execute(
                """
                INSERT INTO temperature_history (
                    timestamp,
                    source,
                    name,
                    type,
                    value,
                    raw
                )

                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    source,
                    name,
                    temp_type,
                    value,
                    raw
                )
            )


        db.commit()


# ============================================================
# Open-Meteo
#
# Ces fonctions ne font qu'interroger l'API distante.
# Elles n'appellent jamais save_temperature() et n'écrivent
# donc jamais dans temperatures.db.
# ============================================================

def _open_meteo_now():

    # Les dates envoyées à Open-Meteo doivent suivre le fuseau
    # configuré et non le fuseau système du serveur.
    try:
        return datetime.now(
            ZoneInfo(OPEN_METEO_TIMEZONE)
        ).replace(tzinfo=None)
    except ZoneInfoNotFoundError:
        return datetime.now()


def _parse_open_meteo_current(payload):

    current = payload.get(
        "current",
        {}
    )

    value = current.get(
        "temperature_2m"
    )

    if value is None:
        return None

    return {
        "timestamp": current.get("time"),
        "value": float(value)
    }


def _parse_open_meteo_history_points(
    payload,
    since_datetime,
    until_datetime
):

    hourly = payload.get(
        "hourly",
        {}
    )

    times = hourly.get(
        "time",
        []
    )

    values = hourly.get(
        "temperature_2m",
        []
    )

    points = []

    for timestamp, value in zip(
        times,
        values
    ):

        if value is None:
            continue

        try:
            point_datetime = datetime.fromisoformat(
                timestamp
            )
        except (TypeError, ValueError):
            continue

        if not (
            since_datetime
            <= point_datetime
            < until_datetime
        ):
            continue

        points.append(
            {
                "timestamp": timestamp,
                "name": OPEN_METEO_NAME,
                "type": "ambient",
                "value": float(value)
            }
        )

    return points


def _reduce_open_meteo_history(
    points,
    bucket_seconds
):

    # L'API Open-Meteo fournit des valeurs horaires. Pour les
    # très longues périodes, on applique la même logique de
    # réduction que l'historique local afin de garder le graphe
    # fluide.
    if (
        bucket_seconds is None
        or bucket_seconds <= 3600
    ):
        return points

    buckets = {}

    for point in points:
        point_datetime = datetime.fromisoformat(
            point["timestamp"]
        )

        bucket_key = int(
            point_datetime.timestamp()
            // bucket_seconds
        )

        bucket = buckets.setdefault(
            bucket_key,
            {
                "timestamp": point["timestamp"],
                "values": []
            }
        )

        bucket["values"].append(
            point["value"]
        )

    reduced = []

    for bucket_key in sorted(buckets):
        bucket = buckets[bucket_key]

        reduced.append(
            {
                "timestamp": bucket["timestamp"],
                "name": OPEN_METEO_NAME,
                "type": "ambient",
                "value": sum(bucket["values"]) / len(bucket["values"])
            }
        )

    return reduced


def fetch_open_meteo_current():

    response = requests.get(
        OPEN_METEO_FORECAST_URL,
        params={
            "latitude": OPEN_METEO_LATITUDE,
            "longitude": OPEN_METEO_LONGITUDE,
            "current": "temperature_2m",
            "timezone": OPEN_METEO_TIMEZONE
        },
        timeout=10
    )

    response.raise_for_status()

    return _parse_open_meteo_current(
        response.json()
    )


def fetch_open_meteo_history(
    since_datetime,
    until_datetime,
    bucket_seconds=None
):

    now = _open_meteo_now()

    effective_until = min(
        until_datetime,
        now
    )

    if effective_until <= since_datetime:
        return [], None

    # Le Forecast API sait fournir jusqu'à 92 jours passés.
    # Pour les vues usuelles (6 h à 3 mois), on récupère donc
    # historique + valeur courante en UN SEUL appel. Cela évite
    # surtout d'envoyer la date du jour à l'Archive API, qui peut
    # n'accepter que la veille selon l'heure de mise à jour.
    days_back = max(
        0,
        (
            now.date()
            - since_datetime.date()
        ).days
    )

    if days_back <= 92:

        response = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": OPEN_METEO_LATITUDE,
                "longitude": OPEN_METEO_LONGITUDE,
                "hourly": "temperature_2m",
                "current": "temperature_2m",
                "past_days": days_back,
                "forecast_days": 1,
                "timezone": OPEN_METEO_TIMEZONE
            },
            timeout=20
        )

        response.raise_for_status()

        payload = response.json()

        points = _parse_open_meteo_history_points(
            payload,
            since_datetime,
            effective_until
        )

        return (
            _reduce_open_meteo_history(
                points,
                bucket_seconds
            ),
            _parse_open_meteo_current(
                payload
            )
        )

    # Pour une longue période (par exemple 1 an), l'Archive API
    # reste adaptée. Sa date de fin est volontairement plafonnée
    # à hier : elle ne reçoit donc jamais la date du jour.
    today_start = datetime.combine(
        now.date(),
        datetime.min.time()
    )

    archive_until = min(
        effective_until,
        today_start
    )

    points = []
    current = None

    if archive_until > since_datetime:

        archive_end_inclusive = (
            archive_until
            - timedelta(microseconds=1)
        )

        response = requests.get(
            OPEN_METEO_ARCHIVE_URL,
            params={
                "latitude": OPEN_METEO_LATITUDE,
                "longitude": OPEN_METEO_LONGITUDE,
                "start_date": since_datetime.date().isoformat(),
                "end_date": archive_end_inclusive.date().isoformat(),
                "hourly": "temperature_2m",
                "timezone": OPEN_METEO_TIMEZONE
            },
            timeout=20
        )

        response.raise_for_status()

        points.extend(
            _parse_open_meteo_history_points(
                response.json(),
                since_datetime,
                archive_until
            )
        )

    # Si la période inclut aujourd'hui, le Forecast API complète
    # uniquement la journée en cours et fournit aussi la valeur
    # courante. Pour une plage entièrement passée, l'appel courant
    # sera effectué ensuite par l'endpoint.
    if effective_until > today_start:

        recent_since = max(
            since_datetime,
            today_start
        )

        response = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": OPEN_METEO_LATITUDE,
                "longitude": OPEN_METEO_LONGITUDE,
                "hourly": "temperature_2m",
                "current": "temperature_2m",
                "forecast_days": 1,
                "timezone": OPEN_METEO_TIMEZONE
            },
            timeout=20
        )

        response.raise_for_status()

        payload = response.json()

        points.extend(
            _parse_open_meteo_history_points(
                payload,
                recent_since,
                effective_until
            )
        )

        current = _parse_open_meteo_current(
            payload
        )

    points.sort(
        key=lambda point: point["timestamp"]
    )

    return (
        _reduce_open_meteo_history(
            points,
            bucket_seconds
        ),
        current
    )


# ============================================================
# Pages
# ============================================================

@app.route("/individuel")
def index():

    return render_template(
        "index.html.j2",
        datajson=datajson
    )


@app.route("/")
def groupes():

    temperatures = {}


    # --------------------------------------------------------
    # Toutes les sondes connues
    # --------------------------------------------------------

    for source, name in temperature_sensors.items():

        temperatures[source] = {

            "name": name,

            "ambient": None,
            "ambient_updated": None,

            "setpoint": None,
            "setpoint_updated": None
        }


    # --------------------------------------------------------
    # Dernières valeurs SQLite
    # --------------------------------------------------------

    with get_db() as db:

        rows = db.execute(
            """
            SELECT
                source,
                name,

                ambient,
                ambient_updated,

                setpoint,
                setpoint_updated

            FROM temperature_state
            """
        ).fetchall()


        for row in rows:

            source = row["source"]


            if source not in temperature_sensors:
                continue


            temperatures[source] = {

                "name":
                    temperature_sensors[source],

                "ambient":
                    row["ambient"],

                "ambient_updated":
                    row["ambient_updated"],

                "setpoint":
                    row["setpoint"],

                "setpoint_updated":
                    row["setpoint_updated"]
            }


    return render_template(
        "index.html.j2",

        datajson=groupes_volet,

        temperatures=temperatures
    )


# ============================================================
# Page historique
# ============================================================

@app.route("/historique")
def historique():

    sensors = [

        {
            "source": source,
            "name": name
        }

        for source, name
        in sorted(
            temperature_sensors.items(),
            key=lambda item: item[1].lower()
        )
    ]


    return render_template(
        "historique.html.j2",
        sensors=sensors,
        outdoor_enabled=OPEN_METEO_ENABLED,
        outdoor_name=OPEN_METEO_NAME
    )


# ============================================================
# API volets
# ============================================================

@app.route(
    "/api/action",
    methods=["POST"]
)
def api_action():

    if request.headers.get(
        "Content-Encoding"
    ) == "gzip":

        raw_data = zlib.decompress(
            request.data
        )

    else:

        raw_data = request.data


    dataapi = json.loads(
        raw_data
    )


    headers = {

        "accept":
            "application/json",

        "Content-Type":
            "application/json",

        "Authorization":
            "Bearer sp://%s@[%s]?gw=0"
            % (
                password_airsend,
                ip_airsend
            )
    }


    if dataapi["voletId"] in dict_groupes:

        sendto = dict_groupes[
            dataapi["voletId"]
        ]["entries"]

    else:

        sendto = [
            dataapi["voletId"]
        ]


    for entry in datajson:

        if entry["name"] not in sendto:
            continue


        data = {

            "wait": False,

            "channel": {

                "id":
                    entry["pid"],

                "source":
                    entry["addr"]
            },

            "thingnotes": {

                "notes": [
                    {
                        "method":
                            "PUT",

                        "type":
                            "STATE",

                        "value":
                            dataapi["action"]
                    }
                ]
            }
        }


        response = requests.post(

            f"{airsendwebservice}/airsend/transfer",

            headers=headers,

            json=data,

            timeout=10
        )


        response.raise_for_status()


    return "OK"


# ============================================================
# Callback AirSend
# ============================================================

@app.route(
    "/airsend",
    methods=["POST"]
)
def airsend_callback():

    data = request.get_json(
        force=True,
        silent=False
    )


    for event in data.get(
        "events",
        []
    ):

        channel = event.get(
            "channel",
            {}
        )


        source = channel.get(
            "source"
        )


        if source is None:
            continue


        # ----------------------------------------------------
        # Seules les sondes connues sont traitées
        # ----------------------------------------------------

        if source not in temperature_sensors:
            continue


        name = temperature_sensors[
            source
        ]


        context = get_context(
            source
        )


        notes = event.get(
            "thingnotes",
            {}
        ).get(
            "notes",
            []
        )


        for note in notes:

            value = note.get(
                "value"
            )


            bits = note.get(
                "value_binsize"
            )


            if bits is None:
                continue


            raw_value, formatted = parse_note_value(
                value,
                bits
            )


            # =================================================
            # TRAME 80 BITS
            #
            # Les types 0x46 et 0x48 sont observés.
            #
            # Le data_id est extrait avec :
            #
            # (raw_value >> 16) & 0xFFFF
            # =================================================

            if bits == 80:

                context[
                    "last_header"
                ] = formatted


                # On annule l'ancien contexte
                context["mode"] = None
                context["data_id"] = None


                if raw_value is None:

                    if DEBUG_MODE:

                        print(
                            f"DEBUG IOU | "
                            f"{name} | "
                            f"80 bits non décodable | "
                            f"{formatted}"
                        )

                    continue


                first_byte = (
                    raw_value >> 72
                ) & 0xFF


                if first_byte in (
                    0x46,
                    0x48
                ):

                    data_id = (
                        raw_value >> 16
                    ) & 0xFFFF


                    mode = (
                        TEMPERATURE_DATA_IDS.get(
                            data_id
                        )
                    )


                    context[
                        "data_id"
                    ] = data_id

                    context[
                        "mode"
                    ] = mode


                    if DEBUG_MODE:

                        if mode is not None:

                            print(
                                f"DEBUG IOU | "
                                f"{name} | "
                                f"header=0x{first_byte:02X} | "
                                f"data_id=0x{data_id:04X} | "
                                f"mode={mode} | "
                                f"{formatted}"
                            )

                        else:

                            print(
                                f"DEBUG IOU | "
                                f"{name} | "
                                f"header=0x{first_byte:02X} | "
                                f"data_id inconnu="
                                f"0x{data_id:04X} | "
                                f"{formatted}"
                            )


                elif DEBUG_MODE:

                    print(
                        f"DEBUG IOU | "
                        f"{name} | "
                        f"80 bits hors température | "
                        f"{formatted}"
                    )


                continue


            # =================================================
            # TRAME 48 BITS
            #
            # Format :
            #
            # 4A XX 00 01 TT TT
            #
            # TT TT = température x10
            # =================================================

            if (
                bits == 48
                and raw_value is not None
            ):

                first_byte = (
                    raw_value >> 40
                ) & 0xFF


                marker = (
                    raw_value >> 16
                ) & 0xFFFF


                if first_byte != 0x4A:
                    continue


                if marker != 0x0001:
                    continue


                raw_temp = (
                    raw_value
                    & 0xFFFF
                )


                temp = (
                    raw_temp
                    / 10.0
                )


                # ---------------------------------------------
                # Filtre de cohérence
                # ---------------------------------------------

                if not (
                    -40.0
                    <= temp
                    <= 80.0
                ):
                    continue


                mode = context.get(
                    "mode"
                )


                data_id = context.get(
                    "data_id"
                )


                # ---------------------------------------------
                # Température demandée
                # ---------------------------------------------

                if mode == "setpoint":

                    save_temperature(
                        source,
                        "setpoint",
                        temp,
                        formatted
                    )


                    print(
                        f"{datetime.now().strftime('%H:%M:%S')} | "
                        f"{name:<15} | "
                        f"Demandée: {temp:.1f} °C"
                    )


                # ---------------------------------------------
                # Température ambiante
                # ---------------------------------------------

                elif mode == "ambient":

                    save_temperature(
                        source,
                        "ambient",
                        temp,
                        formatted
                    )


                    print(
                        f"{datetime.now().strftime('%H:%M:%S')} | "
                        f"{name:<15} | "
                        f"Ambiante: {temp:.1f} °C"
                    )


                # ---------------------------------------------
                # Valeur température avec contexte inconnu
                # ---------------------------------------------

                elif DEBUG_MODE:

                    if data_id is None:

                        data_id_text = "aucun"

                    else:

                        data_id_text = (
                            f"0x{data_id:04X}"
                        )


                    print(
                        f"DEBUG IOU | "
                        f"{name} | "
                        f"température ignorée "
                        f"{temp:.1f} °C | "
                        f"data_id={data_id_text} | "
                        f"{formatted} | "
                        f"header="
                        f"{context.get('last_header')}"
                    )


                # ---------------------------------------------
                # La valeur 48 bits consomme le contexte
                # ---------------------------------------------

                context["mode"] = None
                context["data_id"] = None


    return Response(
        "OK",
        status=200,
        mimetype="text/plain"
    )


# ============================================================
# API températures actuelles
# ============================================================

@app.route(
    "/api/temperatures"
)
def api_temperatures():

    with get_db() as db:

        rows = db.execute(
            """
            SELECT
                source,
                name,

                ambient,
                ambient_updated,

                setpoint,
                setpoint_updated

            FROM temperature_state

            ORDER BY name
            """
        ).fetchall()


    result = []


    for row in rows:

        if row["source"] not in temperature_sensors:
            continue


        result.append(
            dict(row)
        )


    return jsonify(
        result
    )


# ============================================================
# Résolution de l'historique pour le graphique
#
# IMPORTANT :
#
# Cela ne touche JAMAIS aux données SQLite.
#
# L'historique SQLite reste complet et sans limite de durée.
#
# La réduction ne concerne que la réponse envoyée au
# navigateur pour éviter de faire charger des dizaines
# de milliers de points à Chart.js.
# ============================================================

def get_history_bucket_seconds(hours):

    # --------------------------------------------------------
    # Jusqu'à 3 jours :
    # tous les changements enregistrés
    # --------------------------------------------------------

    if hours <= 72:

        return None


    # --------------------------------------------------------
    # Jusqu'à 7 jours :
    # 5 minutes
    # --------------------------------------------------------

    if hours <= 168:

        return 5 * 60


    # --------------------------------------------------------
    # Jusqu'à 30 jours :
    # 30 minutes
    # --------------------------------------------------------

    if hours <= 720:

        return 30 * 60


    # --------------------------------------------------------
    # Jusqu'à 3 mois :
    # 1 heure
    # --------------------------------------------------------

    if hours <= 2160:

        return 60 * 60


    # --------------------------------------------------------
    # Jusqu'à 1 an :
    # 6 heures
    #
    # Environ 1460 buckets pour 365 jours.
    # --------------------------------------------------------

    if hours <= 8760:

        return 6 * 60 * 60


    # --------------------------------------------------------
    # Plus d'un an :
    #
    # Résolution automatique pour viser environ
    # 2000 points maximum.
    # --------------------------------------------------------

    target_points = 2000

    calculated_bucket = int(
        (
            hours
            * 60
            * 60
        )
        / target_points
    )


    # Jamais moins de 6 heures
    return max(
        6 * 60 * 60,
        calculated_bucket
    )


# ============================================================
# API Open-Meteo à la demande
#
# Même syntaxe de période que /api/temperatures/history :
#   ?hours=24
# ou
#   ?start=2026-08-01&end=2026-08-15
#
# Aucun résultat n'est écrit dans SQLite.
# ============================================================

@app.route(
    "/api/open-meteo/history"
)
def api_open_meteo_history():

    if not OPEN_METEO_ENABLED:
        return jsonify(
            {
                "history": [],
                "current": None
            }
        )

    hours = request.args.get(
        "hours",
        default=24,
        type=int
    )

    start_date = request.args.get(
        "start"
    )

    end_date = request.args.get(
        "end"
    )

    if start_date or end_date:

        if not start_date or not end_date:
            return jsonify(
                {
                    "error":
                        "Les paramètres start et end sont requis ensemble."
                }
            ), 400

        try:
            since_datetime = datetime.strptime(
                start_date,
                "%Y-%m-%d"
            )

            end_day = datetime.strptime(
                end_date,
                "%Y-%m-%d"
            )
        except ValueError:
            return jsonify(
                {
                    "error":
                        "Format de date invalide. Utilisez AAAA-MM-JJ."
                }
            ), 400

        if end_day < since_datetime:
            return jsonify(
                {
                    "error":
                        "La date de fin doit être postérieure ou égale à la date de début."
                }
            ), 400

        until_datetime = (
            end_day
            + timedelta(days=1)
        )

        hours = max(
            1,
            int(
                (
                    until_datetime
                    - since_datetime
                ).total_seconds()
                / 3600
            )
        )

    else:
        hours = max(
            1,
            hours or 24
        )

        until_datetime = _open_meteo_now()

        since_datetime = (
            until_datetime
            - timedelta(hours=hours)
        )

    try:
        history, current = fetch_open_meteo_history(
            since_datetime,
            until_datetime,
            get_history_bucket_seconds(hours)
        )

    except Exception as exc:
        print(
            "Erreur historique Open-Meteo:",
            exc
        )

        return jsonify(
            {
                "error":
                    "Impossible de récupérer l'historique Open-Meteo."
            }
        ), 502

    # Pour les périodes récentes, la valeur courante est déjà
    # incluse dans le même appel Forecast que l'historique. Pour
    # une ancienne plage entièrement passée, on la récupère ici.
    if current is None:
        try:
            current = fetch_open_meteo_current()
        except Exception as exc:
            print(
                "Erreur température actuelle Open-Meteo:",
                exc
            )
            current = None

    return jsonify(
        {
            "history": history,
            "current": current
        }
    )


# ============================================================
# API historique températures
#
# Exemples :
#
# /api/temperatures/history?source=11910489&hours=24
#
# /api/temperatures/history?source=11910489&hours=8760
#
# SQLite :
#   aucune suppression
#
# API :
#   réduction automatique pour les longues périodes
# ============================================================

@app.route(
    "/api/temperatures/history"
)
def api_temperature_history():

    source = request.args.get(
        "source",
        type=int
    )


    hours = request.args.get(
        "hours",
        default=24,
        type=int
    )


    start_date = request.args.get(
        "start"
    )


    end_date = request.args.get(
        "end"
    )


    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if source is None:

        return jsonify([])


    if source not in temperature_sensors:

        return jsonify([])


    # --------------------------------------------------------
    # Bornes de période
    #
    # Deux modes sont acceptés :
    #
    #   ?hours=24
    #
    # ou une plage de dates inclusive :
    #
    #   ?start=2026-08-01&end=2026-08-15
    #
    # La date de fin est convertie en borne exclusive au
    # lendemain à 00:00, ce qui inclut toute la journée "end".
    # --------------------------------------------------------

    if start_date or end_date:

        if not start_date or not end_date:

            return jsonify(
                {
                    "error":
                        "Les paramètres start et end sont requis ensemble."
                }
            ), 400


        try:

            since_datetime = datetime.strptime(
                start_date,
                "%Y-%m-%d"
            )


            end_day = datetime.strptime(
                end_date,
                "%Y-%m-%d"
            )

        except ValueError:

            return jsonify(
                {
                    "error":
                        "Format de date invalide. Utilisez AAAA-MM-JJ."
                }
            ), 400


        if end_day < since_datetime:

            return jsonify(
                {
                    "error":
                        "La date de fin doit être postérieure ou égale à la date de début."
                }
            ), 400


        until_datetime = (
            end_day
            + timedelta(
                days=1
            )
        )


        hours = max(
            1,
            int(
                (
                    until_datetime
                    - since_datetime
                ).total_seconds()
                / 3600
            )
        )

    else:

        if hours is None:

            hours = 24


        # Pas de limite supérieure.
        hours = max(
            1,
            hours
        )


        until_datetime = datetime.now()


        since_datetime = (
            until_datetime
            - timedelta(
                hours=hours
            )
        )


    since = since_datetime.isoformat(
        timespec="milliseconds"
    )


    until = until_datetime.isoformat(
        timespec="milliseconds"
    )


    # --------------------------------------------------------
    # Résolution d'affichage
    # --------------------------------------------------------

    bucket_seconds = (
        get_history_bucket_seconds(
            hours
        )
    )


    result = []


    with get_db() as db:

        # ====================================================
        # Température ambiante
        # ====================================================

        if bucket_seconds is None:

            # ------------------------------------------------
            # Période courte :
            #
            # on renvoie tous les changements enregistrés.
            # ------------------------------------------------

            ambient_rows = db.execute(
                """
                SELECT
                    timestamp,
                    source,
                    name,
                    type,
                    value,
                    raw

                FROM temperature_history

                WHERE source = ?
                  AND type = 'ambient'
                  AND timestamp >= ?
                  AND timestamp < ?

                ORDER BY timestamp ASC
                """,
                (
                    source,
                    since,
                    until
                )
            ).fetchall()


        else:

            # ------------------------------------------------
            # Période longue :
            #
            # On regroupe les changements uniquement pour
            # l'affichage.
            # ------------------------------------------------

            ambient_rows = db.execute(
                """
                SELECT
                    MIN(timestamp) AS timestamp,

                    source,

                    MAX(name) AS name,

                    'ambient' AS type,

                    AVG(value) AS value,

                    NULL AS raw

                FROM temperature_history

                WHERE source = ?
                  AND type = 'ambient'
                  AND timestamp >= ?
                  AND timestamp < ?

                GROUP BY
                    CAST(
                        strftime(
                            '%s',
                            timestamp
                        ) / ?
                        AS INTEGER
                    )

                ORDER BY timestamp ASC
                """,
                (
                    source,
                    since,
                    until,
                    bucket_seconds
                )
            ).fetchall()


        for row in ambient_rows:

            result.append(
                dict(row)
            )


        # ====================================================
        # Température demandée
        # ====================================================

        setpoint_rows = db.execute(
            """
            SELECT
                timestamp,
                source,
                name,
                type,
                value,
                raw

            FROM temperature_history

            WHERE source = ?
              AND type = 'setpoint'
              AND timestamp >= ?
              AND timestamp < ?

            ORDER BY timestamp ASC
            """,
            (
                source,
                since,
                until
            )
        ).fetchall()


        for row in setpoint_rows:

            result.append(
                dict(row)
            )


        # ====================================================
        # Valeur connue avant le début de période
        #
        # Elle permet au graphique de démarrer avec l'état
        # connu au début de la plage, sans modifier SQLite.
        # ====================================================

        for temp_type in (
            "ambient",
            "setpoint"
        ):

            previous = db.execute(
                """
                SELECT
                    timestamp,
                    source,
                    name,
                    type,
                    value,
                    raw

                FROM temperature_history

                WHERE source = ?
                  AND type = ?
                  AND timestamp < ?

                ORDER BY timestamp DESC

                LIMIT 1
                """,
                (
                    source,
                    temp_type,
                    since
                )
            ).fetchone()


            if previous is not None:

                previous = dict(
                    previous
                )


                previous[
                    "timestamp"
                ] = since


                previous[
                    "synthetic"
                ] = True


                result.append(
                    previous
                )


    # --------------------------------------------------------
    # Ordre chronologique final
    # --------------------------------------------------------

    result.sort(
        key=lambda row:
            row["timestamp"]
    )


    return jsonify(
        result
    )


# ============================================================
# Bind AirSend IOU
# ============================================================

def bind_airsend():

    callback_url = (
        f"http://{APP_HOST}:{APP_PORT}/airsend"
    )


    headers = {

        "accept":
            "application/json",

        "Content-Type":
            "application/json",

        "Authorization":
            "Bearer sp://%s@[%s]?gw=0"
            % (
                password_airsend,
                ip_airsend
            )
    }


    data = {

        "channel": {
            "id": 26848
        },

        "duration": 0,

        "callback":
            callback_url
    }


    response = requests.post(

        f"{airsendwebservice}/airsend/bind",

        headers=headers,

        json=data,

        timeout=10
    )


    response.raise_for_status()


    print(
        f"AirSend IOU bind OK : "
        f"{airsendwebservice} -> "
        f"{callback_url}"
    )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--debug",
        action="store_true",
        help="Démarre Flask avec le debug IOU"
    )


    args = parser.parse_args()

    DEBUG_MODE = args.debug


    print(
        "Base SQLite :",
        DB_FILE
    )


    print(
        "AirSendWebService :",
        airsendwebservice
    )


    # --------------------------------------------------------
    # Un seul bind au démarrage
    # --------------------------------------------------------

    bind_airsend()


    # --------------------------------------------------------
    # Debug Flask
    # --------------------------------------------------------

    if DEBUG_MODE:

        print(
            "Mode DEBUG Flask"
        )


        app.run(
            host=APP_HOST,
            port=APP_PORT,
            debug=True,

            # Pas de reloader sinon Flask démarre
            # deux processus et ferait deux bind.
            use_reloader=False
        )


    # --------------------------------------------------------
    # Production Waitress
    # --------------------------------------------------------

    else:

        print(
            "Mode production Waitress"
        )


        serve(
            app,
            host=APP_HOST,
            port=APP_PORT,
            threads=5
        )
