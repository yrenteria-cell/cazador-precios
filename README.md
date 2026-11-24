# Cazador de precios bajos 🇨🇴

Revisa cada 15 minutos catálogos **oficiales** de tiendas en Colombia y te avisa por **Telegram** cuando un producto aparece con un precio anormalmente bajo. Corre gratis en la nube (GitHub Actions), aunque tu computador esté apagado.

## Qué tiendas vigila

| Grupo | Tiendas | Requisito |
|---|---|---|
| Cadenas | Olímpica ✔, Éxito, Carulla, Jumbo, Metro, Panamericana | Ninguno |
| Marcas directas | Haceb ✔, Challenger ✔, Whirlpool ✔, Imusa ✔, Electrolux, Mabe, Oster | Ninguno |
| Falabella | Solo productos vendidos por Falabella (no marketplace) | Ninguno (experimental) |
| Tiendas oficiales de marca en Mercado Libre | Samsung, Huawei, Honor, TCL, Xiaomi, Lenovo, Motorola, LG, HP, Asus, Apple, Sony, JBL y más | Credenciales gratuitas de Mercado Libre (paso 5) |

✔ = catálogo comprobado durante la construcción de la app. Las demás se confirman con el modo `diagnostico`.

Las tiendas propias de Samsung, Huawei, Honor, Xiaomi, LG y Lenovo no exponen un catálogo legible por programas. Por eso esas marcas se vigilan a través de sus **tiendas oficiales en Mercado Libre**. La tarea programada de Claude cada 3 horas revisa además sus sitios directos.

## Cómo detecta un precio anormal

| Señal | Qué compara | Confiabilidad |
|---|---|---|
| Caída vs. historial | Precio actual frente a la mediana de los precios registrados de ese producto en esa tienda | Alta (desde 3 observaciones) |
| Brecha vs. otras tiendas | Mismo producto (código EAN/GTIN) más barato que en las demás tiendas | Alta |
| Descuento anunciado | Precio frente al precio "antes" que muestra la tienda | Baja: el "antes" puede estar inflado |

- 🚨 **Posible error de precio (A):** descuento real de 60 % o más, respaldado por el historial o por otras tiendas.
- 🔥 **Descuento excepcional (B):** el resto de hallazgos.

La app ignora accesorios, productos sin stock y productos con un precio normal inferior a $250.000. Todo se ajusta en `config.yaml`.

---

## Instalación

### 1. Crea tu bot de Telegram
1. En Telegram, abre **@BotFather** y envía `/newbot`. Ponle un nombre y un usuario que termine en `bot`.
2. Guarda el **token** que te entrega (por ejemplo, `7123456789:AAH...`). No lo compartas con nadie.
3. Abre tu bot y envíale `/start`.
4. Para obtener tu **chat ID**, escríbele a **@userinfobot**, que te responde con tu ID. Otra opción: abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca `"chat":{"id":...`.

### 2. Crea el repositorio en GitHub
1. Haz clic en **New repository**, ponle el nombre `cazador-precios` y elige **Public**. Uno público tiene minutos gratis ilimitados; uno privado se queda corto. Las claves no quedan expuestas, porque van como *secrets*.
2. Haz clic en **Add file → Upload files**, sube todos los archivos y confirma con **Commit changes**.
   - ⚠️ La carpeta `.github` puede estar oculta en tu computador. Verifica que exista `.github/workflows/cazador.yml` en el repositorio. Si no aparece, usa **Add file → Create new file**, escribe ese nombre exacto y pega el contenido.

### 3. Guarda tus credenciales
Entra a **Settings → Secrets and variables → Actions → New repository secret** y crea:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. Actívalo y pruébalo
1. Abre **Actions** y habilita los workflows.
2. Entra a **Cazador de precios → Run workflow** y elige el modo `prueba`. Debe llegarte un ✅ a Telegram.
3. Ejecuta con el modo `diagnostico`. Recibirás un reporte por tienda: ✅ responde con productos, ⚠️ responde vacío, ❌ bloquea o falla. Desactiva las ❌ en `config.yaml` con `activa: false`.

Desde ese momento corre sola cada 15 minutos y te envía un resumen diario a las 8:07 a.m.

### 5. (Opcional, recomendado) Tiendas oficiales en Mercado Libre
Aquí entran Samsung, Huawei, Honor, TCL, Xiaomi y las demás marcas. La API de Mercado Libre exige autorización, que se configura una sola vez:

1. Entra a **developers.mercadolibre.com.co** con tu cuenta de Mercado Libre y crea una aplicación.
   - **URI de redirección:** `https://www.google.com/`
   - **Permisos:** lectura
2. Copia el **App ID** y la **Secret Key** y guárdalos como secrets `ML_CLIENT_ID` y `ML_CLIENT_SECRET`. Si usaste otra URI de redirección, guárdala como `ML_REDIRECT_URI`.
3. Abre en el navegador esta dirección, con tu App ID en lugar de `TU_APP_ID`:
   `https://auth.mercadolibre.com.co/authorization?response_type=code&client_id=TU_APP_ID&redirect_uri=https://www.google.com/`
4. Acepta. El navegador te llevará a `https://www.google.com/?code=TG-xxxxxxxx`. Copia el código completo, desde `TG-` en adelante.
5. **En menos de 10 minutos**, entra a **Run workflow**, elige el modo `ml_inicio` y pega el código en el campo **codigo**. Te llegará "✅ Mercado Libre autorizado".

La app renueva la autorización sola en cada ejecución y la guarda cifrada. Si pasan más de 7 días sin ejecuciones, o la app reporta un error de token, repite los pasos 3 a 5.

---

## Personalización (`config.yaml`)
- **categorias:** agrega términos de búsqueda, por ejemplo `huawei mate`, `tcl 65`.
- **marcas** (en Mercado Libre): lista de marcas oficiales que te interesan.
- **solo_vendido_por** (en Falabella): agrega vendedores oficiales adicionales si quieres.
- **umbrales / filtros:** ajusta la sensibilidad.
- **Cualquier otra tienda con VTEX** se agrega con una línea: `{nombre: X, tipo: vtex, dominio: www.x.com.co, activa: true}`.

## Limitaciones
- **Las primeras horas** la app solo detecta brechas entre tiendas y descuentos anunciados. La comparación contra el historial mejora con los días.
- **Falabella y Mercado Libre** usan interfaces que pueden cambiar o bloquear servidores en la nube. El diagnóstico te lo mostrará.
- **GitHub puede retrasar** las ejecuciones unos minutos en horas pico.
- **Nota legal:** en Colombia el precio anunciado obliga (Ley 1480, art. 26), pero la SIC exime al vendedor si el error es "evidente o notorio". Un error muy grande puede ser cancelado con reembolso. Compra solo dentro de la tienda oficial y con tarjeta de crédito.
