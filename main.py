"""
=====================================================================
API - DASHBOARD DE REPORTES ESTADÍSTICOS (ERP VENTAS)
=====================================================================
Backend HTTP (functions-framework) pensado para desplegarse en GCP
(Cloud Run / Cloud Functions) sobre las vistas definidas en consultas.sql.

Cada reporte del dashboard tiene SU PROPIA función. Al final,
`registroTableroVentas` es la función principal (router) que recibe
todas las peticiones y las despacha a la función correspondiente.

REGLA DE NEGOCIO (venta válida / no anulada) ya está encapsulada en la
vista base `vw_ventas_validas`:
    ventas.ESTADO = '1' AND detalle_venta.ESTADO = '1'
    AND NOT EXISTS (pagos con TIPO_DE_PAGO='ANULADO' o ESTADO='ANULADO')

Los reportes se consultan sobre `vw_ventas_validas` (no sobre las vistas
ya agregadas) para poder aplicar filtros dinámicos: rango de fechas,
año, mes, asesor, región, tipo de cliente, origen, clasificación, etc.
Sin filtros, el resultado es equivalente a las vistas agregadas.

VARIABLES DE ENTORNO (configurar en el servicio de GCP):
    DB_USER                     usuario MySQL
    DB_PASSWORD                 password MySQL
    DB_NAME                     nombre de la base de datos
    INSTANCE_CONNECTION_NAME    ej: proyecto:region:instancia  (Cloud SQL)
    DB_SOCKET_DIR               opcional, default /cloudsql
    DB_HOST                     alternativa a Cloud SQL socket (IP/host)
    DB_PORT                     opcional, default 3306
    DB_CONNECT_TIMEOUT          opcional, default 10 (segundos)
    API_KEY                     opcional; si se define, se exige el
                                header X-API-Key en cada request
    CORS_ORIGIN                 opcional, default *
=====================================================================
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import functions_framework
import pymysql

logging.basicConfig(level=logging.INFO)

# Zona horaria de Perú (UTC-5)
TZ_PERU = timezone(timedelta(hours=-5))

# Vista base con la regla de negocio de "venta válida"
VISTA_BASE = "vw_ventas_validas"

# Tope de filas por respuesta para no reventar la memoria del servicio
LIMITE_MAXIMO = 5000


def get_now_peru():
    return datetime.now(TZ_PERU).strftime("%Y-%m-%d %H:%M:%S")


# =====================================================================
# CONEXIÓN A LA BASE DE DATOS (100% por variables de entorno)
# =====================================================================
def get_connection():
    """Abre una conexión a MySQL leyendo la configuración del entorno.

    Si existe INSTANCE_CONNECTION_NAME se conecta por unix_socket
    (Cloud SQL); en caso contrario usa DB_HOST/DB_PORT (TCP).
    """
    try:
        params = {
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "db": os.environ["DB_NAME"],
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
            "charset": os.environ.get("DB_CHARSET", "utf8mb4"),
        }

        instancia = os.environ.get("INSTANCE_CONNECTION_NAME")
        if instancia:
            socket_dir = os.environ.get("DB_SOCKET_DIR", "/cloudsql")
            params["unix_socket"] = f"{socket_dir}/{instancia}"
        else:
            params["host"] = os.environ.get("DB_HOST", "127.0.0.1")
            params["port"] = int(os.environ.get("DB_PORT", "3306"))

        return pymysql.connect(**params)
    except KeyError as e:
        logging.error(f"Falta la variable de entorno obligatoria: {e}")
        return None
    except Exception as e:
        logging.error(f"Error crítico al conectar a la base de datos: {e}")
        return None


def consultar(sql, params=None):
    """Ejecuta un SELECT y devuelve la lista de filas (dicts)."""
    conn = get_connection()
    if conn is None:
        raise ConexionError("No se pudo conectar a la base de datos")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def consultar_uno(sql, params=None):
    """Ejecuta un SELECT y devuelve solo la primera fila (o {})."""
    filas = consultar(sql, params)
    return filas[0] if filas else {}


class ConexionError(Exception):
    """Error de conexión a la base de datos."""


class SolicitudInvalida(Exception):
    """Parámetros de entrada inválidos (se responde 400)."""


# =====================================================================
# HELPERS DE SERIALIZACIÓN Y RESPUESTA HTTP
# =====================================================================
def _json_default(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, timedelta):
        return str(valor)
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return str(valor)


def _cabeceras():
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key",
        "Access-Control-Max-Age": "3600",
    }


def responder(payload, status=200):
    cuerpo = json.dumps(payload, default=_json_default, ensure_ascii=False)
    return (cuerpo, status, _cabeceras())


def responder_ok(data, extra=None):
    payload = {
        "success": True,
        "timestamp": get_now_peru(),
        "total_registros": len(data) if isinstance(data, list) else 1,
        "data": data,
    }
    if extra:
        payload.update(extra)
    return responder(payload)


def responder_error(mensaje, status=500):
    return responder({"success": False, "error": mensaje, "timestamp": get_now_peru()}, status)


# =====================================================================
# HELPERS DE FILTROS (se aplican sobre la vista base)
# =====================================================================
# Mapa de filtro -> condición SQL. Las claves son los nombres que se
# reciben por query string; los valores viajan siempre parametrizados.
FILTROS_VENTAS = {
    "desde": "FECHA >= %s",
    "hasta": "FECHA <= %s",
    "anio": "ANIO = %s",
    "mes": "MES = %s",
    "anio_mes": "ANIO_MES = %s",
    "asesor": "ASESOR = %s",
    "id_region": "ID_REGION = %s",
    "region": "REGION_VENTA = %s",
    "id_distrito": "ID_DISTRITO = %s",
    "id_cliente": "ID_CLIENTE = %s",
    "tipo_cliente": "TIPO_CLIENTE = %s",
    "origen": "ORIGEN = %s",
    "segmentacion": "SEGMENTACION = %s",
    "clasificacion": "VENTA_CLASIFICACION = %s",
    "clasificacion_cliente": "CLIENTE_CLASIFICACION = %s",
    "salida_de_pedido": "SALIDA_DE_PEDIDO = %s",
    "codigo": "CODIGO = %s",
}

FILTROS_NUMERICOS = {"anio", "mes", "id_cliente"}
FILTROS_FECHA = {"desde", "hasta"}


def _leer_parametros(request):
    """Une los parámetros de query string con los del body JSON."""
    datos = {}
    try:
        cuerpo = request.get_json(silent=True)
        if isinstance(cuerpo, dict):
            datos.update({k: v for k, v in cuerpo.items() if v not in (None, "")})
    except Exception:
        pass
    for clave in request.args.keys():
        valor = request.args.get(clave)
        if valor not in (None, ""):
            datos[clave] = valor
    return datos


def construir_filtros(request, permitidos=None):
    """Arma el fragmento WHERE y sus parámetros a partir del request."""
    permitidos = permitidos or FILTROS_VENTAS
    datos = _leer_parametros(request)

    condiciones, valores = [], []
    for clave, condicion in permitidos.items():
        if clave not in datos:
            continue
        valor = datos[clave]

        if clave in FILTROS_NUMERICOS:
            try:
                valor = int(valor)
            except (TypeError, ValueError):
                raise SolicitudInvalida(f"El parámetro '{clave}' debe ser numérico")
        elif clave in FILTROS_FECHA:
            try:
                datetime.strptime(str(valor), "%Y-%m-%d")
            except ValueError:
                raise SolicitudInvalida(f"El parámetro '{clave}' debe tener formato YYYY-MM-DD")

        condiciones.append(condicion)
        valores.append(valor)

    where = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
    return where, tuple(valores)


def leer_limite(request, default=50):
    """Devuelve el LIMIT solicitado, acotado a LIMITE_MAXIMO."""
    datos = _leer_parametros(request)
    bruto = datos.get("limit", default)
    try:
        limite = int(bruto)
    except (TypeError, ValueError):
        raise SolicitudInvalida("El parámetro 'limit' debe ser numérico")
    if limite <= 0:
        raise SolicitudInvalida("El parámetro 'limit' debe ser mayor a 0")
    return min(limite, LIMITE_MAXIMO)


def leer_texto(request, clave, default=None):
    return _leer_parametros(request).get(clave, default)


def leer_entero(request, clave, default):
    valor = _leer_parametros(request).get(clave, default)
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise SolicitudInvalida(f"El parámetro '{clave}' debe ser numérico")


# =====================================================================
# REPORTES DEL DASHBOARD (una función por reporte)
# =====================================================================

# ---------------------------------------------------------------------
# Total de ventas por mes  (vw_ventas_por_mes)
# ---------------------------------------------------------------------
def ventas_por_mes(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ANIO,
               MES,
               ANIO_MES,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS,
               ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
        FROM {VISTA_BASE}
        {where}
        GROUP BY ANIO, MES, ANIO_MES
        ORDER BY ANIO, MES
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Cantidad total de ventas por año  (vw_ventas_por_anio)
# ---------------------------------------------------------------------
def ventas_por_anio(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ANIO,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS,
               ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
        FROM {VISTA_BASE}
        {where}
        GROUP BY ANIO
        ORDER BY ANIO
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Productos más vendidos  (vw_productos_mas_vendidos)
# ---------------------------------------------------------------------
def productos_mas_vendidos(request):
    where, params = construir_filtros(request)
    limite = leer_limite(request, 20)
    orden = "MONTO_TOTAL" if leer_texto(request, "orden") == "monto" else "CANTIDAD_TOTAL"
    sql = f"""
        SELECT CODIGO,
               PRODUCTO,
               SUM(CANTIDAD)            AS CANTIDAD_TOTAL,
               SUM(TOTAL)               AS MONTO_TOTAL,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY CODIGO, PRODUCTO
        ORDER BY {orden} DESC
        LIMIT %s
    """
    return responder_ok(consultar(sql, params + (limite,)))


# ---------------------------------------------------------------------
# Productos menos vendidos  (vw_productos_menos_vendidos)
# ---------------------------------------------------------------------
def productos_menos_vendidos(request):
    where, params = construir_filtros(request)
    limite = leer_limite(request, 20)
    sql = f"""
        SELECT CODIGO,
               PRODUCTO,
               SUM(CANTIDAD)            AS CANTIDAD_TOTAL,
               SUM(TOTAL)               AS MONTO_TOTAL,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY CODIGO, PRODUCTO
        HAVING CANTIDAD_TOTAL > 0
        ORDER BY CANTIDAD_TOTAL ASC
        LIMIT %s
    """
    return responder_ok(consultar(sql, params + (limite,)))


# ---------------------------------------------------------------------
# Clientes que más compran  (vw_clientes_top_compradores)
# ---------------------------------------------------------------------
def clientes_top_compradores(request):
    where, params = construir_filtros(request)
    limite = leer_limite(request, 20)
    orden = "NUM_VENTAS" if leer_texto(request, "orden") == "frecuencia" else "TOTAL_COMPRADO"
    sql = f"""
        SELECT ID_CLIENTE,
               CLIENTE,
               TIPO_CLIENTE,
               ORIGEN,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_COMPRADO,
               MAX(FECHA)               AS ULTIMA_COMPRA,
               ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
        FROM {VISTA_BASE}
        {where}
        GROUP BY ID_CLIENTE, CLIENTE, TIPO_CLIENTE, ORIGEN
        ORDER BY {orden} DESC
        LIMIT %s
    """
    return responder_ok(consultar(sql, params + (limite,)))


# ---------------------------------------------------------------------
# Cantidad de clientes (KPI)  (vw_kpi_clientes)
# ---------------------------------------------------------------------
def kpi_clientes(request):
    sql = """
        SELECT (SELECT COUNT(*) FROM cliente WHERE ESTADO = '1')          AS CLIENTES_ACTIVOS_TOTAL,
               (SELECT COUNT(DISTINCT ID_CLIENTE) FROM vw_ventas_validas) AS CLIENTES_CON_COMPRAS,
               (SELECT COUNT(*) FROM cliente WHERE ESTADO = '1')
                 - (SELECT COUNT(DISTINCT ID_CLIENTE) FROM vw_ventas_validas) AS CLIENTES_SIN_COMPRAS
    """
    return responder_ok(consultar_uno(sql))


# ---------------------------------------------------------------------
# Clientes por ORIGEN (maestro de clientes)  (vw_clientes_por_origen)
# ---------------------------------------------------------------------
def clientes_por_origen(request):
    sql = """
        SELECT ORIGEN,
               COUNT(*) AS NUM_CLIENTES
        FROM cliente
        WHERE ESTADO = '1'
        GROUP BY ORIGEN
        ORDER BY NUM_CLIENTES DESC
    """
    return responder_ok(consultar(sql))


# ---------------------------------------------------------------------
# Ventas por ORIGEN del cliente  (vw_ventas_por_origen)
# ---------------------------------------------------------------------
def ventas_por_origen(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ORIGEN,
               COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
               COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY ORIGEN
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Clientes por TIPO_CLIENTE (maestro)  (vw_clientes_por_tipo)
# ---------------------------------------------------------------------
def clientes_por_tipo(request):
    sql = """
        SELECT TIPO_CLIENTE,
               COUNT(*) AS NUM_CLIENTES
        FROM cliente
        WHERE ESTADO = '1'
        GROUP BY TIPO_CLIENTE
        ORDER BY NUM_CLIENTES DESC
    """
    return responder_ok(consultar(sql))


# ---------------------------------------------------------------------
# Ventas por TIPO_CLIENTE  (vw_ventas_por_tipo_cliente)
# ---------------------------------------------------------------------
def ventas_por_tipo_cliente(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT TIPO_CLIENTE,
               COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
               COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY TIPO_CLIENTE
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Ventas por región (región de la venta)  (vw_ventas_por_region)
# ---------------------------------------------------------------------
def ventas_por_region(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ID_REGION,
               REGION_VENTA             AS REGION,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY ID_REGION, REGION_VENTA
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Ventas por distrito (distrito de la venta)  (vw_ventas_por_distrito)
# ---------------------------------------------------------------------
def ventas_por_distrito(request):
    where, params = construir_filtros(request)
    limite = leer_limite(request, 100)
    sql = f"""
        SELECT ID_REGION,
               REGION_VENTA             AS REGION,
               ID_DISTRITO,
               DISTRITO_VENTA           AS DISTRITO,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY ID_REGION, REGION_VENTA, ID_DISTRITO, DISTRITO_VENTA
        ORDER BY TOTAL_VENTAS DESC
        LIMIT %s
    """
    return responder_ok(consultar(sql, params + (limite,)))


# ---------------------------------------------------------------------
# Ventas por asesor (cantidad + total vendido)  (vw_ventas_por_asesor)
# ---------------------------------------------------------------------
def ventas_por_asesor(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ASESOR,
               COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS,
               COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES_ATENDIDOS,
               ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
        FROM {VISTA_BASE}
        {where}
        GROUP BY ASESOR
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Productos más vendidos por asesor  (vw_productos_por_asesor)
# Parámetros extra: asesor (opcional), top (default 5)
# ---------------------------------------------------------------------
def productos_por_asesor(request):
    where, params = construir_filtros(request)
    top = leer_entero(request, "top", 5)
    if top <= 0:
        raise SolicitudInvalida("El parámetro 'top' debe ser mayor a 0")
    sql = f"""
        SELECT ASESOR,
               CODIGO,
               PRODUCTO,
               CANTIDAD_TOTAL,
               MONTO_TOTAL,
               RANKING
        FROM (
            SELECT ASESOR,
                   CODIGO,
                   PRODUCTO,
                   CANTIDAD_TOTAL,
                   MONTO_TOTAL,
                   RANK() OVER (PARTITION BY ASESOR ORDER BY CANTIDAD_TOTAL DESC) AS RANKING
            FROM (
                SELECT ASESOR,
                       CODIGO,
                       PRODUCTO,
                       SUM(CANTIDAD) AS CANTIDAD_TOTAL,
                       SUM(TOTAL)    AS MONTO_TOTAL
                FROM {VISTA_BASE}
                {where}
                GROUP BY ASESOR, CODIGO, PRODUCTO
            ) agrupado
        ) ranking
        WHERE RANKING <= %s
        ORDER BY ASESOR, RANKING
    """
    return responder_ok(consultar(sql, params + (top,)))


# ---------------------------------------------------------------------
# Ventas por clasificación de la VENTA  (vw_ventas_por_clasificacion)
# ---------------------------------------------------------------------
def ventas_por_clasificacion(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT VENTA_CLASIFICACION     AS CLASIFICACION,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY VENTA_CLASIFICACION
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Ventas por clasificación del CLIENTE
# ---------------------------------------------------------------------
def ventas_por_clasificacion_cliente(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT CLIENTE_CLASIFICACION    AS CLASIFICACION_CLIENTE,
               COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
               COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY CLIENTE_CLASIFICACION
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Salidas de pedido  (vw_salidas_de_pedido)
# ---------------------------------------------------------------------
def salidas_de_pedido(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT SALIDA_DE_PEDIDO,
               COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
               SUM(TOTAL)               AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY SALIDA_DE_PEDIDO
        ORDER BY NUM_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Ventas por segmentación del cliente  (vw_ventas_por_segmentacion)
# ---------------------------------------------------------------------
def ventas_por_segmentacion(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT SEGMENTACION,
               COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
               COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS
        FROM {VISTA_BASE}
        {where}
        GROUP BY SEGMENTACION
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# KPIs resumen (tarjetas). Por defecto el mes actual; acepta filtros.
# ---------------------------------------------------------------------
def kpi_resumen(request):
    where, params = construir_filtros(request)
    if not where:
        where = " WHERE ANIO = YEAR(CURDATE()) AND MES = MONTH(CURDATE())"
    sql = f"""
        SELECT COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
               SUM(TOTAL)                 AS TOTAL_VENTAS,
               COUNT(DISTINCT ID_CLIENTE) AS CLIENTES_ATENDIDOS,
               COUNT(DISTINCT ASESOR)     AS ASESORES_ACTIVOS,
               COUNT(DISTINCT CODIGO)     AS PRODUCTOS_DISTINTOS,
               SUM(CANTIDAD)              AS UNIDADES_VENDIDAS,
               ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO,
               MIN(FECHA)                 AS PRIMERA_VENTA,
               MAX(FECHA)                 AS ULTIMA_VENTA
        FROM {VISTA_BASE}
        {where}
    """
    return responder_ok(consultar_uno(sql, params))


# ---------------------------------------------------------------------
# Clientes nuevos por mes  (vw_clientes_nuevos_por_mes)
# ---------------------------------------------------------------------
def clientes_nuevos_por_mes(request):
    sql = """
        SELECT YEAR(FECHA_CREACION)                 AS ANIO,
               MONTH(FECHA_CREACION)                AS MES,
               DATE_FORMAT(FECHA_CREACION, '%%Y-%%m') AS ANIO_MES,
               COUNT(*)                             AS CLIENTES_NUEVOS
        FROM cliente
        WHERE ESTADO = '1'
        GROUP BY YEAR(FECHA_CREACION), MONTH(FECHA_CREACION), DATE_FORMAT(FECHA_CREACION, '%%Y-%%m')
        ORDER BY ANIO, MES
    """
    return responder_ok(consultar(sql, ()))


# ---------------------------------------------------------------------
# Ventas por tipo de pago  (vw_ventas_por_tipo_pago)
# El total se calcula por venta (subconsulta) para no multiplicar el
# monto por cada línea de detalle.
# ---------------------------------------------------------------------
def ventas_por_tipo_pago(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT p.TIPO_DE_PAGO,
               p.REGULARIZADO,
               p.CANCELADO,
               COUNT(DISTINCT p.ID_VENTA) AS NUM_VENTAS,
               SUM(vt.TOTAL_VENTA)        AS TOTAL_VENTAS
        FROM (
            SELECT DISTINCT ID_VENTA, TIPO_DE_PAGO, REGULARIZADO, CANCELADO
            FROM pagos
            WHERE TIPO_DE_PAGO <> 'ANULADO' AND ESTADO <> 'ANULADO'
        ) p
        INNER JOIN (
            SELECT ID_VENTA, SUM(TOTAL) AS TOTAL_VENTA
            FROM {VISTA_BASE}
            {where}
            GROUP BY ID_VENTA
        ) vt ON vt.ID_VENTA = p.ID_VENTA
        GROUP BY p.TIPO_DE_PAGO, p.REGULARIZADO, p.CANCELADO
        ORDER BY TOTAL_VENTAS DESC
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Comparativo mensual (mes vs mes anterior con variación %)
# ---------------------------------------------------------------------
def comparativo_mensual(request):
    where, params = construir_filtros(request)
    sql = f"""
        SELECT ANIO_MES,
               NUM_VENTAS,
               TOTAL_VENTAS,
               TOTAL_VENTAS_MES_ANTERIOR,
               ROUND(
                   (TOTAL_VENTAS - TOTAL_VENTAS_MES_ANTERIOR)
                   / NULLIF(TOTAL_VENTAS_MES_ANTERIOR, 0) * 100
               , 2) AS VARIACION_PORCENTUAL
        FROM (
            SELECT ANIO_MES,
                   NUM_VENTAS,
                   TOTAL_VENTAS,
                   LAG(TOTAL_VENTAS) OVER (ORDER BY ANIO_MES) AS TOTAL_VENTAS_MES_ANTERIOR
            FROM (
                SELECT ANIO_MES,
                       COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
                       SUM(TOTAL)               AS TOTAL_VENTAS
                FROM {VISTA_BASE}
                {where}
                GROUP BY ANIO_MES
            ) mensual
        ) comparativo
        ORDER BY ANIO_MES
    """
    return responder_ok(consultar(sql, params))


# ---------------------------------------------------------------------
# Top clientes por asesor  (vw_top_clientes_por_asesor)
# ---------------------------------------------------------------------
def top_clientes_por_asesor(request):
    where, params = construir_filtros(request)
    top = leer_entero(request, "top", 5)
    if top <= 0:
        raise SolicitudInvalida("El parámetro 'top' debe ser mayor a 0")
    sql = f"""
        SELECT ASESOR,
               ID_CLIENTE,
               CLIENTE,
               NUM_VENTAS,
               TOTAL_COMPRADO,
               RANKING
        FROM (
            SELECT ASESOR,
                   ID_CLIENTE,
                   CLIENTE,
                   COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
                   SUM(TOTAL)               AS TOTAL_COMPRADO,
                   RANK() OVER (PARTITION BY ASESOR ORDER BY SUM(TOTAL) DESC) AS RANKING
            FROM {VISTA_BASE}
            {where}
            GROUP BY ASESOR, ID_CLIENTE, CLIENTE
        ) ranking
        WHERE RANKING <= %s
        ORDER BY ASESOR, RANKING
    """
    return responder_ok(consultar(sql, params + (top,)))


# ---------------------------------------------------------------------
# Clientes inactivos (sin compras en los últimos N meses)
# ---------------------------------------------------------------------
def clientes_inactivos(request):
    meses = leer_entero(request, "meses", 6)
    if meses <= 0:
        raise SolicitudInvalida("El parámetro 'meses' debe ser mayor a 0")
    limite = leer_limite(request, 200)
    sql = f"""
        SELECT c.ID_CLIENTE,
               c.CLIENTE,
               c.TIPO_CLIENTE,
               c.ASESOR,
               MAX(vv.FECHA) AS ULTIMA_COMPRA
        FROM cliente c
        LEFT JOIN {VISTA_BASE} vv ON vv.ID_CLIENTE = c.ID_CLIENTE
        WHERE c.ESTADO = '1'
        GROUP BY c.ID_CLIENTE, c.CLIENTE, c.TIPO_CLIENTE, c.ASESOR
        HAVING MAX(vv.FECHA) IS NULL
            OR MAX(vv.FECHA) < DATE_SUB(CURDATE(), INTERVAL %s MONTH)
        ORDER BY ULTIMA_COMPRA
        LIMIT %s
    """
    return responder_ok(consultar(sql, (meses, limite)))


# ---------------------------------------------------------------------
# Catálogos para los combos de filtros del dashboard
# ---------------------------------------------------------------------
def catalogos(request):
    data = {
        "asesores": consultar(
            "SELECT DISTINCT ASESOR FROM ventas WHERE ESTADO = '1' AND ASESOR IS NOT NULL ORDER BY ASESOR"
        ),
        "regiones": consultar("SELECT ID_REGION, REGION FROM region ORDER BY REGION"),
        "distritos": consultar(
            "SELECT ID_DISTRITO, DISTRITO, ID_REGION FROM distrito ORDER BY DISTRITO"
        ),
        "tipos_cliente": consultar(
            "SELECT DISTINCT TIPO_CLIENTE FROM cliente WHERE ESTADO = '1' ORDER BY TIPO_CLIENTE"
        ),
        "origenes": consultar(
            "SELECT DISTINCT ORIGEN FROM cliente WHERE ESTADO = '1' ORDER BY ORIGEN"
        ),
        "clasificaciones_venta": consultar(
            "SELECT DISTINCT CLASIFICACION FROM ventas WHERE ESTADO = '1' ORDER BY CLASIFICACION"
        ),
        "salidas_de_pedido": consultar(
            "SELECT DISTINCT SALIDA_DE_PEDIDO FROM ventas WHERE ESTADO = '1' ORDER BY SALIDA_DE_PEDIDO"
        ),
        "anios": consultar(
            f"SELECT DISTINCT ANIO FROM {VISTA_BASE} ORDER BY ANIO DESC"
        ),
    }
    return responder_ok(data)


# ---------------------------------------------------------------------
# Dashboard completo: arma en una sola llamada los bloques principales
# (evita que el front haga 15 requests al cargar la pantalla)
# ---------------------------------------------------------------------
def dashboard_completo(request):
    bloques = {
        "kpi_resumen": kpi_resumen,
        "kpi_clientes": kpi_clientes,
        "ventas_por_mes": ventas_por_mes,
        "ventas_por_anio": ventas_por_anio,
        "productos_mas_vendidos": productos_mas_vendidos,
        "productos_menos_vendidos": productos_menos_vendidos,
        "clientes_top_compradores": clientes_top_compradores,
        "clientes_por_origen": clientes_por_origen,
        "ventas_por_origen": ventas_por_origen,
        "clientes_por_tipo": clientes_por_tipo,
        "ventas_por_tipo_cliente": ventas_por_tipo_cliente,
        "ventas_por_region": ventas_por_region,
        "ventas_por_asesor": ventas_por_asesor,
        "ventas_por_clasificacion": ventas_por_clasificacion,
        "salidas_de_pedido": salidas_de_pedido,
    }

    data, errores = {}, {}
    for nombre, funcion in bloques.items():
        try:
            cuerpo, _status, _headers = funcion(request)
            data[nombre] = json.loads(cuerpo).get("data")
        except Exception as e:
            logging.exception(f"Error armando el bloque '{nombre}' del dashboard")
            errores[nombre] = str(e)
            data[nombre] = None

    payload = {
        "success": not errores,
        "timestamp": get_now_peru(),
        "data": data,
    }
    if errores:
        payload["errores"] = errores
    return responder(payload)


# ---------------------------------------------------------------------
# Health check (verifica que la conexión a MySQL responda)
# ---------------------------------------------------------------------
def health(request):
    try:
        consultar_uno("SELECT 1 AS ok")
        estado_db = "ok"
    except Exception as e:
        logging.error(f"Health check falló: {e}")
        estado_db = f"error: {e}"
    return responder(
        {
            "success": estado_db == "ok",
            "servicio": "api-dashboard-ventas",
            "base_datos": estado_db,
            "timestamp": get_now_peru(),
        },
        200 if estado_db == "ok" else 503,
    )


# =====================================================================
# TABLA DE RUTAS: ruta -> función del reporte
# =====================================================================
RUTAS = {
    "health": health,
    "catalogos": catalogos,
    "dashboard": dashboard_completo,
    "kpi_resumen": kpi_resumen,
    "kpi_clientes": kpi_clientes,
    "ventas_por_mes": ventas_por_mes,
    "ventas_por_anio": ventas_por_anio,
    "productos_mas_vendidos": productos_mas_vendidos,
    "productos_menos_vendidos": productos_menos_vendidos,
    "productos_por_asesor": productos_por_asesor,
    "clientes_top_compradores": clientes_top_compradores,
    "clientes_por_origen": clientes_por_origen,
    "ventas_por_origen": ventas_por_origen,
    "clientes_por_tipo": clientes_por_tipo,
    "ventas_por_tipo_cliente": ventas_por_tipo_cliente,
    "ventas_por_region": ventas_por_region,
    "ventas_por_distrito": ventas_por_distrito,
    "ventas_por_asesor": ventas_por_asesor,
    "ventas_por_clasificacion": ventas_por_clasificacion,
    "ventas_por_clasificacion_cliente": ventas_por_clasificacion_cliente,
    "ventas_por_segmentacion": ventas_por_segmentacion,
    "salidas_de_pedido": salidas_de_pedido,
    "ventas_por_tipo_pago": ventas_por_tipo_pago,
    "comparativo_mensual": comparativo_mensual,
    "top_clientes_por_asesor": top_clientes_por_asesor,
    "clientes_nuevos_por_mes": clientes_nuevos_por_mes,
    "clientes_inactivos": clientes_inactivos,
}


def _resolver_ruta(request):
    """Obtiene el nombre del reporte desde el path o desde ?reporte=."""
    segmento = (request.path or "/").strip("/").split("/")[-1]
    if not segmento:
        segmento = leer_texto(request, "reporte", "") or ""
    return segmento.replace("-", "_").lower()


def _autorizado(request):
    """Si API_KEY está configurada, exige el header X-API-Key."""
    api_key = os.environ.get("API_KEY")
    if not api_key:
        return True
    recibida = request.headers.get("X-API-Key") or request.args.get("api_key")
    return recibida == api_key


# =====================================================================
# FUNCIÓN PRINCIPAL (ROUTER) - entrypoint del servicio en GCP
# =====================================================================
@functions_framework.http
def registro_dashboard_ventas(request):
    """Router principal: recibe la petición y la despacha a la función
    del reporte correspondiente según la ruta.

    Ejemplos:
        GET /ventas_por_mes?anio=2026
        GET /ventas-por-asesor?desde=2026-01-01&hasta=2026-03-31
        GET /productos_mas_vendidos?limit=10&asesor=JUAN
        GET /productos_por_asesor?asesor=JUAN&top=5
        GET /dashboard?anio=2026
        GET /?reporte=ventas_por_region
    """
    if request.method == "OPTIONS":
        return ("", 204, _cabeceras())

    if not _autorizado(request):
        return responder_error("No autorizado", 401)

    ruta = _resolver_ruta(request)

    # Sin ruta: se devuelve el índice de endpoints disponibles
    if not ruta:
        return responder(
            {
                "success": True,
                "servicio": "api-dashboard-ventas",
                "timestamp": get_now_peru(),
                "endpoints": sorted(RUTAS.keys()),
                "filtros_disponibles": sorted(FILTROS_VENTAS.keys()) + ["limit", "top", "orden", "meses"],
            }
        )

    funcion = RUTAS.get(ruta)
    if funcion is None:
        return responder_error(
            f"Reporte '{ruta}' no encontrado. Endpoints disponibles: {sorted(RUTAS.keys())}", 404
        )

    try:
        return funcion(request)
    except SolicitudInvalida as e:
        return responder_error(str(e), 400)
    except ConexionError as e:
        logging.error(f"[{ruta}] {e}")
        return responder_error(str(e), 503)
    except pymysql.MySQLError as e:
        logging.exception(f"[{ruta}] Error de MySQL")
        return responder_error(f"Error al consultar la base de datos: {e}", 500)
    except Exception as e:
        logging.exception(f"[{ruta}] Error inesperado")
        return responder_error(f"Error interno: {e}", 500)
