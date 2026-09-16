-- =====================================================================
-- DASHBOARD DE REPORTES - ERP VENTAS
-- Vistas SQL (MySQL 8.0+, requiere soporte de funciones de ventana)
-- =====================================================================
--
-- REGLA DE NEGOCIO CENTRAL (venta "válida" / no anulada):
--   - ventas.ESTADO = '1'
--   - detalle_venta.ESTADO = '1'
--   - No debe existir ningún registro en `pagos` asociado a esa venta
--     con TIPO_DE_PAGO = 'ANULADO' o ESTADO = 'ANULADO'
--
-- Todas las vistas de reporte parten de la vista base vw_ventas_validas
-- para no repetir esta lógica en cada consulta (si cambia la regla de
-- negocio, se cambia en un solo lugar).
--
-- SUPUESTOS A VALIDAR CON EL EQUIPO:
--  Si "ventas por región/distrito" debe usar la región de la venta o la del cliente (dejé la de la venta por defecto).
-- para esa parte usar la de la venta región/distrito de la venta

-- Si "Ventas por Clasificación" es la de ventas.CLASIFICACION o la de cliente.CLASIFICACION (dejé la de ventas).
--usar el CLASIFICACION de la venta y para otro de los reportes que es CLASIFICACION DEL CLIENTE usar la del cliente

-- =====================================================================


-- =====================================================================
-- 0. ÍNDICES RECOMENDADOS (mejoran mucho el performance de estas vistas)
-- =====================================================================
-- ALTER TABLE ventas         ADD INDEX idx_ventas_fecha (FECHA);
-- ALTER TABLE ventas         ADD INDEX idx_ventas_cliente (ID_CLIENTE);
-- ALTER TABLE ventas         ADD INDEX idx_ventas_asesor (ASESOR);
-- ALTER TABLE ventas         ADD INDEX idx_ventas_estado (ESTADO);
-- ALTER TABLE ventas         ADD INDEX idx_ventas_region (ID_REGION);
-- ALTER TABLE ventas         ADD INDEX idx_ventas_distrito (ID_DISTRITO);
-- ALTER TABLE detalle_venta  ADD INDEX idx_detalle_venta (ID_VENTA);
-- ALTER TABLE detalle_venta  ADD INDEX idx_detalle_estado (ESTADO);
-- ALTER TABLE pagos          ADD INDEX idx_pagos_venta (ID_VENTA);
-- ALTER TABLE pagos          ADD INDEX idx_pagos_tipo_estado (TIPO_DE_PAGO, ESTADO);
-- ALTER TABLE cliente        ADD INDEX idx_cliente_estado (ESTADO);


-- =====================================================================
-- 1. VISTA BASE: vw_ventas_validas
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_validas AS
SELECT
    v.ID_VENTA,
    v.ID_CLIENTE,
    v.FECHA,
    YEAR(v.FECHA)                  AS ANIO,
    MONTH(v.FECHA)                 AS MES,
    DATE_FORMAT(v.FECHA, '%Y-%m')  AS ANIO_MES,
    v.CLASIFICACION                AS VENTA_CLASIFICACION,
    v.ASESOR,
    v.N_COMPROBANTE,
    v.ID_REGION,
    r.REGION                       AS REGION_VENTA,
    v.ID_DISTRITO,
    d.DISTRITO                     AS DISTRITO_VENTA,
    v.ID_LUGAR,
    v.SALIDA_DE_PEDIDO,
    dv.ID_DETALLE,
    dv.CODIGO,
    dv.PRODUCTO,
    dv.CANTIDAD,
    dv.PRECIO_VENTA,
    dv.TOTAL,
    c.CLIENTE,
    c.TIPO_CLIENTE,
    c.ORIGEN,
    c.SEGMENTACION,
    c.COND_TRIBUTARIA,
    c.CLASIFICACION                AS CLIENTE_CLASIFICACION,
    c.REGION                       AS CLIENTE_REGION,
    c.DISTRITO                     AS CLIENTE_DISTRITO,
    c.LUGAR                        AS CLIENTE_LUGAR
FROM ventas v
INNER JOIN detalle_venta dv ON dv.ID_VENTA = v.ID_VENTA
INNER JOIN cliente c        ON c.ID_CLIENTE = v.ID_CLIENTE
LEFT JOIN region r          ON r.ID_REGION = v.ID_REGION
LEFT JOIN distrito d        ON d.ID_DISTRITO = v.ID_DISTRITO
WHERE v.ESTADO = '1'
  AND dv.ESTADO = '1'
  AND NOT EXISTS (
        SELECT 1
        FROM pagos p
        WHERE p.ID_VENTA = v.ID_VENTA
          AND (p.TIPO_DE_PAGO = 'ANULADO' OR p.ESTADO = 'ANULADO')
      );


-- =====================================================================
-- 2. TOTAL DE VENTAS POR MES
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_por_mes AS
SELECT
    ANIO,
    MES,
    ANIO_MES,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)               AS TOTAL_VENTAS,
    ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
FROM vw_ventas_validas
GROUP BY ANIO, MES, ANIO_MES
ORDER BY ANIO, MES;


-- =====================================================================
-- 3. CANTIDAD TOTAL DE VENTAS POR AÑO
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_por_anio AS
SELECT
    ANIO,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)               AS TOTAL_VENTAS,
    ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
FROM vw_ventas_validas
GROUP BY ANIO
ORDER BY ANIO;


-- =====================================================================
-- 4. PRODUCTOS MÁS VENDIDOS (ranking general)
-- =====================================================================
CREATE OR REPLACE VIEW vw_productos_mas_vendidos AS
SELECT
    CODIGO,
    PRODUCTO,
    SUM(CANTIDAD)             AS CANTIDAD_TOTAL,
    SUM(TOTAL)                AS MONTO_TOTAL,
    COUNT(DISTINCT ID_VENTA)  AS NUM_VENTAS
FROM vw_ventas_validas
GROUP BY CODIGO, PRODUCTO
ORDER BY CANTIDAD_TOTAL DESC;


-- =====================================================================
-- 5. PRODUCTOS MENOS VENDIDOS (ranking inverso)
-- =====================================================================
CREATE OR REPLACE VIEW vw_productos_menos_vendidos AS
SELECT
    CODIGO,
    PRODUCTO,
    SUM(CANTIDAD)             AS CANTIDAD_TOTAL,
    SUM(TOTAL)                AS MONTO_TOTAL,
    COUNT(DISTINCT ID_VENTA)  AS NUM_VENTAS
FROM vw_ventas_validas
GROUP BY CODIGO, PRODUCTO
ORDER BY CANTIDAD_TOTAL ASC;
-- Nota: en el backend, para "menos vendidos" normalmente se pagina
-- con LIMIT (ej. top 15) y se puede filtrar CANTIDAD_TOTAL > 0 si no
-- quieres incluir productos con una sola unidad vendida en un evento aislado.


-- =====================================================================
-- 6. CLIENTES QUE MÁS COMPRAN (ranking por monto y por frecuencia)
-- =====================================================================
CREATE OR REPLACE VIEW vw_clientes_top_compradores AS
SELECT
    v.ID_CLIENTE,
    v.CLIENTE,
    v.TIPO_CLIENTE,
    v.ORIGEN,
    COUNT(DISTINCT v.ID_VENTA)  AS NUM_VENTAS,
    SUM(v.TOTAL)                AS TOTAL_COMPRADO,
    MAX(v.FECHA)                AS ULTIMA_COMPRA,
    ROUND(SUM(v.TOTAL) / NULLIF(COUNT(DISTINCT v.ID_VENTA), 0), 2) AS TICKET_PROMEDIO
FROM vw_ventas_validas v
GROUP BY v.ID_CLIENTE, v.CLIENTE, v.TIPO_CLIENTE, v.ORIGEN
ORDER BY TOTAL_COMPRADO DESC;


-- =====================================================================
-- 7. CANTIDAD DE CLIENTES (totales generales para tarjetas KPI)
-- =====================================================================
CREATE OR REPLACE VIEW vw_kpi_clientes AS
SELECT
    (SELECT COUNT(*) FROM cliente WHERE ESTADO = '1')            AS CLIENTES_ACTIVOS_TOTAL,
    (SELECT COUNT(DISTINCT ID_CLIENTE) FROM vw_ventas_validas)   AS CLIENTES_CON_COMPRAS,
    (SELECT COUNT(*) FROM cliente WHERE ESTADO = '1')
      - (SELECT COUNT(DISTINCT ID_CLIENTE) FROM vw_ventas_validas) AS CLIENTES_SIN_COMPRAS;


-- =====================================================================
-- 8. CLIENTES POR ORIGEN
-- =====================================================================
-- 8a. Conteo de clientes por origen (maestro de clientes, sin importar si compraron)
CREATE OR REPLACE VIEW vw_clientes_por_origen AS
SELECT
    ORIGEN,
    COUNT(*) AS NUM_CLIENTES
FROM cliente
WHERE ESTADO = '1'
GROUP BY ORIGEN
ORDER BY NUM_CLIENTES DESC;

-- 8b. Ventas agrupadas por origen del cliente (impacto comercial de cada origen)
CREATE OR REPLACE VIEW vw_ventas_por_origen AS
SELECT
    ORIGEN,
    COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
    COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
    SUM(TOTAL)                 AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY ORIGEN
ORDER BY TOTAL_VENTAS DESC;


-- =====================================================================
-- 9. TIPO_CLIENTE
-- =====================================================================
-- 9a. Conteo de clientes por tipo (maestro)
CREATE OR REPLACE VIEW vw_clientes_por_tipo AS
SELECT
    TIPO_CLIENTE,
    COUNT(*) AS NUM_CLIENTES
FROM cliente
WHERE ESTADO = '1'
GROUP BY TIPO_CLIENTE
ORDER BY NUM_CLIENTES DESC;

-- 9b. Ventas por tipo de cliente
CREATE OR REPLACE VIEW vw_ventas_por_tipo_cliente AS
SELECT
    TIPO_CLIENTE,
    COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
    COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
    SUM(TOTAL)                 AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY TIPO_CLIENTE
ORDER BY TOTAL_VENTAS DESC;


-- =====================================================================
-- 10. VENTAS POR REGIÓN (y por distrito)
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_por_region AS
SELECT
    ID_REGION,
    REGION_VENTA AS REGION,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)               AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY ID_REGION, REGION_VENTA
ORDER BY TOTAL_VENTAS DESC;

CREATE OR REPLACE VIEW vw_ventas_por_distrito AS
SELECT
    ID_REGION,
    REGION_VENTA   AS REGION,
    ID_DISTRITO,
    DISTRITO_VENTA AS DISTRITO,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)               AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY ID_REGION, REGION_VENTA, ID_DISTRITO, DISTRITO_VENTA
ORDER BY TOTAL_VENTAS DESC;


-- =====================================================================
-- 11. VENTAS POR ASESOR (cantidad de ventas + total vendido)
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_por_asesor AS
SELECT
    ASESOR,
    COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
    SUM(TOTAL)                 AS TOTAL_VENTAS,
    COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES_ATENDIDOS,
    ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO
FROM vw_ventas_validas
GROUP BY ASESOR
ORDER BY TOTAL_VENTAS DESC;


-- =====================================================================
-- 12. PRODUCTOS MÁS VENDIDOS POR ASESOR (top N por asesor)
-- =====================================================================
-- Incluye ranking (RANK) para que el backend pueda filtrar top 5/10
-- por asesor con un simple WHERE RANKING <= N.
CREATE OR REPLACE VIEW vw_productos_por_asesor AS
SELECT
    ASESOR,
    CODIGO,
    PRODUCTO,
    CANTIDAD_TOTAL,
    MONTO_TOTAL,
    RANK() OVER (PARTITION BY ASESOR ORDER BY CANTIDAD_TOTAL DESC) AS RANKING
FROM (
    SELECT
        ASESOR,
        CODIGO,
        PRODUCTO,
        SUM(CANTIDAD) AS CANTIDAD_TOTAL,
        SUM(TOTAL)    AS MONTO_TOTAL
    FROM vw_ventas_validas
    GROUP BY ASESOR, CODIGO, PRODUCTO
) sub;
-- Uso típico en backend:
--   SELECT * FROM vw_productos_por_asesor WHERE ASESOR = ? AND RANKING <= 5;


-- =====================================================================
-- 13. VENTAS POR CLASIFICACIÓN (clasificación de la venta/comprobante)
-- =====================================================================
CREATE OR REPLACE VIEW vw_ventas_por_clasificacion AS
SELECT
    VENTA_CLASIFICACION AS CLASIFICACION,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)                AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY VENTA_CLASIFICACION
ORDER BY TOTAL_VENTAS DESC;


-- =====================================================================
-- 14. SALIDAS DE PEDIDO
-- =====================================================================
CREATE OR REPLACE VIEW vw_salidas_de_pedido AS
SELECT
    SALIDA_DE_PEDIDO,
    COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
    SUM(TOTAL)                AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY SALIDA_DE_PEDIDO
ORDER BY NUM_VENTAS DESC;


-- =====================================================================
-- 15. VISTAS ADICIONALES (útiles para un dashboard completo)
-- =====================================================================

-- 15a. KPIs generales de un rango de fechas (tarjetas resumen del dashboard)
--      Se recomienda parametrizar por rango de fechas desde el backend
--      (WHERE FECHA BETWEEN :desde AND :hasta) en vez de una vista fija.
CREATE OR REPLACE VIEW vw_kpi_resumen_mes_actual AS
SELECT
    COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS_MES,
    SUM(TOTAL)                 AS TOTAL_VENTAS_MES,
    COUNT(DISTINCT ID_CLIENTE) AS CLIENTES_ATENDIDOS_MES,
    ROUND(SUM(TOTAL) / NULLIF(COUNT(DISTINCT ID_VENTA), 0), 2) AS TICKET_PROMEDIO_MES
FROM vw_ventas_validas
WHERE ANIO = YEAR(CURDATE()) AND MES = MONTH(CURDATE());

-- 15b. Clientes nuevos por mes (según fecha de creación del cliente)
CREATE OR REPLACE VIEW vw_clientes_nuevos_por_mes AS
SELECT
    YEAR(FECHA_CREACION)                   AS ANIO,
    MONTH(FECHA_CREACION)                  AS MES,
    DATE_FORMAT(FECHA_CREACION, '%Y-%m')   AS ANIO_MES,
    COUNT(*)                               AS CLIENTES_NUEVOS
FROM cliente
WHERE ESTADO = '1'
GROUP BY YEAR(FECHA_CREACION), MONTH(FECHA_CREACION), DATE_FORMAT(FECHA_CREACION, '%Y-%m')
ORDER BY ANIO, MES;

-- 15c. Ventas por segmentación del cliente
CREATE OR REPLACE VIEW vw_ventas_por_segmentacion AS
SELECT
    SEGMENTACION,
    COUNT(DISTINCT ID_CLIENTE) AS NUM_CLIENTES,
    COUNT(DISTINCT ID_VENTA)   AS NUM_VENTAS,
    SUM(TOTAL)                 AS TOTAL_VENTAS
FROM vw_ventas_validas
GROUP BY SEGMENTACION
ORDER BY TOTAL_VENTAS DESC;


-- 15e. Formas / estado de pago (excluyendo anulados, ya filtrados en la base)
--      Útil para ver: contado vs crédito, cuántos pagos regularizados, etc.
CREATE OR REPLACE VIEW vw_ventas_por_tipo_pago AS
SELECT
    p.TIPO_DE_PAGO,
    p.REGULARIZADO,
    p.CANCELADO,
    COUNT(DISTINCT p.ID_VENTA) AS NUM_VENTAS,
    SUM(vv.TOTAL)              AS TOTAL_VENTAS
FROM pagos p
INNER JOIN vw_ventas_validas vv ON vv.ID_VENTA = p.ID_VENTA
WHERE p.TIPO_DE_PAGO <> 'ANULADO' AND p.ESTADO <> 'ANULADO'
GROUP BY p.TIPO_DE_PAGO, p.REGULARIZADO, p.CANCELADO
ORDER BY TOTAL_VENTAS DESC;

-- 15f. Comparativo mes actual vs mes anterior (variación %) para tarjetas de tendencia
CREATE OR REPLACE VIEW vw_comparativo_mensual AS
SELECT
    actual.ANIO_MES,
    actual.NUM_VENTAS,
    actual.TOTAL_VENTAS,
    prev.TOTAL_VENTAS AS TOTAL_VENTAS_MES_ANTERIOR,
    ROUND(
        (actual.TOTAL_VENTAS - prev.TOTAL_VENTAS) / NULLIF(prev.TOTAL_VENTAS, 0) * 100
    , 2) AS VARIACION_PORCENTUAL
FROM vw_ventas_por_mes actual
LEFT JOIN vw_ventas_por_mes prev
    ON prev.ANIO_MES = DATE_FORMAT(
        DATE_SUB(STR_TO_DATE(CONCAT(actual.ANIO_MES, '-01'), '%Y-%m-%d'), INTERVAL 1 MONTH),
        '%Y-%m'
    )
ORDER BY actual.ANIO_MES;

-- 15g. Top clientes por asesor (útil para evaluar cartera de cada vendedor)
CREATE OR REPLACE VIEW vw_top_clientes_por_asesor AS
SELECT
    ASESOR,
    ID_CLIENTE,
    CLIENTE,
    NUM_VENTAS,
    TOTAL_COMPRADO,
    RANKING
FROM (
    SELECT
        ASESOR,
        ID_CLIENTE,
        CLIENTE,
        COUNT(DISTINCT ID_VENTA) AS NUM_VENTAS,
        SUM(TOTAL)               AS TOTAL_COMPRADO,
        RANK() OVER (PARTITION BY ASESOR ORDER BY SUM(TOTAL) DESC) AS RANKING
    FROM vw_ventas_validas
    GROUP BY ASESOR, ID_CLIENTE, CLIENTE
) sub;

-- 15h. Clientes inactivos (activos en el maestro pero sin compras en los últimos N meses)
--      Ejemplo con 6 meses; ajustar el INTERVAL según la necesidad del negocio.
CREATE OR REPLACE VIEW vw_clientes_inactivos AS
SELECT
    c.ID_CLIENTE,
    c.CLIENTE,
    c.TIPO_CLIENTE,
    c.ASESOR,
    MAX(vv.FECHA) AS ULTIMA_COMPRA
FROM cliente c
LEFT JOIN vw_ventas_validas vv ON vv.ID_CLIENTE = c.ID_CLIENTE
WHERE c.ESTADO = '1'
GROUP BY c.ID_CLIENTE, c.CLIENTE, c.TIPO_CLIENTE, c.ASESOR
HAVING MAX(vv.FECHA) IS NULL
    OR MAX(vv.FECHA) < DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
ORDER BY ULTIMA_COMPRA;


