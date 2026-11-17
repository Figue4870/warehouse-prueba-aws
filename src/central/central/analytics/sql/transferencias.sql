-- Transferencias entre bodegas (Saga, Fase 6) por resultado, últimos ${days} días
WITH transferencias AS (
    SELECT DISTINCT transfer_id, final_state, quantity, duration_seconds, needs_review
    FROM "${database}"."transfers"
    WHERE dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
)
SELECT
    final_state                                        AS resultado,
    count(*)                                           AS transferencias,
    sum(quantity)                                      AS unidades,
    round(avg(duration_seconds), 1)                    AS duracion_promedio_s,
    max(duration_seconds)                              AS duracion_maxima_s,
    sum(CASE WHEN needs_review THEN 1 ELSE 0 END)      AS requieren_revision
FROM transferencias
GROUP BY 1
ORDER BY 2 DESC
