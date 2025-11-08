import os, json, re, tempfile, datetime as dt, traceback
import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# === Cargar variables .env ===
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GSPREAD_SHEET_NAME", "gastos_diarios")
SA_JSON_PATH = os.getenv("GSPREAD_SA_JSON", "./service_account.json")
TZ = pytz.timezone(os.getenv("TZ", "America/Bogota"))

# === Config de transcripción de audio ===
# Engine por defecto: OpenAI (recomendado para Railway). Alternativa: faster-whisper
TRANSCRIBE_ENGINE = (os.getenv("TRANSCRIBE_ENGINE", "openai") or "openai").lower()
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
# Opcionales para faster-whisper
FAST_WHISPER_MODEL = os.getenv("FAST_WHISPER_MODEL", "small")
FAST_WHISPER_DEVICE = os.getenv("FAST_WHISPER_DEVICE", "cpu")
FAST_WHISPER_COMPUTE = os.getenv("FAST_WHISPER_COMPUTE", "int8")

# === Inicializar clientes ===
client = OpenAI(api_key=OPENAI_API_KEY)

# === Google Sheets helpers ===
# Estructura esperada en la hoja 'gastos_diarios'
GASTOS_HEADERS = [
    "fecha","hora","valor","tienda","categoria_id","categoria","subcategoria","detalle"
]

# --- Soporte para credencial desde variable de entorno ---
def ensure_sa_file():
    sa_json_env = os.getenv("SERVICE_ACCOUNT_JSON")
    if sa_json_env:
        try:
            if (not os.path.exists(SA_JSON_PATH)) or os.path.getsize(SA_JSON_PATH) == 0:
                with open(SA_JSON_PATH, "w", encoding="utf-8") as f:
                    f.write(sa_json_env)
        except Exception as e:
            print("No pude escribir service_account.json desde SERVICE_ACCOUNT_JSON:", e)

ensure_sa_file()

def gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SA_JSON_PATH, scopes=scopes)
    return gspread.authorize(creds)

def get_gastos_ws():
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    # Intentar abrir la hoja por nombre explícito; si no existe, crearla
    try:
        return sh.worksheet("gastos_diarios")
    except Exception:
        try:
            ws = sh.add_worksheet(title="gastos_diarios", rows=1000, cols=len(GASTOS_HEADERS))
            ws.append_row(GASTOS_HEADERS)
            return ws
        except Exception:
            # Último recurso: usar sheet1
            return sh.sheet1

def get_categorias_ws():
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    # Hoja "categorias" con columnas: Id, Categoria, Subcategoria, Descripción
    try:
        return sh.worksheet("categorias")
    except Exception:
        ws = sh.add_worksheet(title="categorias", rows=1000, cols=4)
        ws.append_row(["Id","Categoria","Subcategoria","Descripción"])
        return ws

def load_categorias_map():
    """Lee la hoja categorias y devuelve:
    - mapa {(Categoria, Subcategoria): Id}
    - max_id actual
    Usar Title Case normalizado para claves del mapa.
    """
    ws = get_categorias_ws()
    rows = ws.get_all_values()
    if not rows:
        return ws, {}, 0
    header = [h.strip() for h in rows[0]]
    idx_id = header.index("Id") if "Id" in header else 0
    idx_cat = header.index("Categoria") if "Categoria" in header else 1
    idx_sub = header.index("Subcategoria") if "Subcategoria" in header else 2
    mapping = {}
    max_id = 0
    for r in rows[1:]:
        if not any(r):
            continue
        try:
            rid = int(str(r[idx_id]).strip()) if idx_id < len(r) and r[idx_id] != "" else None
        except Exception:
            rid = None
        cat = (r[idx_cat] if idx_cat < len(r) else "").strip()
        sub = (r[idx_sub] if idx_sub < len(r) else "").strip()
        if rid is None:
            continue
        key = (to_title_case(cat), to_title_case(sub))
        mapping[key] = rid
        if rid > max_id:
            max_id = rid
    return ws, mapping, max_id

def ensure_categoria_id(categoria: str, subcategoria: str) -> int:
    """Devuelve el Id para (categoria, subcategoria). Si no existe en la hoja categorias, lo agrega.
    Aplica normalización y canónica para evitar duplicados por variantes.
    """
    categoria_tc, subcategoria_tc = canonicalize_cat_sub(categoria, subcategoria)
    ws_cat, mapping, max_id = load_categorias_map()
    key = (categoria_tc, subcategoria_tc)
    if key in mapping:
        return mapping[key]
    new_id = max_id + 1 if max_id else 1
    ws_cat.append_row([new_id, categoria_tc, subcategoria_tc, ""], value_input_option="USER_ENTERED")
    return new_id

def ensure_named_range_gastos(ws):
    """Asegura que el rango con nombre 'gastos' cubra muchas filas para que
    los nuevos registros queden dentro de la tabla. Si no existe, lo crea.
    """
    try:
        sh = ws.spreadsheet
        meta = sh.fetch_sheet_metadata()
        named = meta.get("namedRanges", []) or []
        target = None
        for nr in named:
            if (nr.get("name") or "").strip().lower() == "gastos":
                target = nr
                break
        end_rows = max(10000, ws.row_count)
        rng = {
            "sheetId": ws.id,
            "startRowIndex": 0,
            "startColumnIndex": 0,
            "endRowIndex": end_rows,
            "endColumnIndex": len(GASTOS_HEADERS),
        }
        if target and target.get("namedRangeId"):
            sh.batch_update({
                "requests": [
                    {
                        "updateNamedRange": {
                            "namedRange": {
                                "namedRangeId": target["namedRangeId"],
                                "range": rng,
                            },
                            "fields": "range",
                        }
                    }
                ]
            })
        else:
            sh.batch_update({
                "requests": [
                    {
                        "addNamedRange": {
                            "namedRange": {
                                "name": "gastos",
                                "range": rng,
                            }
                        }
                    }
                ]
            })
    except Exception:
        # Si no se puede, no bloquea la inserción
        pass
        pass

# === Utilidades de validación de fecha/hora ===
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RX = re.compile(r"^[0-2]\d:[0-5]\d$")  # 00:00–29:59 (luego verificamos rango real)

def is_valid_date(s: str) -> bool:
    if not s or not DATE_RX.match(s):
        return False
    try:
        dt.date.fromisoformat(s)
        return True
    except Exception:
        return False

def is_valid_time(s: str) -> bool:
    if not s or not TIME_RX.match(s):
        return False
    try:
        hh, mm = s.split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except Exception:
        return False

# === Helpers de texto ===
def to_title_case(s: str) -> str:
    if not s:
        return ""
    try:
        return str(s).strip().title()
    except Exception:
        return str(s).strip()

def canonicalize_cat_sub(categoria: str, subcategoria: str) -> tuple:
    """Mapea variantes a las etiquetas canónicas existentes en la hoja 'categorias'.
    Devuelve (Categoria, Subcategoria) normalizados en Title Case.
    """
    cat = to_title_case(categoria or "")
    sub = to_title_case(subcategoria or "")

    # Normaliza acentos y variantes simples
    def n(s: str) -> str:
        return (s or "").strip().lower().replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")

    cn = n(cat)
    sn = n(sub)

    # Categorías canónicas conocidas
    if cn in ("comida", "alimentacion", "alimentacion"):  # robustez doble
        cat = "Comida"
    elif cn == "negro":
        cat = "Negro"
    elif cn == "compras":
        cat = "Compras"
    elif cn == "entretenimiento":
        cat = "Entretenimiento"
    elif cn == "personal":
        cat = "Personal"
    elif cn == "regalo":
        cat = "Regalo"
    elif cn == "transporte":
        cat = "Transporte"

    # Subcategorías canónicas por sinónimos frecuentes
    if sn in ("restaurante", "restaurantes", "cena", "almuerzo", "desayuno"):
        sub = "Restaurantes"
    elif sn in ("mercado", "mercados", "super", "supermercado", "supermercados", "viveres", "viveres"):
        sub = "Mercado"
    elif sn in ("antojo", "antojitos", "postre", "postres", "helado", "snack", "dulces"):
        sub = "Antojo"
    elif sn in ("ropa", "vestimenta"):
        sub = "Ropa"
    elif sn in ("videojuego", "videojuegos", "gaming"):
        sub = "Videojuegos"
    elif sn in ("salud", "medicina", "medicamentos"):
        sub = "Salud"
    elif sn in ("familia negro", "familianegro"):
        sub = "Familia Negro"
    elif sn in ("donacion", "donaciones"):
        sub = "Donaciones"
    elif sn in ("familia",):
        sub = "Familia"
    elif sn in ("amigos", "amigo", "amiga"):
        sub = "Amigos"
    elif sn in ("plataforma", "plataformas", "uber", "didi", "indrive", "cabify"):
        sub = "Plataformas"
    elif sn in ("publico", "transporte publico", "taxi", "bus", "metro"):
        sub = "Público"
    elif sn in ("propio", "gasolina", "peaje", "parqueadero"):
        sub = "Propio"
    elif sn in ("otros", "otro"):
        sub = "Otros"

    return cat, sub

def pad_time(h: str) -> str:
    """Asegura formato HH:MM:SS para mejorar parseo en Sheets locales ES."""
    try:
        parts = (h or "").split(":")
        if len(parts) == 2:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        elif len(parts) == 3:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
        else:
            return h
    except Exception:
        return h

# === Parseo de JSON estricto desde la respuesta de GPT ===
def parse_json_strict(text):
    if not text:
        return None
    # Intento directo
    try:
        return json.loads(text)
    except Exception:
        pass
    # Buscar un objeto {...}
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
    except Exception:
        pass
    # Buscar una lista [...]
    try:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            arr = json.loads(text[start:end+1])
            return {"gastos": arr}
    except Exception:
        pass
    return None

# === Llamada a GPT: NO inferir fecha/hora; dejarlas vacías si no están en el texto ===
def call_gpt_extract(msg_text):
    system_prompt = (
        "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON con estas claves exactas: "
        "{'fecha','hora','valor','tienda','categoria','subcategoria','detalle'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- \"tienda\" es comercio/lugar o app/página web donde se hizo (Uber, DiDi, Rappi, iFood, Steam, etc.). "

        "- 'categoria/subcategoria' concisas ('comida/almuerzo', 'transporte/taxi', etc.). "
        "- 'detalle' es descripción breve. "
        "- No incluyas explicaciones ni comentarios, solo el JSON."
    )
    user_prompt = f'Texto: "{msg_text}"'

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0.1,
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":user_prompt}
        ]
    )
    txt = resp.choices[0].message.content.strip()
    return parse_json_strict(txt)

# === Extracción multi-gasto ===
def call_gpt_extract_many(msg_text):
    system_prompt = (
        "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON válido, sin texto adicional, con la forma: "
        "{\"gastos\": [ {\"fecha\":\"\",\"hora\":\"\",\"valor\":0,\"tienda\":\"\",\"categoria\":\"\",\"subcategoria\":\"\",\"detalle\":\"\"}, ... ]}. "
        "Reglas: "
        "- Identifica TODOS los gastos presentes en el texto (pueden venir separados por comas, 'y', punto y aparte, saltos de línea, etc.). "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente para un gasto, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' a 28500 (entero). "
        "- \"tienda\" es comercio/lugar o app/página web donde se hizo (Uber, DiDi, Rappi, iFood, Steam, etc.). "

        "- 'categoria/subcategoria' concisas ('comida/almuerzo', 'transporte/taxi', etc.). "
        "- 'detalle' es descripción breve. "
        "- Regla personalizada: si el texto dice 'se lo di a negro', 'a negro', 'se lo di a dani' o 'a dani' (sin importar mayúsculas/minúsculas), entonces para esos gastos fija 'categoria' = 'Negro' y selecciona 'subcategoria' según el contexto: 'restaurante' (comida/restaurante/almuerzo/cena/desayuno), 'mercado' (super/mercado/víveres/compras de comida), 'transporte' (taxi/uber/bus/gasolina/peaje), o 'otros' si no encaja en los anteriores. "
        "- Nunca combines gastos distintos en uno solo; crea un objeto por cada gasto."
    )
    # Override: versión sin 'plataforma' y usando 'tienda' también para apps/web
    system_prompt = (
        "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON válido, sin texto adicional, con la forma: "
        "{\"gastos\": [ {\"fecha\":\"\",\"hora\":\"\",\"valor\":0,\"tienda\":\"\",\"categoria\":\"\",\"subcategoria\":\"\",\"detalle\":\"\"}, ... ]}. "
        "Reglas: "
        "- Identifica TODOS los gastos presentes en el texto (separados por comas, 'y', punto y aparte, saltos de línea, etc.). "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente para un gasto, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' a 28500 (entero). "
        "- 'tienda' es comercio/lugar o app/página web donde se hizo (Uber, DiDi, Rappi, iFood, Steam, etc.). "
        "- 'categoria/subcategoria' concisas ('Comida/Restaurantes', 'Comida/Mercado', 'Transporte/Plataformas', etc.). "
        "- 'detalle' es descripción breve. "
        "- Regla personalizada: si el texto dice 'se lo di a negro', 'a negro', 'se lo di a dani' o 'a dani' (sin importar mayúsculas/minúsculas), entonces para esos gastos fija 'categoria' = 'Negro' y selecciona 'subcategoria' según el contexto: 'Restaurantes', 'Mercado', 'Transporte' u 'Otros'. "
        "- Nunca combines gastos distintos en uno solo; crea un objeto por cada gasto."
    )
    user_prompt = f'Texto: "{msg_text}"'

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0.1,
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":user_prompt}
        ]
    )
    txt = (resp.choices[0].message.content or "").strip()
    parsed = parse_json_strict(txt)
    return parsed

def _coerce_to_records(parsed):
    """Normaliza la salida parseada a una lista de registros dict."""
    if not parsed:
        return []
    if isinstance(parsed, dict):
        if isinstance(parsed.get("gastos"), list):
            return [x for x in parsed["gastos"] if isinstance(x, dict)]
        # ¿vino un solo registro plano?
        if any(k in parsed for k in ("valor","tienda","categoria","subcategoria","detalle")):
            return [parsed]
        return []
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    return []

# === Normalización: fecha/hora vacías o inválidas -> ahora; valor -> entero COP ===
def normalize_record(rec):
    now = dt.datetime.now(TZ)

    # valor -> entero
    val = rec.get("valor")
    if isinstance(val, str):
        v = re.sub(r"[^\d,\.]", "", val)
        v = v.replace(".", "").replace(",", ".")
        try:
            val = int(round(float(v)))
        except Exception:
            val = ""
    rec["valor"] = val

    # fecha/hora
    fecha = (rec.get("fecha") or "").strip()
    hora  = (rec.get("hora") or "").strip()
    if not is_valid_date(fecha):
        fecha = now.date().isoformat()
    if not is_valid_time(hora):
        hora = now.strftime("%H:%M")
    rec["fecha"] = fecha
    rec["hora"]  = pad_time(hora)

    # strings seguros y normalización de tienda/categoría/subcategoría
    plataforma = (rec.get("plataforma", "") or "").strip()
    tienda = (rec.get("tienda", "") or "").strip()
    if not tienda and plataforma:
        tienda = plataforma
    rec["tienda"] = to_title_case(tienda)

    rec["categoria"], rec["subcategoria"] = canonicalize_cat_sub(
        rec.get("categoria", ""), rec.get("subcategoria", "")
    )
    rec["detalle"] = (rec.get("detalle", "") or "").strip()

    # Normaliza categoría Negro
    if (rec.get("categoria") or "").strip().lower() == "negro":
        rec["categoria"] = "Negro"

    # Sinónimos y estandarización de categorías/subcategorías
    rec = apply_synonym_normalization(rec)
    # Reaplicar canónico tras posibles cambios
    rec["categoria"], rec["subcategoria"] = canonicalize_cat_sub(
        rec.get("categoria", ""), rec.get("subcategoria", "")
    )

    # Asegurar claves esperadas
    for k in ["fecha","hora","valor","tienda","categoria","subcategoria","detalle"]:
        rec.setdefault(k, "")

    return rec

def refine_classification(rec):
    """Ajustes finales: prioriza 'Regalo' por palabras clave salvo trigger explícito
    de 'a negro'/'a dani'. Evita crear subcategorías nuevas por variaciones.
    """
    try:
        text = f"{rec.get('categoria','')} {rec.get('subcategoria','')} {rec.get('tienda','')} {rec.get('detalle','')}".lower()
        negro_trigger = re.search(r"\b(se\s*lo\s*di\s*a\s*(negro|dani)|a\s*(negro|dani))\b", text)
        if (not negro_trigger) and ("regalo" in text or "donaci" in text or "familia" in text or "amigo" in text):
            rec["categoria"] = "Regalo"
            if "donaci" in text:
                rec["subcategoria"] = "Donaciones"
            elif "familia negro" in text:
                rec["subcategoria"] = "Familia Negro"
            elif "familia" in text:
                rec["subcategoria"] = "Familia"
            elif "amig" in text:
                rec["subcategoria"] = "Amigos"
            else:
                rec["subcategoria"] = "Otros"
    except Exception:
        pass

def ensure_basic_filter_range(ws):
    """Asegura que el filtro básico cubra A1:H10000 para que el estilo de tabla
    incluya nuevas filas. No rompe si no existe o falla.
    """
    try:
        sh = ws.spreadsheet
        rng = {
            "sheetId": ws.id,
            "startRowIndex": 0,
            "startColumnIndex": 0,
            "endRowIndex": max(10000, ws.row_count),
            "endColumnIndex": len(GASTOS_HEADERS),
        }
        sh.batch_update({
            "requests": [
                {"setBasicFilter": {"filter": {"range": rng}}}
            ]
        })
    except Exception:
        pass
    return rec

def apply_synonym_normalization(rec):
    """Mapea subcategorías y categorías a las existentes usando sinónimos.
    No borra lo existente; solo ajusta a nombres canónicos cuando aplica.
    """
    cat = (rec.get("categoria") or "").strip()
    sub = (rec.get("subcategoria") or "").strip()
    tienda = (rec.get("tienda") or "").strip()
    detalle = (rec.get("detalle") or "").strip()
    text = f"{cat} {sub} {tienda} {detalle}".lower()

    def has_any(words):
        return any(w in text for w in words)

    # Negro → subcategorías: Restaurantes, Mercado, Transporte, Otros
    if cat.lower() == "negro":
        if has_any(["super", "súper", "supermerc", "mercado", "víveres", "viveres", "despensa"]):
            sub = "Mercado"
        elif has_any(["uber", "didi", "indrive", "in drive", "cabify", "taxi", "bus", "buses", "metro", "gasolina", "peaje", "parqueadero"]):
            sub = "Transporte"
        elif has_any(["restaur", "comida", "almuerzo", "cena", "desayuno", "hamburg", "pizza", "pollo", "wok", "mcdonalds", "mc donalds", "kfc", "corral", "domino"]):
            sub = "Restaurantes"
        else:
            sub = "Otros"
        rec["subcategoria"] = sub
        rec["categoria"] = "Negro"
        return rec

    # Comida
    if cat.lower() in ("comida", "alimentacion", "alimentación") or has_any(["almuerzo", "cena", "desayuno", "restaur", "helado", "postre", "antojo", "mercado", "super", "supermerc"]):
        cat = "Comida"
        if sub.lower() == "cena":
            sub = "Restaurantes"
        elif has_any(["helado", "postre", "antojo", "snack", "dulce", "dulces", "percimon", "mimos"]):
            sub = "Antojo"
        elif has_any(["super", "súper", "supermerc", "mercado", "víveres", "viveres", "despensa"]):
            sub = "Mercado"
        else:
            sub = "Restaurantes"
        rec["categoria"], rec["subcategoria"] = cat, sub
        return rec

    # Transporte
    if cat.lower() == "transporte" or has_any(["uber", "didi", "indrive", "in drive", "cabify", "taxi", "bus", "buses", "metro", "gasolina", "peaje", "parqueadero"]):
        cat = "Transporte"
        if has_any(["uber", "didi", "indrive", "cabify"]):
            sub = "Plataformas"
        elif has_any(["taxi", "bus", "buses", "metro", "transmilenio"]):
            sub = "Público"
        elif has_any(["gasolina", "peaje", "parqueadero"]):
            sub = "Propio"
        rec["categoria"], rec["subcategoria"] = cat, sub or rec.get("subcategoria", "")
        return rec

    # Compras / Ropa
    if cat.lower() in ("compras",) or has_any(["ropa", "camisa", "pantal", "jean", "chaqueta", "zapato", "chevignon", "americanino"]):
        cat = "Compras"
        sub = "Ropa"
        rec["categoria"], rec["subcategoria"] = cat, sub
        return rec

    # Entretenimiento / Videojuegos
    if cat.lower() in ("entretenimiento",) or has_any(["steam", "xbox", "playstation", "psn", "nintendo", "epic", "videojuego"]):
        cat = "Entretenimiento"
        sub = "Videojuegos"
        rec["categoria"], rec["subcategoria"] = cat, sub
        return rec

    # Personal / Salud
    if cat.lower() in ("personal",) or has_any(["salud", "medicina", "medicamento", "medicamentos", "farmacia", "eps", "médico", "medico", "consulta"]):
        cat = "Personal"
        sub = "Salud"
        rec["categoria"], rec["subcategoria"] = cat, sub
        return rec

    # Regalo (si se detecta palabra regalo)
    if cat.lower() in ("regalo",) or has_any(["regalo", "donaci", "amigo", "familia"]):
        cat = "Regalo"
        if has_any(["donaci"]):
            sub = "Donaciones"
        elif has_any(["familia negro"]):
            sub = "Familia Negro"
        elif has_any(["familia"]):
            sub = "Familia"
        elif has_any(["amigo"]):
            sub = "Amigos"
        else:
            sub = "Otros"
        rec["categoria"], rec["subcategoria"] = cat, sub
        return rec

    return rec

# === Reglas de negocio personalizadas ===
def enforce_business_rules(rec):
    """
    Regla solicitada:
    - Si categoria es 'alimentación'/'alimentacion'/'comida' y la hora está entre 18:00 y 02:00,
      entonces subcategoria = 'cena' (forzado).
    """
    cat = (rec.get("categoria") or "").strip().lower()
    hora = (rec.get("hora") or "00:00").strip()

    try:
        hh = int(hora.split(":")[0])
    except Exception:
        hh = -1  # fuerza a no coincidir si hora inválida, aunque normalmente ya está normalizada

    if cat in ("alimentación", "alimentacion", "comida"):
        # Ventana 18:00–23:59 o 00:00–01:59 (cruza medianoche)
        if (hh >= 18) or (0 <= hh < 2):
            rec["subcategoria"] = "Restaurantes"

    return rec

def persist_to_gsheets(rec):
    ws = get_gastos_ws()
    ensure_named_range_gastos(ws)
    ensure_basic_filter_range(ws)
    # Obtener/crear ID de categoría
    cat_id = ensure_categoria_id(rec.get("categoria", ""), rec.get("subcategoria", ""))

    # Armar fila en el orden esperado por la hoja 'gastos_diarios'
    row = [
        rec.get("fecha", ""),
        pad_time(rec.get("hora", "")),
        rec.get("valor", ""),
        rec.get("tienda", ""),
        cat_id,
        "",  # categoria (la calcula la hoja por fórmula)
        "",  # subcategoria (la calcula la hoja por fórmula)
        rec.get("detalle", ""),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

# === Helpers de validación obligatoria ===
def has_required_description(rec) -> bool:
    return any(rec.get(k) for k in ("categoria", "subcategoria", "detalle"))

# === Telegram Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Soy tu bot de gastos.\n"
        "Obligatorio: 💰 valor y 📝 descripción (categoría/subcategoría/detalle).\n"
        "Regla: si la categoría es 'alimentación/comida' y el gasto es entre 18:00 y 02:00, subcategoría = 'cena'.\n"
        "Ejemplos:\n"
        "• 'Uber 7.820 a la oficina'\n"
        "• 'Almuerzo 28.500 en El Corral'\n"
        "• 'Comida 40.000 El Corral 20:30'\n"
        "Guardaré todo en tu Google Sheets 'gastos_diarios'."
    )
    await update.message.reply_text("Tambien recibo audio (es/en) y lo transcribo.")
    await update.message.reply_text("También podés enviar varios gastos en un solo mensaje o audio y los separo automáticamente.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        rec = call_gpt_extract(text)
        if not rec:
            await update.message.reply_text("😅 No pude entender el gasto. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
            return

        rec = normalize_record(rec)

        # Validación obligatoria
        if not rec["valor"]:
            await update.message.reply_text("💰 Me falta el valor del gasto. Enviame el monto (ej: 25000 o 28.500).")
            return
        if not has_required_description(rec):
            await update.message.reply_text("📝 Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.")
            return

        # Reglas de negocio
        rec = enforce_business_rules(rec)

        # Guardar
        persist_to_gsheets(rec)

        await update.message.reply_text(
            f"✅ Guardado: {rec['categoria']} / {rec['subcategoria']} | ${rec['valor']} | {rec['fecha']} {rec['hora']}"
            + (f" | {rec['plataforma']}" if rec.get('plataforma') else "")
            + (f" | {rec['tienda']}" if rec.get('tienda') else "")
        )

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_text_multi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parsed = call_gpt_extract_many(text)
        records = _coerce_to_records(parsed)
        if not records:
            await update.message.reply_text("No pude entender el/los gasto(s). Decime el/los monto(s) y una descripción corta (ej: 'comida almuerzo 28000 y café 5000').")
            return

        saved, skipped = [], []
        for rec in records:
            rec = normalize_record(rec)
            if not rec["valor"]:
                skipped.append((rec, "Falta el valor"))
                continue
            if not has_required_description(rec):
                skipped.append((rec, "Falta descripción/categoría"))
                continue
            rec = enforce_business_rules(rec)
            rec = refine_classification(rec)
            try:
                persist_to_gsheets(rec)
                saved.append(rec)
            except Exception as e:
                try:
                    print("Persist error:\n" + traceback.format_exc())
                except Exception:
                    pass
                skipped.append((rec, f"Error guardando: {e}"))

        if not saved:
            await update.message.reply_text("No se guardó ningún gasto. " + ("; ".join(reason for _, reason in skipped) if skipped else ""))
            return

        lines = [
            f"Guardados: {len(saved)} gasto(s)" + (f" | Omitidos: {len(skipped)}" if skipped else "")
        ]
        for i, r in enumerate(saved, 1):
            extra_s = f" | {r['tienda']}" if r.get('tienda') else ""
            lines.append(f"{i}. {r['categoria']} / {r['subcategoria']} | ${r['valor']} | {r['fecha']} {r['hora']}{extra_s}")
        if skipped:
            lines.append("— Omitidos:")
            for _, reason in skipped:
                lines.append(f"• {reason}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def start2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola 👋 Soy tu bot de gastos.\n"
        "- Obligatorio: monto y descripción (categoría/subcategoría/detalle).\n"
        "- Soporto varios gastos en un solo mensaje o audio.\n"
        "- Regla cena: si la categoría es 'alimentación/comida' y es entre 18:00 y 02:00, subcategoría = 'cena'.\n"
        "- Regla Negro/Dani: si dices 'se lo di a negro' o 'a dani', la categoría = 'Negro' y la subcategoría según el caso: 'restaurante', 'mercado', 'transporte' u 'otros'.\n"
        "Ejemplos:\n"
        "• 'Almuerzo 28.500 en El Corral'\n"
        "• 'Café 5.000 y taxi 12.000'\n"
        "• 'Le di a negro 20.000 para taxi' (Negro / transporte)\n"
        "Guardaré todo en tu Google Sheets 'gastos_diarios'."
    )

# === Descarga y transcripción de audio ===
async def _download_telegram_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Descarga voice/audio a un archivo temporal y devuelve su ruta."""
    message = update.message
    if not message:
        raise RuntimeError("No message found")

    file_id = None
    suffix = ".ogg"
    if message.voice:
        file_id = message.voice.file_id
        suffix = ".ogg"
    elif message.audio:
        file_id = message.audio.file_id
        if message.audio.file_name and "." in message.audio.file_name:
            suffix = os.path.splitext(message.audio.file_name)[1] or ".mp3"
        else:
            suffix = ".mp3"
    else:
        raise RuntimeError("Tipo de mensaje no soportado para audio")

    tg_file = await context.bot.get_file(file_id)
    fd, tmp_path = tempfile.mkstemp(prefix="tg_audio_", suffix=suffix)
    os.close(fd)
    await tg_file.download_to_drive(tmp_path)
    return tmp_path

def _transcribe_with_openai(path: str) -> str:
    model = (OPENAI_TRANSCRIBE_MODEL or "gpt-4o-mini-transcribe").strip()
    try:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model,
                file=f,
                temperature=0,
            )
        text = getattr(resp, "text", None)
        if not text:
            text = str(resp)
        return (text or "").strip()
    except Exception as e:
        if model != "whisper-1":
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    temperature=0,
                )
            text = getattr(resp, "text", None) or str(resp)
            return (text or "").strip()
        raise e

def _transcribe_with_faster_whisper(path: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        raise RuntimeError("Para usar faster-whisper instala 'faster-whisper' y asegúrate de tener ffmpeg en el sistema.") from e

    model = WhisperModel(FAST_WHISPER_MODEL, device=FAST_WHISPER_DEVICE, compute_type=FAST_WHISPER_COMPUTE)
    segments, info = model.transcribe(
        path,
        vad_filter=True,
        without_timestamps=True,
        beam_size=5,
    )
    text_parts = [seg.text for seg in segments]
    return (" ".join(text_parts)).strip()

def transcribe_audio_file(path: str) -> str:
    engine = TRANSCRIBE_ENGINE
    if engine == "openai":
        return _transcribe_with_openai(path)
    elif engine in ("faster-whisper", "faster_whisper", "fwhisper"):
        return _transcribe_with_faster_whisper(path)
    else:
        return _transcribe_with_openai(path)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tmp_path = await _download_telegram_media(update, context)
        try:
            transcript = transcribe_audio_file(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if not transcript:
            await update.message.reply_text("No pude transcribir el audio ??. ¿Podés intentar de nuevo o mandar texto?")
            return

        rec = call_gpt_extract(transcript)
        if not rec:
            await update.message.reply_text("?? No entendí el gasto del audio. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
            return

        rec = normalize_record(rec)

        if not rec["valor"]:
            await update.message.reply_text("?? Me falta el valor del gasto. Enviame el monto (ej: 25000 o 28.500).\nTranscripción: " + transcript[:500])
            return
        if not has_required_description(rec):
            await update.message.reply_text("?? Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.\nTranscripción: " + transcript[:500])
            return

        rec = enforce_business_rules(rec)
        persist_to_gsheets(rec)

        await update.message.reply_text(
            f"? Guardado (audio): {rec['categoria']} / {rec['subcategoria']} | ${rec['valor']} | {rec['fecha']} {rec['hora']}"
            + (f" | {rec['plataforma']}" if rec.get('plataforma') else "")
            + (f" | {rec['tienda']}" if rec.get('tienda') else "")
        )
    except Exception as e:
        await update.message.reply_text(f"Error transcribiendo audio: {e}")

async def handle_audio_multi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tmp_path = await _download_telegram_media(update, context)
        try:
            transcript = transcribe_audio_file(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if not transcript:
            await update.message.reply_text("No pude transcribir el audio. ¿Podés intentar de nuevo o mandar texto?")
            return

        parsed = call_gpt_extract_many(transcript)
        records = _coerce_to_records(parsed)
        if not records:
            await update.message.reply_text("No entendí gastos en el audio. Decime el/los monto(s) y una descripción corta (ej: 'comida almuerzo 28000 y café 5000').\nTranscripción: " + transcript[:500])
            return

        saved, skipped = [], []
        for rec in records:
            rec = normalize_record(rec)
            if not rec["valor"]:
                skipped.append((rec, "Falta el valor"))
                continue
            if not has_required_description(rec):
                skipped.append((rec, "Falta descripción/categoría"))
                continue
            rec = enforce_business_rules(rec)
            rec = refine_classification(rec)
            try:
                persist_to_gsheets(rec)
                saved.append(rec)
            except Exception as e:
                try:
                    print("Persist error (audio):\n" + traceback.format_exc())
                except Exception:
                    pass
                skipped.append((rec, f"Error guardando: {e}"))

        if not saved:
            await update.message.reply_text("No se guardó ningún gasto del audio. " + ("; ".join(reason for _, reason in skipped) if skipped else "") + "\nTranscripción: " + transcript[:500])
            return

        lines = [
            f"Guardados (audio): {len(saved)} gasto(s)" + (f" | Omitidos: {len(skipped)}" if skipped else "")
        ]
        for i, r in enumerate(saved, 1):
            extra_s = f" | {r['tienda']}" if r.get('tienda') else ""
            lines.append(f"{i}. {r['categoria']} / {r['subcategoria']} | ${r['valor']} | {r['fecha']} {r['hora']}{extra_s}")
        if skipped:
            lines.append("— Omitidos:")
            for _, reason in skipped:
                lines.append(f"• {reason}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error transcribiendo audio: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start2))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_multi))
    # Soporte de mensajes de audio y voice
    app.add_handler(MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_audio_multi))
    app.run_polling()

if __name__ == "__main__":
    main()

