import os, sys, csv, io, traceback
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import tweepy

# =========================
# Config
# =========================
# Retenciones por defecto (editables con env vars del workflow si querés)
RET = {
    "soja":   float(os.getenv("RET_SOJA", "0.33")),
    "maiz":   float(os.getenv("RET_MAIZ", "0.12")),
    "trigo":  float(os.getenv("RET_TRIGO", "0.12")),
    "girasol":float(os.getenv("RET_GIRASOL","0.07")),
}

# Mostrar estos TC (separados por coma). Soporta: oficial, mep, blue, ccl, mayorista, etc.
SHOW_DOLLARS = [s.strip() for s in os.getenv("SHOW_DOLLARS", "oficial,mep").split(",") if s.strip()]

# DRY_RUN=1 imprime el tweet y NO postea (úsalo en la primera corrida)
DRY_RUN = os.getenv("DRY_RUN", "0") == "0"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) bot-granos/1.0 (+github actions)"}

# =========================
# Utils
# =========================
def gross_up(precio, ret):
    try:
        return round(precio / (1 - ret), 0) if (precio is not None) else None
    except Exception:
        return None

def fmt_ars(v, dec=0):
    if v is None:
        return "S/C"
    s = f"{float(v):,.{dec}f}"
    # Formato AR: 1.234.567,89
    return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_usd(v, dec=2):
    if v is None:
        return "S/C"
    s = f"{float(v):,.{dec}f}"
    return "USD" + s.replace(",", "X").replace(".", ",").replace("X", ".")

def get(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

# =========================
# 1) Precios nacionales (BCR, ARS/t)
# =========================
def parse_ars(texto):
    # "$ 281.300,00" -> 281300.00
    s = (texto or "").replace("$", "").replace("\xa0", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def precios_bcr():
    url = "https://www.bcr.com.ar/es/mercados/mercado-de-granos/cotizaciones/cotizaciones-locales-0"
    data = {"soja": None, "maiz": None, "trigo": None, "girasol": None}
    try:
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            nombre = tds[0].get_text(strip=True).lower()
            val = parse_ars(tds[1].get_text(strip=True))
            if val is None:
                continue
            if "soja" in nombre:
                data["soja"] = val
            elif "maíz" in nombre or "maiz" in nombre:
                data["maiz"] = val
            elif "trigo" in nombre:
                data["trigo"] = val
            elif "girasol" in nombre:
                data["girasol"] = val
    except Exception as e:
        print(f"WARN_BCR: {e}")
    return data, url

# =========================
# 2) Chicago (USD/t) via Stooq (CSV)
# =========================
BU_TON = {"soja": 36.7437, "maiz": 39.3680, "trigo": 36.7437}  # bu -> t
SYMB   = {"soja": "zs.f",   "maiz": "zc.f",   "trigo": "zw.f"} # Stooq symbols

def stooq_last_close(symbol):
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        txt = get(url, timeout=30).text
        rows = list(csv.DictReader(io.StringIO(txt)))
        if not rows:
            return None, url
        close_cents = float(rows[-1]["Close"])  # ¢/bu
        return close_cents / 100.0, url         # USD/bu
    except Exception as e:
        print(f"WARN_STOOQ_{symbol}: {e}")
        return None, url

def chicago_usd_ton():
    out, src = {}, {}
    for k, s in SYMB.items():
        usd_bu, url = stooq_last_close(s)
        src[k] = url
        if usd_bu is not None:
            out[k] = round(usd_bu * BU_TON[k], 2)  # USD/t
    return out, src

# =========================
# 3) Tipo de cambio (DolarApi)
# =========================
def dolarapi(kind="oficial"):
    url = f"https://dolarapi.com/v1/dolares/{kind}"
    try:
        js = get(url, timeout=20).json()
        v = js.get("venta") or js.get("compra")
        return (float(v) if v is not None else None), url
    except Exception as e:
        print(f"WARN_DOLAR_{kind}: {e}")
        return None, url

# =========================
# Build tweet
# =========================
def build_tweet():
    ar, src_bcr   = precios_bcr()
    cb, src_cbot  = chicago_usd_ton()
    tc_vals, tc_srcs = [], {}

    for k in SHOW_DOLLARS:
        val, url = dolarapi(k)
        tc_srcs[k] = url
        label = {"oficial":"Oficial","mep":"MEP","blue":"Blue","ccl":"CCL"}.get(k, k.upper())
        tc_vals.append(f"{label}: {fmt_ars(val, 2)}" if val is not None else f"{label}: S/C")

    sin_ret = {k: gross_up(ar.get(k), RET[k]) for k in RET}

    linea_ar = []
    for nice, key in [("Soja","soja"), ("Maíz","maiz"), ("Girasol","girasol"), ("Trigo","trigo")]:
        p = ar.get(key); sr = sin_ret.get(key)
        linea_ar.append(f"{nice}: {fmt_ars(p)} (sin ret {fmt_ars(sr)})")

    linea_cbot = f"Soja: {fmt_usd(cb.get('soja'))} | Maíz: {fmt_usd(cb.get('maiz'))} | Trigo: {fmt_usd(cb.get('trigo'))}"

    hoy = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")
    msg = (
        "📊 Precios 🇦🇷 y Chicago\n"
        f"• {' | '.join(linea_ar)}\n"
        "—\n"
        f"CBOT (USD/t): {linea_cbot}\n"
        f"TC: {' | '.join(tc_vals)}\n"
        f"({hoy}) #Agro #Granos #Soja #Maíz #Trigo #Girasol"
    )

    if len(msg) > 280:
        msg = msg[:279] + "…"

    fuentes = {"BCR": src_bcr, "CBOT": src_cbot, "TC": tc_srcs}
    return msg, fuentes

# =========================
# X (Twitter)
# =========================
def post_to_x(text):
    # X API v2 (tweepy.Client) con OAuth 1.0a user context
    key = os.getenv("X_API_KEY")
    secret = os.getenv("X_API_SECRET")
    token = os.getenv("X_ACCESS_TOKEN")
    token_secret = os.getenv("X_ACCESS_SECRET")

    missing = [n for n,v in {
        "X_API_KEY":key, "X_API_SECRET":secret, "X_ACCESS_TOKEN":token, "X_ACCESS_SECRET":token_secret
    }.items() if not v]
    if missing:
        print("ERROR_SECRETS_FALTAN:", ", ".join(missing))
        return False, "Faltan secrets de X."

    try:
        client = tweepy.Client(
            consumer_key=key,
            consumer_secret=secret,
            access_token=token,
            access_token_secret=token_secret
        )
        resp = client.create_tweet(text=text)
        print("X_V2_RESP:", resp)
        return True, "OK"
    except Exception as e:
        # Pista típica: 403/453 = falta permiso de escritura o tier incompatible
        return False, f"ERROR_TWITTER_V2_POST: {e}"


def main():
    tweet, fuentes = build_tweet()

    print("---- PREVIEW ----")
    print(tweet)
    print("\n---- FUENTES ----")
    print(fuentes)

    if DRY_RUN:
        print("\nDRY_RUN=1 => No publica (solo preview).")
        return

    ok, info = post_to_x(tweet)
    if not ok:
        print(info)
        sys.exit(1)
    else:
        print("Publicado correctamente.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR_FATAL:", e)
        traceback.print_exc()
        sys.exit(1)
