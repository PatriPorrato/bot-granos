import os
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import tweepy
import traceback

# ----- Config -----
RET = {"soja": 0.33, "maiz": 0.12, "trigo": 0.12, "girasol": 0.07}
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

def gross_up(precio, ret):
    try:
        return round(precio / (1 - ret), 0) if precio else None
    except Exception:
        return None

def fmt(v, unit="$", dec=0):
    try:
        if v is None:
            return "S/C"
        return f"{unit}{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "S/C"

# ----- Nacionales (ARS/t) -----
def precios_bcr():
    url = "https://www.bcr.com.ar/es/mercados/mercado-de-granos/cotizaciones/cotizaciones-locales-0"
    try:
        html = requests.get(url, timeout=25).text
        soup = BeautifulSoup(html, "html.parser")
        data = {"soja": None, "maiz": None, "trigo": None, "girasol": None}

        def parse_val(s):
            s = s.replace("$", "").replace(".", "").replace(",", "").strip()
            return float(s) if s.isdigit() else None

        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 2:
                continue
            val = parse_val(tds[1])
            nombre = tds[0].lower()
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
        return data, url
    except Exception as e:
        print("WARN: precios_bcr falló:", e)
        return {"soja": None, "maiz": None, "trigo": None, "girasol": None}, url

# ----- Chicago (USD/t) vía Stooq -----
BU_TON = {"soja": 36.74, "maiz": 39.37, "trigo": 36.74}
SYMB = {"soja": "zs.f", "maiz": "zc.f", "trigo": "zw.f"}

def chicago():
    out, src = {}, {}
    for k, s in SYMB.items():
        url = f"https://stooq.com/q/d/l/?s={s}&i=d"
        try:
            df = pd.read_csv(url)
            if df.empty:
                print(f"WARN: stooq vacío para {k}")
                continue
            close = float(df.iloc[-1]["Close"]) / 100.0  # ¢/bu -> USD/bu
            out[k] = round(close * BU_TON[k], 2)         # USD/t
            src[k] = url
        except Exception as e:
            print(f"WARN: stooq {k} falló:", e)
            src[k] = url + " (error)"
    return out, src

# ----- Tipo de cambio -----
def dolar(tipo="oficial"):
    url = f"https://dolarapi.com/v1/dolares/{tipo}"
    try:
        js = requests.get(url, timeout=20).json()
        return float(js.get("venta") or js.get("compra")), url
    except Exception as e:
        print(f"WARN: dolar {tipo} falló:", e)
        return None, url + " (error)"

def build_tweet():
    ar, src_bcr = precios_bcr()
    cb, src_cbot = chicago()
    fx_of, src_of = dolar("oficial")
    fx_mep, src_mep = dolar("mep")

    sin_ret = {k: gross_up(ar.get(k), RET[k]) for k in RET}

    linea_ar = []
    for nice, key in [("Soja", "soja"), ("Maíz", "maiz"), ("Girasol", "girasol"), ("Trigo", "trigo")]:
        p = ar.get(key); sr = sin_ret.get(key)
        linea_ar.append(f"{nice}: {fmt(p)} (sin ret {fmt(sr)})")

    linea_cb = [
        f"Soja: USD{cb.get('soja','S/C')} | Maíz: USD{cb.get('maiz','S/C')} | Trigo: USD{cb.get('trigo','S/C')}"
    ]

    linea_tc = []
    if fx_of is not None: linea_tc.append(f"Oficial: {fmt(fx_of)}")
    if fx_mep is not None: linea_tc.append(f"MEP: {fmt(fx_mep)}")

    hoy = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")

    msg = "📊 Precios 🇦🇷 y Chicago\n"
    msg += "• " + " | ".join(linea_ar) + "\n"
    msg += "—\nCBOT (USD/t): " + " ".join(linea_cb) + "\n"
    msg += "TC: " + " | ".join(linea_tc) + "\n"
    msg += f"({hoy}) #Agro #Granos"

    # Recorte defensivo
    return msg[:280], {"BCR": src_bcr, "CBOT": src_cbot, "TC": {"oficial": src_of, "mep": src_mep}}

def main():
    try:
        tweet, fuentes = build_tweet()
        print("---- PREVIEW ----\n" + tweet)
        print("\n---- FUENTES ----")
        for k, v in fuentes.items():
            print(k, ":", v)

        if DRY_RUN:
            print("\nDRY_RUN=1 → No publica.")
            return

        api = tweepy.API(tweepy.OAuth1UserHandler(
            os.getenv("X_API_KEY"),
            os.getenv("X_API_SECRET"),
            os.getenv("X_ACCESS_TOKEN"),
            os.getenv("X_ACCESS_SECRET")
        ))
        api.verify_credentials()
        api.update_status(tweet)
        print("\nOK: Tweet publicado.")
    except Exception as e:
        print("\nERROR:", e)
        traceback.print_exc()
        raise  # deja exit code 1 para que GitHub Actions lo marque

if __name__ == "__main__":
    main()
