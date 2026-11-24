#!/usr/bin/env python3
"""
Cazador de precios bajos — tiendas oficiales VTEX en Colombia.

Modos:
  normal       Revisa catálogos, actualiza historial y envía alertas por Telegram.
  diagnostico  Igual que normal, pero además envía un reporte de qué tiendas respondieron.
  resumen      Igual que normal + resumen diario ("sigo vivo") por Telegram.
  prueba       Solo envía un mensaje de prueba a Telegram.
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
HIST_FILE = DATA / "historial.json"
ALERT_FILE = DATA / "alertas_enviadas.json"
STATS_FILE = DATA / "estadisticas.json"
TZ_BOG = timezone(timedelta(hours=-5))
MAX_OBS = 20            # observaciones de precio guardadas por producto
DIAS_PURGA = 30         # olvidar productos no vistos en N días

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36 cazador-precios-personal",
    "Accept": "application/json",
    "Accept-Language": "es-CO,es;q=0.9",
}


# ------------------------------------------------------------------ utilidades
def ahora():
    return datetime.now(timezone.utc)


def cargar_json(path, defecto):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defecto


def guardar_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def cop(v):
    return "$" + f"{v:,.0f}".replace(",", ".")


def normalizar(t):
    t = t.lower()
    for a, b in zip("áéíóúñ", "aeioun"):
        t = t.replace(a, b)
    return t


# ------------------------------------------------------------------ Telegram
def telegram(texto):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] Sin credenciales; mensaje no enviado:\n" + texto)
        return False
    ok = True
    # Telegram limita a 4096 caracteres por mensaje
    trozos, actual = [], ""
    for bloque in texto.split("\n\n"):
        if len(actual) + len(bloque) + 2 > 3900:
            trozos.append(actual)
            actual = ""
        actual += bloque + "\n\n"
    if actual.strip():
        trozos.append(actual)
    for t in trozos:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": t, "parse_mode": "HTML",
                      "disable_web_page_preview": False},
                timeout=20,
            )
            if r.status_code != 200:
                ok = False
                print(f"[telegram] Error {r.status_code}: {r.text[:300]}")
        except Exception as e:
            ok = False
            print(f"[telegram] Excepción: {e}")
    return ok


# ------------------------------------------------------------------ VTEX
def buscar_vtex(dominio, termino, orden, pagina, pausa):
    desde = pagina * 50
    url = f"https://{dominio}/api/catalog_system/pub/products/search"
    params = {"ft": termino, "_from": desde, "_to": desde + 49, "O": orden}
    time.sleep(pausa)
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"HTTP {r.status_code}")
    datos = r.json()
    if not isinstance(datos, list):
        raise RuntimeError("respuesta inesperada")
    return datos


def extraer_ofertas(productos, tienda, dominio):
    """Convierte productos VTEX en ofertas planas (una por ítem/SKU con stock)."""
    salida = []
    for p in productos:
        nombre = p.get("productName") or ""
        link = p.get("link") or ""
        if link.startswith("/"):
            link = f"https://{dominio}{link}"
        for it in p.get("items") or []:
            mejor = None
            for s in it.get("sellers") or []:
                of = s.get("commertialOffer") or {}
                precio = of.get("Price") or 0
                disp = of.get("AvailableQuantity") or 0
                if precio <= 0 or disp <= 0:
                    continue
                if mejor is None or precio < mejor["precio"]:
                    mejor = {
                        "precio": float(precio),
                        "antes": float(of.get("ListPrice") or of.get("PriceWithoutDiscount") or precio),
                        "stock": int(disp),
                        "vendedor": s.get("sellerName") or "",
                    }
            if not mejor:
                continue
            salida.append({
                "clave": f"{dominio}:{it.get('itemId')}",
                "tienda": tienda,
                "nombre": nombre,
                "marca": p.get("brand") or "",
                "ean": (it.get("ean") or "").strip(),
                "link": link,
                **mejor,
            })
    return salida


# ------------------------------------------------------------------ Falabella (experimental)
def numero(v):
    if isinstance(v, list):
        v = v[0] if v else None
    s = re.sub(r"[^\d]", "", str(v or ""))
    return float(s) if s else 0.0


def buscar_falabella(dominio, termino, pagina, pausa):
    time.sleep(pausa)
    r = requests.get(f"https://{dominio}/s/browse/v1/search/co",
                     params={"Ntt": termino, "page": pagina + 1}, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return ((r.json() or {}).get("data") or {}).get("results") or []


def extraer_falabella(resultados, t):
    permitidos = [normalizar(x) for x in (t.get("solo_vendido_por") or [])]
    salida = []
    for r in resultados:
        vendedor = r.get("sellerName") or ""
        if permitidos and not any(p in normalizar(vendedor) for p in permitidos):
            continue
        precios = r.get("prices") or []
        actuales = [numero(p.get("price")) for p in precios
                    if not p.get("crossed") and p.get("type") != "cmrPrice"]
        tachados = [numero(p.get("price")) for p in precios if p.get("crossed")]
        cmr = [numero(p.get("price")) for p in precios if p.get("type") == "cmrPrice"]
        actuales = [x for x in actuales if x > 0]
        if not actuales:
            continue
        precio = min(actuales)
        antes = max([x for x in tachados if x > 0] or [precio])
        url = r.get("url") or ""
        if url.startswith("/"):
            url = f"https://{t['dominio']}{url}"
        nombre = r.get("displayName") or ""
        if cmr and min(cmr) > 0 and min(cmr) < precio:
            nombre += f" (con tarjeta CMR: {cop(min(cmr))})"
        salida.append({
            "clave": f"falabella:{r.get('skuId') or r.get('productId')}",
            "tienda": t["nombre"], "nombre": nombre, "marca": r.get("brand") or "",
            "ean": "", "link": url, "precio": precio, "antes": antes,
            "stock": "?", "vendedor": vendedor,
        })
    return salida


# ------------------------------------------------------------------ Mercado Libre (tiendas oficiales)
ML_TOKEN_FILE = DATA / "ml_token.enc"
_ml_cache = {}


def _fernet():
    from cryptography.fernet import Fernet
    secreto = os.getenv("ML_CLIENT_SECRET", "")
    clave = base64.urlsafe_b64encode(hashlib.sha256(("cazador:" + secreto).encode()).digest())
    return Fernet(clave)


def _ml_guardar_refresh(rt):
    DATA.mkdir(parents=True, exist_ok=True)
    ML_TOKEN_FILE.write_bytes(_fernet().encrypt(rt.encode()))


def _ml_pedir_token(datos):
    r = requests.post("https://api.mercadolibre.com/oauth/token", data=datos,
                      headers={"accept": "application/json"}, timeout=20)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in j:
        raise RuntimeError(f"token ML HTTP {r.status_code} {j.get('error', '')}")
    return j


def ml_access_token():
    if "token" in _ml_cache:
        return _ml_cache["token"]
    cid, sec = os.getenv("ML_CLIENT_ID"), os.getenv("ML_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("faltan credenciales ML (ver README)")
    candidatos = []
    if ML_TOKEN_FILE.exists():
        try:
            candidatos.append(_fernet().decrypt(ML_TOKEN_FILE.read_bytes()).decode())
        except Exception:
            pass
    if os.getenv("ML_REFRESH_TOKEN"):
        candidatos.append(os.getenv("ML_REFRESH_TOKEN"))
    if not candidatos:
        raise RuntimeError("ML sin autorizar: ejecuta el modo ml_inicio (ver README)")
    ultimo = None
    for rt in candidatos:
        try:
            j = _ml_pedir_token({"grant_type": "refresh_token", "client_id": cid,
                                 "client_secret": sec, "refresh_token": rt})
            _ml_guardar_refresh(j["refresh_token"])   # ML entrega un refresh token nuevo cada vez
            _ml_cache["token"] = j["access_token"]
            return j["access_token"]
        except Exception as e:
            ultimo = e
    raise RuntimeError(f"no se pudo renovar el token ML ({ultimo}); repite ml_inicio")


def ml_inicio(codigo):
    cid, sec = os.getenv("ML_CLIENT_ID"), os.getenv("ML_CLIENT_SECRET")
    redirect = os.getenv("ML_REDIRECT_URI") or "https://www.google.com/"
    j = _ml_pedir_token({"grant_type": "authorization_code", "client_id": cid, "client_secret": sec,
                         "code": codigo.strip(), "redirect_uri": redirect})
    _ml_guardar_refresh(j["refresh_token"])
    telegram("✅ <b>Mercado Libre autorizado.</b> El cazador ya puede revisar tiendas oficiales.")


def buscar_ml(termino, pagina, pausa):
    time.sleep(pausa)
    r = requests.get("https://api.mercadolibre.com/sites/MCO/search",
                     params={"q": termino, "offset": pagina * 50, "limit": 50},
                     headers={**HEADERS, "Authorization": f"Bearer {ml_access_token()}"}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return (r.json() or {}).get("results") or []


def extraer_ml(resultados, t):
    marcas = [normalizar(m) for m in (t.get("marcas") or [])]
    salida = []
    for r in resultados:
        if not r.get("official_store_id"):
            continue                       # solo tiendas oficiales
        attrs = {a.get("id"): (a.get("value_name") or "") for a in (r.get("attributes") or [])}
        marca = attrs.get("BRAND", "")
        texto = normalizar(f"{marca} {r.get('title', '')}")
        if marcas and not any(m in texto for m in marcas):
            continue
        precio = float(r.get("price") or 0)
        if precio <= 0:
            continue
        tienda_of = r.get("official_store_name") or (r.get("seller") or {}).get("nickname") or "Tienda oficial"
        salida.append({
            "clave": f"mercadolibre:{r.get('id')}",
            "tienda": t["nombre"], "nombre": r.get("title") or "", "marca": marca,
            "ean": re.sub(r"\D", "", attrs.get("GTIN", ""))[:14], "link": r.get("permalink") or "",
            "precio": precio, "antes": float(r.get("original_price") or precio),
            "stock": r.get("available_quantity") or "?", "vendedor": tienda_of,
        })
    return salida


# ------------------------------------------------------------------ detección
def excluido(oferta, filtros):
    n = normalizar(oferta["nombre"])
    return any(normalizar(w) in n for w in filtros.get("palabras_excluidas", []))


def detectar(ofertas, historial, cfg):
    u = cfg["umbrales"]
    f = cfg["filtros"]
    minimo = f.get("precio_referencia_minimo", 0)

    # Índice por EAN para comparar entre tiendas
    por_ean = {}
    for o in ofertas:
        if len(o["ean"]) >= 8:
            por_ean.setdefault(o["ean"], {})
            tienda_prev = por_ean[o["ean"]].get(o["tienda"])
            if tienda_prev is None or o["precio"] < tienda_prev:
                por_ean[o["ean"]][o["tienda"]] = o["precio"]

    hallazgos = []
    for o in ofertas:
        if excluido(o, f):
            continue
        senales = []
        referencias = []

        # 1) Caída frente a su propio historial
        obs = [x[1] for x in historial.get(o["clave"], {}).get("obs", [])]
        if len(obs) >= 3:
            med = statistics.median(obs)
            caida = (1 - o["precio"] / med) * 100 if med else 0
            if caida >= u["caida_vs_historial"]:
                senales.append(f"{caida:.0f}% bajo su mediana histórica ({cop(med)})")
                referencias.append((med, caida))

        # 2) Brecha frente al mismo producto (EAN) en otras tiendas
        otras = {t: p for t, p in por_ean.get(o["ean"], {}).items() if t != o["tienda"]}
        if otras:
            med_otras = statistics.median(otras.values())
            brecha = (1 - o["precio"] / med_otras) * 100 if med_otras else 0
            if brecha >= u["brecha_vs_otras_tiendas"]:
                detalle = ", ".join(f"{t} {cop(p)}" for t, p in otras.items())
                senales.append(f"{brecha:.0f}% más barato que en otras tiendas ({detalle})")
                referencias.append((med_otras, brecha))

        # 3) Descuento anunciado frente a "precio antes" (señal más débil)
        dto_anunciado = (1 - o["precio"] / o["antes"]) * 100 if o["antes"] > 0 else 0
        if dto_anunciado >= u["descuento_anunciado"]:
            senales.append(f"{dto_anunciado:.0f}% de descuento anunciado (antes {cop(o['antes'])})")

        if not senales:
            continue

        # Precio de referencia: el mejor dato independiente disponible
        if referencias:
            ref, dto_real = max(referencias, key=lambda x: x[1])
            fuente = "historial/otras tiendas"
        else:
            ref, dto_real = o["antes"], dto_anunciado
            fuente = "solo precio 'antes' de la tienda"
        if ref < minimo:
            continue

        # sin referencia independiente nunca se marca como "posible error": el "antes" puede estar inflado
        nivel = "A" if (dto_real >= u["nivel_A"] and referencias) else "B"
        confianza = "alta" if len(referencias) >= 2 else ("media" if referencias else "baja")
        hallazgos.append({**o, "senales": senales, "referencia": ref, "dto_real": dto_real,
                          "fuente": fuente, "nivel": nivel, "confianza": confianza})

    hallazgos.sort(key=lambda h: (h["nivel"], {"alta": 0, "media": 1, "baja": 2}[h["confianza"]], -h["dto_real"]))
    return hallazgos


def actualizar_historial(historial, ofertas):
    ts = int(ahora().timestamp())
    for o in ofertas:
        h = historial.setdefault(o["clave"], {"n": o["nombre"][:80], "obs": []})
        obs = h["obs"]
        # registra solo si cambió el precio o pasaron >6 h desde la última observación
        if not obs or obs[-1][1] != o["precio"] or ts - obs[-1][0] > 6 * 3600:
            obs.append([ts, o["precio"]])
            del obs[:-MAX_OBS]
        h["u"] = ts
    limite = ts - DIAS_PURGA * 86400
    for k in [k for k, v in historial.items() if v.get("u", 0) < limite]:
        del historial[k]


def filtrar_repetidas(hallazgos, enviadas, horas):
    ts = int(ahora().timestamp())
    nuevas = []
    for h in hallazgos:
        prev = enviadas.get(h["clave"])
        if prev and ts - prev["ts"] < horas * 3600 and h["precio"] >= prev["precio"] * 0.97:
            continue
        nuevas.append(h)
        enviadas[h["clave"]] = {"ts": ts, "precio": h["precio"]}
    for k in [k for k, v in enviadas.items() if ts - v["ts"] > 7 * 86400]:
        del enviadas[k]
    return nuevas


def formatear(h):
    icono = "🚨" if h["nivel"] == "A" else "🔥"
    etiqueta = "POSIBLE ERROR DE PRECIO" if h["nivel"] == "A" else "Descuento excepcional"
    lineas = [
        f"{icono} <b>{etiqueta}</b> · confianza {h['confianza']}",
        f"<b>{html.escape(h['nombre'])}</b>",
        f"🏬 {html.escape(h['tienda'])}" + (f" · vende: {html.escape(h['vendedor'])}" if h["vendedor"] else ""),
        f"💰 <b>{cop(h['precio'])}</b>  (referencia {cop(h['referencia'])} · −{h['dto_real']:.0f}%)",
        f"📦 Stock: {h['stock']}",
        "📊 " + "; ".join(html.escape(s) for s in h["senales"]),
        *(["⚠️ Referencia solo del precio 'antes' de la tienda: compáralo con otras tiendas."]
          if h["fuente"].startswith("solo") else []),
        f"🔗 {html.escape(h['link'])}",
    ]
    return "\n".join(lineas)


# ------------------------------------------------------------------ principal
def planes(t, cfg):
    """Devuelve la lista de (descripción, función de búsqueda, extractor) según el tipo de tienda."""
    tipo = t.get("tipo", "vtex")
    if tipo == "vtex":
        return [((lambda term, pag, o=orden: buscar_vtex(t["dominio"], term, o, pag, cfg["_pausa"])),
                 n, (lambda d: extraer_ofertas(d, t["nombre"], t["dominio"])), 50)
                for orden, n in cfg["paginas"].items()]
    if tipo == "falabella":
        return [((lambda term, pag: buscar_falabella(t["dominio"], term, pag, cfg["_pausa"])),
                 cfg.get("paginas_falabella", 2), (lambda d: extraer_falabella(d, t)), 40)]
    if tipo == "mercadolibre":
        return [((lambda term, pag: buscar_ml(term, pag, cfg["_pausa"])),
                 cfg.get("paginas_mercadolibre", 2), (lambda d: extraer_ml(d, t)), 50)]
    raise ValueError(f"tipo de tienda desconocido: {tipo}")


def revisar_tienda(t, cfg, pausa):
    ok, err, lista = 0, [], []
    cfg = {**cfg, "_pausa": pausa}
    for terminos in cfg["categorias"].values():
        for term in terminos:
            for buscar, paginas, extraer, tam in planes(t, cfg):
                for pag in range(paginas):
                    try:
                        datos = buscar(term, pag)
                        ok += 1
                        lista.extend(extraer(datos))
                        if len(datos) < tam:
                            break
                    except Exception as e:
                        err.append(str(e)[:80])
                        break
                if ok == 0 and len(err) >= 3:   # la tienda no responde: no insistir
                    return lista, {"ok": 0, "errores": len(err), "ofertas": 0, "ultimo_error": err[-1]}
    return lista, {"ok": ok, "errores": len(err), "ofertas": len(lista),
                   "ultimo_error": err[-1] if err else ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", default="normal",
                    choices=["normal", "diagnostico", "resumen", "prueba", "ml_inicio"])
    ap.add_argument("--codigo", default="", help="código de autorización de Mercado Libre (modo ml_inicio)")
    args = ap.parse_args()

    if args.modo == "ml_inicio":
        ml_inicio(args.codigo)
        print("Mercado Libre autorizado.")
        sys.exit(0)

    if args.modo == "prueba":
        ok = telegram("✅ <b>Cazador de precios</b>: conexión con Telegram funcionando.")
        sys.exit(0 if ok else 1)

    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    historial = cargar_json(HIST_FILE, {})
    enviadas = cargar_json(ALERT_FILE, {})
    stats = cargar_json(STATS_FILE, {"ejecuciones": 0, "alertas": 0, "desde": ahora().isoformat()})
    pausa = cfg.get("pausa_segundos", 0.6)

    ofertas, estado = [], {}
    inicio = time.time()
    activas = [t for t in cfg["tiendas"] if t.get("activa", True)]
    # cada tienda en paralelo; dentro de cada tienda las solicitudes van en serie con pausa
    with ThreadPoolExecutor(max_workers=max(1, len(activas))) as ex:
        resultados = list(ex.map(lambda t: revisar_tienda(t, cfg, pausa), activas))
    for t, (lista, e) in zip(activas, resultados):
        ofertas.extend(lista)
        estado[t["nombre"]] = e
        print(f"[{t['nombre']}] solicitudes OK={e['ok']} errores={e['errores']} ofertas={e['ofertas']}")

    # quitar duplicados (misma clave en varias búsquedas)
    unicas = {}
    for o in ofertas:
        if o["clave"] not in unicas or o["precio"] < unicas[o["clave"]]["precio"]:
            unicas[o["clave"]] = o
    ofertas = list(unicas.values())

    hallazgos = detectar(ofertas, historial, cfg)
    actualizar_historial(historial, ofertas)
    nuevas = filtrar_repetidas(hallazgos, enviadas, cfg.get("horas_sin_repetir", 24))

    if nuevas:
        encabezado = f"🛒 <b>{len(nuevas)} hallazgo(s)</b> · {ahora().astimezone(TZ_BOG):%d/%m %H:%M}"
        cuerpo = "\n\n".join(formatear(h) for h in nuevas[:15])
        pie = ("<i>Verifica en la página antes de pagar. Errores evidentes pueden ser cancelados "
               "por la tienda (criterio SIC); paga con tarjeta de crédito dentro de la tienda oficial.</i>")
        telegram(f"{encabezado}\n\n{cuerpo}\n\n{pie}")

    stats["ejecuciones"] += 1
    stats["alertas"] += len(nuevas)
    stats["ultima"] = ahora().isoformat()
    stats["estado_tiendas"] = estado

    if args.modo in ("diagnostico", "resumen"):
        titulo = "🩺 <b>Diagnóstico</b>" if args.modo == "diagnostico" else "☀️ <b>Resumen diario</b>"
        filas = []
        for nom, e in estado.items():
            icono = "❌" if e["ok"] == 0 else ("⚠️" if e["ofertas"] == 0 else "✅")
            extra = f" · último error: {html.escape(e['ultimo_error'])}" if e["ok"] == 0 and e["ultimo_error"] else ""
            filas.append(f"{icono} {html.escape(nom)}: {e['ofertas']} ofertas leídas{extra}")
        telegram(
            f"{titulo} del Cazador de precios\n\n" + "\n".join(filas) +
            f"\n\nProductos únicos revisados: {len(ofertas)}"
            f"\nProductos en historial: {len(historial)}"
            f"\nHallazgos en esta ejecución: {len(hallazgos)} (nuevos: {len(nuevas)})"
            f"\nEjecuciones acumuladas: {stats['ejecuciones']} · alertas enviadas: {stats['alertas']}"
            f"\nDuración: {time.time() - inicio:.0f} s"
        )

    guardar_json(HIST_FILE, historial)
    guardar_json(ALERT_FILE, enviadas)
    guardar_json(STATS_FILE, stats)
    print(f"Listo: {len(ofertas)} ofertas, {len(hallazgos)} hallazgos, {len(nuevas)} alertas nuevas.")


if __name__ == "__main__":
    main()
