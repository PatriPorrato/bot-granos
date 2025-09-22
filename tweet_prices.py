import os, requests, pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import tweepy

# --- Retenciones ---
RET = {"soja":0.33, "maiz":0.12, "trigo":0.12, "girasol":0.07}

def gross_up(precio, ret):
    return round(precio/(1-ret),0) if precio else None

def fmt(v, unit="$", dec=0):
    if not v: return "S/C"
    return f"{unit}{v:,.{dec}f}".replace(",",".").replace(".",",",1)

# --- Nacionales (ARS/t) ---
def precios_bcr():
    url = "https://www.bcr.com.ar/es/mercados/mercado-de-granos/cotizaciones/cotizaciones-locales-0"
    html = requests.get(url, timeout=20).text
    soup = BeautifulSoup(html,"html.parser")
    data = {"soja":None,"maiz":None,"trigo":None,"girasol":None}
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds)<2: continue
        val = tds[1].replace("$","").replace(".","").replace(",","").strip()
        try: val=float(val)
        except: continue
        n = tds[0].lower()
        if "soja" in n: data["soja"]=val
        elif "maiz" in n or "maíz" in n: data["maiz"]=val
        elif "trigo" in n: data["trigo"]=val
        elif "girasol" in n: data["girasol"]=val
    return data

# --- Chicago (USD/t) vía Stooq ---
BU_TON = {"soja":36.74,"maiz":39.37,"trigo":36.74}
SYMB   = {"soja":"zs.f","maiz":"zc.f","trigo":"zw.f"}

def chicago():
    out={}
    for k,s in SYMB.items():
        url=f"https://stooq.com/q/d/l/?s={s}&i=d"
        df=pd.read_csv(url)
        if df.empty: continue
        close=float(df.iloc[-1]["Close"])/100
        out[k]=round(close*BU_TON[k],2)
    return out

# --- TC ---
def dolar(tipo="oficial"):
    url=f"https://dolarapi.com/v1/dolares/{tipo}"
    js=requests.get(url).json()
    return float(js.get("venta"))

def build_tweet():
    ar = precios_bcr()
    cb = chicago()
    fx = {"Oficial":dolar("oficial"), "MEP":dolar("mep")}

    linea_ar=[]
    for nice,k in [("Soja","soja"),("Maíz","maiz"),("Girasol","girasol"),("Trigo","trigo")]:
        p=ar.get(k); sr=gross_up(p,RET[k])
        linea_ar.append(f"{nice}: {fmt(p)} (sin ret {fmt(sr)})")

    linea_cb = [f"Soja: USD{cb.get('soja','S/C')} | Maíz: USD{cb.get('maiz','S/C')} | Trigo: USD{cb.get('trigo','S/C')}"]

    linea_tc = [f"{k}: {fmt(v)}" for k,v in fx.items()]

    hoy = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")

    msg = "📊 Precios 🇦🇷 y Chicago\n"
    msg += "• " + " | ".join(linea_ar) + "\n"
    msg += "—\nCBOT (USD/t): " + " ".join(linea_cb) + "\n"
    msg += "TC: " + " | ".join(linea_tc) + "\n"
    msg += f"({hoy}) #Agro #Granos"

    return msg[:280]

def main():
    tweet=build_tweet()
    print("Tweet:\n",tweet)
    api=tweepy.API(tweepy.OAuth1UserHandler(
        os.getenv("X_API_KEY"),os.getenv("X_API_SECRET"),
        os.getenv("X_ACCESS_TOKEN"),os.getenv("X_ACCESS_SECRET")
    ))
    api.update_status(tweet)

if __name__=="__main__":
    main()
