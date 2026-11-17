// La URL base es relativa: Nginx (default.conf.template) reenvía /api/* a la API.
const API_BASE = '/api';

const MOVE_HELP = {
    RECEIPT: 'Suma unidades al disponible.',
    SHIPMENT: 'Resta del disponible. Se rechaza si no alcanza (el inventario nunca queda negativo).',
    ADJUSTMENT: 'Corrige un desfase: positivo suma, negativo resta. Genera una alerta DISCREPANCY.',
    RESERVATION: 'Pasa unidades de disponible a reservado.',
    RELEASE: 'Devuelve unidades de reservado a disponible.',
    RETURN: 'Suma unidades devueltas al disponible.',
    // Fase 6: las transferencias se piden en la central (POST /transfers o su tablero /dashboard);
    // la API de la bodega rechaza estos tipos con 409 para que nunca salga algo sin entrar en la otra bodega.
    TRANSFER_IN: 'Las transferencias se piden en la central (Saga, Fase 6).',
    TRANSFER_OUT: 'Las transferencias se piden en la central (Saga, Fase 6).',
};

const money = new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', maximumFractionDigits: 0 });

document.addEventListener('DOMContentLoaded', () => {
    checkHealth();
    loadDashboard().then(loadMetrics); // métricas después de leer la tabla
    loadAlerts();
    loadReports();

    document.getElementById('product-form').addEventListener('submit', createProduct);
    document.getElementById('movement-form').addEventListener('submit', executeMovement);
    document.getElementById('move-type').addEventListener('change', updateMoveHelp);
    document.getElementById('refresh-stock-btn').addEventListener('click', async () => { await loadDashboard(); loadMetrics(); });
    document.getElementById('refresh-metrics-btn').addEventListener('click', loadMetrics);
    document.getElementById('reset-metrics-btn').addEventListener('click', resetMetrics);
    document.getElementById('refresh-alerts-btn').addEventListener('click', loadAlerts);
    document.getElementById('refresh-reports-btn').addEventListener('click', loadReports);
    document.getElementById('report-form').addEventListener('submit', requestReport);
    document.querySelector('.close-modal').addEventListener('click', () => {
        document.getElementById('history-modal').style.display = 'none';
    });
    updateMoveHelp();
});

// ---------- utilidades ----------
// Crea celdas con textContent (NUNCA innerHTML con datos del usuario: evita inyección de HTML/JS)
function cell(text, className) {
    const td = document.createElement('td');
    td.textContent = text ?? '-';
    if (className) td.className = className;
    return td;
}

function pill(text, kind) {
    const span = document.createElement('span');
    span.className = `pill ${kind}`;
    span.textContent = text;
    return span;
}

function formatMoney(value) {
    if (value === null || value === undefined || value === '') return '-';
    return money.format(Number(value));
}

function optionalNumber(id) {
    const value = document.getElementById(id).value;
    return value === '' ? null : value; // se envía como texto: la API lo valida como Decimal
}

function optionalText(id) {
    const value = document.getElementById(id).value.trim();
    return value === '' ? null : value;
}

async function readError(res) {
    try {
        const data = await res.json();
        if (Array.isArray(data.detail)) return data.detail.map(d => d.msg).join(' · ');
        return data.detail || `Error HTTP ${res.status}`;
    } catch {
        return `Error HTTP ${res.status}`;
    }
}

function setBadge(id, label, state) {
    const badge = document.getElementById(id);
    badge.textContent = label;
    badge.className = `status-badge ${state}`;
}

function updateMoveHelp() {
    const type = document.getElementById('move-type').value;
    const qty = document.getElementById('move-quantity');
    document.getElementById('move-help').textContent = MOVE_HELP[type];
    if (type === 'ADJUSTMENT') {
        qty.removeAttribute('min');
        qty.placeholder = 'Ej: -3 o 5';
    } else {
        qty.min = '1';
        qty.placeholder = 'Ej: 50';
    }
}

// ---------- salud ----------
async function checkHealth() {
    try {
        const res = await fetch(`${API_BASE}/health`);
        const data = await res.json();
        document.getElementById('warehouse-id').textContent = data.warehouse_id || '?';
        document.title = `Bodega ${data.warehouse_id} · Warehouse Cloud Platform`;
        setBadge('badge-api', 'API online', 'ok');
        setBadge('badge-db', `PostgreSQL ${data.database}`, data.database === 'ok' ? 'ok' : 'error');
        const redisState = data.redis === 'ok' ? 'ok' : (data.redis === 'disabled' ? 'loading' : 'error');
        setBadge('badge-redis', `Redis ${data.redis}`, redisState);
        // Fase 5: cada recarga puede ser atendida por otra copia de la API (api-1, api-2, api-3)
        setBadge('badge-instance', `Atendió: ${data.instance || '?'}`, 'ok');
    } catch {
        setBadge('badge-api', 'API offline', 'error');
    }
}

// ---------- inventario ----------
async function loadDashboard() {
    const tbody = document.querySelector('#stock-table tbody');
    tbody.replaceChildren();
    try {
        const prodRes = await fetch(`${API_BASE}/products`);
        if (!prodRes.ok) throw new Error(await readError(prodRes));
        const products = await prodRes.json();
        fillProductSelect(products);

        if (products.length === 0) {
            const tr = document.createElement('tr');
            const td = cell('No hay productos registrados.');
            td.colSpan = 9;
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        for (const product of products) {
            const stockRes = await fetch(`${API_BASE}/inventory/${product.id}`);
            const stock = stockRes.ok ? await stockRes.json() : { quantity: '?', reserved_quantity: '?' };
            const cacheStatus = stockRes.headers.get('X-Cache') || '-';

            const tr = document.createElement('tr');
            tr.appendChild(cell(product.id));
            tr.appendChild(cell(product.sku, 'strong'));
            const nameTd = cell(product.name);
            const unit = document.createElement('small');
            unit.className = 'muted block';
            unit.textContent = `${product.unit}${product.category ? ' · ' + product.category : ''}`;
            nameTd.appendChild(unit);
            tr.appendChild(nameTd);
            tr.appendChild(cell([product.supplier, product.lot].filter(Boolean).join(' / ') || '-'));
            tr.appendChild(cell(formatMoney(product.retail_price)));
            tr.appendChild(cell(stock.quantity, stock.quantity === 0 ? 'qty zero' : 'qty'));
            tr.appendChild(cell(stock.reserved_quantity, 'muted'));
            const cacheTd = document.createElement('td');
            cacheTd.appendChild(pill(cacheStatus, cacheStatus.toLowerCase()));
            tr.appendChild(cacheTd);

            const actions = document.createElement('td');
            const history = document.createElement('span');
            history.className = 'action-link';
            history.textContent = 'Historial';
            history.addEventListener('click', () => openHistory(product.id, product.name));
            const remove = document.createElement('span');
            remove.className = 'action-link danger';
            remove.textContent = 'Dar de baja';
            remove.addEventListener('click', () => deactivateProduct(product.id, product.sku));
            actions.append(history, ' · ', remove);
            tr.appendChild(actions);
            tbody.appendChild(tr);
        }
    } catch (err) {
        showToast(`Error al cargar el inventario: ${err.message}`, 'error');
    }
}

function fillProductSelect(products) {
    const select = document.getElementById('move-product-id');
    const previous = select.value;
    select.replaceChildren();
    for (const p of products) {
        const option = document.createElement('option');
        option.value = p.id;
        option.textContent = `#${p.id} · ${p.sku} · ${p.name}`;
        select.appendChild(option);
    }
    if (previous) select.value = previous;
}

async function createProduct(e) {
    e.preventDefault();
    const payload = {
        sku: document.getElementById('sku').value.trim(),
        name: document.getElementById('name').value.trim(),
        unit: document.getElementById('unit').value.trim(),
        category: optionalText('category'),
        lot: optionalText('lot'),
        supplier: optionalText('supplier'),
        supplier_cost: optionalNumber('supplier_cost'),
        retail_price: optionalNumber('retail_price'),
        wholesale_price: optionalNumber('wholesale_price'),
        description: optionalText('description'),
    };
    try {
        const res = await fetch(`${API_BASE}/products`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(await readError(res));
        const data = await res.json();
        showToast(`Producto ${data.sku} creado (inventario en 0)`, 'ok');
        document.getElementById('product-form').reset();
        loadDashboard();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function executeMovement(e) {
    e.preventDefault();
    const payload = {
        product_id: parseInt(document.getElementById('move-product-id').value, 10),
        type: document.getElementById('move-type').value,
        quantity: parseInt(document.getElementById('move-quantity').value, 10),
        lot: optionalText('move-lot'),
        reference: optionalText('move-reference'),
    };
    try {
        const res = await fetch(`${API_BASE}/inventory/movements`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(await readError(res));
        const data = await res.json();
        let msg = `${data.type} registrado. Disponible: ${data.quantity_after}, reservado: ${data.reserved_after}`;
        if (data.alerts_generated.length) msg += ` · ⚠️ ${data.alerts_generated.join(', ')}`;
        showToast(msg, data.alerts_generated.length ? 'warn' : 'ok');
        document.getElementById('move-quantity').value = '';
        document.getElementById('move-reference').value = '';
        await loadDashboard();
        loadAlerts();
        loadMetrics();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function deactivateProduct(productId, sku) {
    if (!window.confirm(`¿Dar de baja ${sku}? Solo se permite si no tiene existencias.`)) return;
    try {
        const res = await fetch(`${API_BASE}/products/${productId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(await readError(res));
        showToast(`${sku} dado de baja (su historial se conserva)`, 'ok');
        loadDashboard();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function openHistory(productId, productName) {
    document.getElementById('modal-title').textContent = `Historial — ${productName}`;
    const tbody = document.getElementById('history-table-body');
    tbody.replaceChildren();
    document.getElementById('history-modal').style.display = 'flex';
    try {
        const res = await fetch(`${API_BASE}/inventory/${productId}/movements`);
        if (!res.ok) throw new Error(await readError(res));
        const movements = await res.json();
        if (movements.length === 0) {
            const tr = document.createElement('tr');
            const td = cell('Sin movimientos.');
            td.colSpan = 8;
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        for (const m of movements) {
            const tr = document.createElement('tr');
            const positive = ['RECEIPT', 'TRANSFER_IN', 'RETURN', 'RELEASE'].includes(m.type) || (m.type === 'ADJUSTMENT' && m.quantity > 0);
            tr.appendChild(cell(m.id));
            tr.appendChild(cell(m.type, positive ? 'in' : 'out'));
            tr.appendChild(cell(m.quantity));
            tr.appendChild(cell(m.quantity_after));
            tr.appendChild(cell(m.reserved_after));
            tr.appendChild(cell(m.lot));
            tr.appendChild(cell(m.reference));
            tr.appendChild(cell(new Date(m.created_at).toLocaleString()));
            tbody.appendChild(tr);
        }
    } catch (err) {
        const tr = document.createElement('tr');
        const td = cell(`Error al leer los movimientos: ${err.message}`);
        td.colSpan = 8;
        tr.appendChild(td);
        tbody.appendChild(tr);
    }
}

// ---------- caché / métricas ----------
async function loadMetrics() {
    const grid = document.getElementById('metrics-grid');
    try {
        const res = await fetch(`${API_BASE}/metrics`);
        const m = await res.json();
        const c = m.cache;
        const items = [
            ['Estado', c.enabled ? c.status : 'apagado'],
            ['TTL', `${c.ttl_seconds} s`],
            ['Hits', c.hits ?? '-'],
            ['Misses', c.misses ?? '-'],
            ['Hit ratio', c.hit_ratio === null ? '-' : `${(c.hit_ratio * 100).toFixed(1)} %`],
            ['Claves en Redis', c.cached_keys ?? '-'],
            ['Consultas SQL (API)', m.sql_queries_total],
            ['Ejemplo de clave', c.key_example],
        ];
        grid.replaceChildren();
        for (const [label, value] of items) {
            const box = document.createElement('div');
            box.className = 'metric';
            const l = document.createElement('span');
            l.className = 'metric-label';
            l.textContent = label;
            const v = document.createElement('span');
            v.className = 'metric-value';
            v.textContent = value;
            box.append(l, v);
            grid.appendChild(box);
        }
    } catch {
        grid.textContent = 'No se pudieron leer las métricas.';
    }
}

async function resetMetrics() {
    await fetch(`${API_BASE}/metrics/reset`, { method: 'POST' });
    loadMetrics();
}

// ---------- alertas ----------
async function loadAlerts() {
    const list = document.getElementById('alerts-list');
    list.replaceChildren();
    try {
        const res = await fetch(`${API_BASE}/alerts?limit=20`);
        const alerts = await res.json();
        if (alerts.length === 0) {
            const li = document.createElement('li');
            li.className = 'muted';
            li.textContent = 'Sin alertas por ahora.';
            list.appendChild(li);
            return;
        }
        for (const a of alerts) {
            const li = document.createElement('li');
            li.appendChild(pill(a.type, a.type === 'OUT_OF_STOCK' ? 'bypass' : 'miss'));
            const text = document.createElement('span');
            text.textContent = ` ${a.message}`;
            const when = document.createElement('small');
            when.className = 'muted block';
            when.textContent = new Date(a.created_at).toLocaleString() +
                (a.notified_at ? ` · ✓ notificada al administrador (${a.notification_channel})` : ' · pendiente de notificar');
            li.append(text, when);
            list.appendChild(li);
        }
    } catch {
        list.textContent = 'No se pudieron leer las alertas.';
    }
}

// ---------- reportes asíncronos (Fase 3) ----------
let reportsTimer = null;

async function requestReport(e) {
    e.preventDefault();
    const params = {};
    const from = document.getElementById('report-from').value;
    const to = document.getElementById('report-to').value;
    if (from) params.date_from = from;
    if (to) params.date_to = to;
    try {
        const res = await fetch(`${API_BASE}/reports`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: document.getElementById('report-type').value, params }),
        });
        if (res.status !== 202) throw new Error(await readError(res));
        const data = await res.json();
        showToast(`202 Accepted · ${data.job_id} en proceso`, 'ok');
        loadReports();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function loadReports() {
    const tbody = document.getElementById('reports-table-body');
    try {
        const res = await fetch(`${API_BASE}/reports/jobs?limit=10`);
        const jobs = await res.json();
        tbody.replaceChildren();
        if (jobs.length === 0) {
            const tr = document.createElement('tr');
            const td = cell('Aún no hay reportes.');
            td.colSpan = 5;
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
        let running = false;
        for (const job of jobs) {
            const tr = document.createElement('tr');
            tr.appendChild(cell(job.id, 'strong'));
            tr.appendChild(cell(job.report_type));
            const statusTd = document.createElement('td');
            const kind = job.status === 'COMPLETED' ? 'hit' : (job.status === 'FAILED' ? 'bypass' : 'miss');
            statusTd.appendChild(pill(job.status, kind));
            tr.appendChild(statusTd);
            tr.appendChild(cell(new Date(job.requested_at).toLocaleString()));
            const resultTd = document.createElement('td');
            if (job.status === 'COMPLETED') {
                const link = document.createElement('a');
                link.className = 'action-link';
                link.href = `${API_BASE}/reports/jobs/${encodeURIComponent(job.id)}/download`;
                link.textContent = `Descargar CSV (${job.result.rows.length} filas)`;
                resultTd.appendChild(link);
            } else {
                resultTd.textContent = job.error || '…';
                running = running || job.status === 'PENDING' || job.status === 'PROCESSING';
            }
            tr.appendChild(resultTd);
            tbody.appendChild(tr);
        }
        clearTimeout(reportsTimer);
        if (running) reportsTimer = setTimeout(loadReports, 1500);  // sigue consultando hasta que termine
    } catch {
        tbody.textContent = 'No se pudieron leer los reportes.';
    }
}

function showToast(message, type) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.style.background = type === 'ok' ? '#15803d' : (type === 'warn' ? '#a16207' : '#b91c1c');
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 4500);
}
