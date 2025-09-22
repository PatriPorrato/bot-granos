import os, sys, csv, io, traceback
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import tweepy

# =========================
# Config
# =========================
RET = {"soja": 0.33, "maiz": 0.12, "trigo": 0.12, "girasol": 0.07}
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) bot-granos/1.0 (+github actions)"
}

# =========================
# Utils
# =========================
def gross_up(precio, ret):
    try:
        return round(precio / (1 - ret), 0) if precio else None
    except Exception:
        return None

def fmt_ars(v, dec=0):
    if v is None:
        return "S/C"
    s = f"{float(v):,.{dec}f}"
    # 1.234.567,89 formato AR
    return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_usd(v, dec=2):
    if v is None:
        return "S/C"
    return f"USD{float(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")

def get(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

# =========================
# 1) Precios nacionales (BCR)
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
        # Tomamos la primera tabla con filas relevantes
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
BU_TON = {"soja": 36.7437, "maiz": 39.3680, "trigo": 36.7437}
SYMB   = {"soja": "zs.f",   "maiz": "zc.f",   "trigo": "zw.f"}

def stooq_last_close(symbol):
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        txt = get(url, timeout=30).text
        reader = csv.DictReader(io.StringIO(txt))
        rows = list(reader)
        if not rows:
            return None, url
        close_cents = float(rows[-1]["Close"])
        return close_cents / 100.0, url  # USD/bu
    except Exception as e:
        print(f"WARN_STOOQ_{symbol}: {e}")
        return None, url

def chicago_usd_ton():
    out, src = {}, {}
    for k, s in SYMB.items():
        usd_bu, url = stooq_last_close(s)
        src[k] = url
        if usd_bu is not None:
            out[k] = round(usd_bu * BU_TON[k], 2)
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
    ar, src_bcr     = precios_bcr()
    cb, src_cbot    = chicago_usd_ton()
    fx_of, src_of   = dolarapi("oficial")
    fx_mep, src_mep = dolarapi("mep")

    sin_ret = {k: gross_up(ar.get(k), RET[k]) for k in RET.keys()}

    linea_ar = []
    for nice, key in [("Soja","soja"), ("Maíz","maiz"), ("Girasol","girasol"), ("Trigo","trigo")]:
        p = ar.get(key); sr = sin_ret.get(key)
        linea_ar.append(f"{nice}: {fmt_ars(p)} (sin ret {fmt_ars(sr)})")

    linea_cbot = f"Soja: {fmt_usd(cb.get('soja'))} | Maíz: {fmt_usd(cb.get('maiz'))} | Trigo: {fmt_usd(cb.get('trigo'))}"

    linea_tc = []
    if fx_of is not None: linea_tc.append(f"Oficial: {fmt_ars(fx_of, 2)}")
    if fx_mep is not None: linea_tc.append(f"MEP: {fmt_ars(fx_mep, 2)}")
    if not linea_tc: linea_tc.append("TC: S/C")

    hoy = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")

    msg = (
        "📊 Precios 🇦🇷 y Chicago\n"
        f"• {' | '.join(linea_ar)}\n"
        "—\n"
        f"CBOT (USD/t): {linea_cbot}\n"
        f"TC: {' | '.join(linea_tc)}\n"
        f"({hoy}) #Agro #Granos #Soja #Maíz #Trigo #Girasol"
    )

    # recorte defensivo
    if len(msg) > 280:
        msg = msg[:279] + "…"

    fuentes = {
        "BCR": src_bcr,
        "CBOT": src_cbot,
        "TC": {"oficial": src_of, "mep": src_mep}
    }
    return msg, fuentes

# =========================
# X (Twitter)
# =========================
def post_to_x(text):
    key = os.getenv("X_API_KEY")
    secret = os.getenv("X_API_SECRET")
    token = os.getenv("X_ACCESS_TOKEN")
    token_secret = os.getenv("X_ACCESS_SECRET")

    missing = [n for n,v in {
        "X_API_KEY":key, "X_API_SECRET":secret, "X_ACCESS_TOKEN":token, "X_ACCESS_SECRET":token_secret
    }.items() if not v]
    if missing:
        print("ERROR_SECRETS_FALTAN:", ", ".join(missing))
        return False, "Faltan secrets de X (ver Settings > Secrets > Actions)."

    auth = tweepy.OAuth1UserHandler(key, secret, token, token_secret)
    api = tweepy.API(auth)

    try:
        api.verify_credentials()
    except Exception as e:
        return False, f"ERROR_TWITTER_VERIFY: {e}"

    try:
        api.update_status(status=text)
        return True, "OK"
    except Exception as e:
        return False, f"ERROR_TWITTER_POST: {e}"

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
        # Dejar trazas para depurar
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
