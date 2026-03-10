/* ======================================================
   BBBIG - A股智能选股与持仓分析  前端核心逻辑
   ====================================================== */

// ==================== 工具函数 ====================

function api(url, options = {}) {
    const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
    return fetch(url, opts).then(r => r.json()).catch(e => {
        console.error('API Error:', e);
        toast('网络请求失败', 'error');
        return null;
    });
}

function toast(msg, type = 'info') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

function formatNum(n) {
    if (n === undefined || n === null || n === '-') return '-';
    return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

// ==================== 页面路由 ====================

const pageTitles = {
    dashboard: '仪表盘',
    selection: '智能选股',
    portfolio: '持仓分析',
    history: '历史记录'
};

function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const pageEl = document.getElementById('page-' + page);
    const navEl = document.querySelector(`[data-page="${page}"]`);
    if (pageEl) pageEl.classList.add('active');
    if (navEl) navEl.classList.add('active');
    document.getElementById('pageTitle').textContent = pageTitles[page] || page;

    // 进入页面时加载数据
    if (page === 'dashboard') loadDashboard();
    else if (page === 'selection') loadSelectionResult();
    else if (page === 'portfolio') loadPortfolioList();
    else if (page === 'history') loadHistory();
}

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', e => {
        e.preventDefault();
        navigateTo(item.dataset.page);
    });
});

// 移动端菜单
document.getElementById('menuToggle').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('open');
});

// ==================== 时钟 ====================

function updateClock() {
    const now = new Date();
    document.getElementById('currentTime').textContent =
        now.toLocaleDateString('zh-CN', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }) +
        ' ' + now.toLocaleTimeString('zh-CN');
}
setInterval(updateClock, 1000);
updateClock();

// ==================== 仪表盘 ====================

async function loadDashboard() {
    const [selResult, portfolio, selStatus, ptfStatus] = await Promise.all([
        api('/api/selection/result'),
        api('/api/portfolio/list'),
        api('/api/selection/status'),
        api('/api/portfolio/analyze/status')
    ]);

    // 统计卡
    const recCount = selResult?.recommendations?.length || 0;
    document.getElementById('statSelectionCount').textContent = recCount;
    document.getElementById('statHoldingCount').textContent = portfolio?.length || 0;
    document.getElementById('statLastSelection').textContent = selStatus?.last_run || '未执行';
    document.getElementById('statLastAnalysis').textContent = ptfStatus?.last_run || '未执行';

    // 推荐列表
    const dashRec = document.getElementById('dashRecommendations');
    if (selResult?.recommendations?.length) {
        dashRec.innerHTML = '<table class="holding-table"><thead><tr><th>#</th><th>代码</th><th>名称</th><th>行业</th><th>当前价</th><th>目标价</th><th>止损价</th></tr></thead><tbody>' +
            selResult.recommendations.map(r =>
                `<tr><td>${r.rank||'-'}</td><td><strong>${r.code||'-'}</strong></td><td>${r.name||'-'}</td><td><span class="stock-industry">${r.industry||'-'}</span></td><td>${formatNum(r.current_price)}</td><td class="profit-up">${r.target_price||'-'}</td><td class="profit-down">${r.stop_loss||'-'}</td></tr>`
            ).join('') + '</tbody></table>';
    }

    // 市场分析
    const dashAnalysis = document.getElementById('dashMarketAnalysis');
    if (selResult?.analysis) {
        dashAnalysis.innerHTML = `<div class="analysis-text">${selResult.analysis}</div>`;
    }

    // 持仓概览
    const dashPtf = document.getElementById('dashPortfolio');
    if (portfolio?.length) {
        dashPtf.innerHTML = '<table class="holding-table"><thead><tr><th>代码</th><th>名称</th><th>成本价</th><th>股数</th></tr></thead><tbody>' +
            portfolio.map(h =>
                `<tr><td><strong>${h.code}</strong></td><td>${h.name||'-'}</td><td>${formatNum(h.cost)}</td><td>${h.shares||'-'}</td></tr>`
            ).join('') + '</tbody></table>';
    }
}

// ==================== 智能选股 ====================

let selectionPolling = null;

async function runSelection() {
    const btn = document.getElementById('btnRunSelection');
    btn.disabled = true;
    const res = await api('/api/selection/run', { method: 'POST' });
    if (res?.ok) {
        toast('选股任务已启动，请耐心等待...', 'success');
        document.getElementById('selectionProgress').style.display = 'block';
        document.getElementById('selectionStatus').textContent = '分析中...';
        document.getElementById('selectionStatus').classList.add('running');
        startSelectionPolling();
    } else {
        toast(res?.msg || '启动失败', 'error');
        btn.disabled = false;
    }
}

function startSelectionPolling() {
    if (selectionPolling) clearInterval(selectionPolling);
    selectionPolling = setInterval(async () => {
        const status = await api('/api/selection/status');
        if (status && !status.running) {
            clearInterval(selectionPolling);
            selectionPolling = null;
            document.getElementById('selectionProgress').style.display = 'none';
            document.getElementById('selectionStatus').textContent = '就绪';
            document.getElementById('selectionStatus').classList.remove('running');
            document.getElementById('btnRunSelection').disabled = false;
            toast('选股分析完成！', 'success');
            loadSelectionResult();
        }
    }, 3000);
}

async function loadSelectionResult() {
    const [result, status] = await Promise.all([
        api('/api/selection/result'),
        api('/api/selection/status')
    ]);

    if (status?.running) {
        document.getElementById('selectionProgress').style.display = 'block';
        document.getElementById('selectionStatus').textContent = '分析中...';
        document.getElementById('selectionStatus').classList.add('running');
        document.getElementById('btnRunSelection').disabled = true;
        if (!selectionPolling) startSelectionPolling();
    }

    if (status?.last_run) {
        document.getElementById('selectionLastRun').textContent = '上次执行: ' + status.last_run;
    }

    if (!result || (!result.recommendations?.length && !result.analysis)) return;

    // 市场分析
    if (result.analysis) {
        document.getElementById('selectionAnalysisCard').style.display = 'block';
        document.getElementById('selectionAnalysisText').textContent = result.analysis;
    }

    // 板块资金
    if (result.hot_sectors) {
        document.getElementById('selectionSectorsCard').style.display = 'block';
        document.getElementById('selectionSectorsText').textContent = result.hot_sectors;
    }

    // 推荐股票
    if (result.recommendations?.length) {
        document.getElementById('selectionResultCard').style.display = 'block';
        const container = document.getElementById('selectionResultList');
        container.innerHTML = result.recommendations.map(r => `
            <div class="stock-card">
                <div class="stock-rank">${r.rank || '?'}</div>
                <div class="stock-name">${r.name || '-'}</div>
                <div class="stock-code">${r.code || '-'}</div>
                <span class="stock-industry">${r.industry || '-'}</span>
                <div class="stock-price">¥ ${formatNum(r.current_price)}</div>
                <div class="stock-meta">
                    <div class="stock-meta-item">
                        <span class="stock-meta-label">买入区间</span>
                        <span class="stock-meta-value">${r.suggested_buy_range || '-'}</span>
                    </div>
                    <div class="stock-meta-item">
                        <span class="stock-meta-label">目标价</span>
                        <span class="stock-meta-value up">${r.target_price || '-'}</span>
                    </div>
                    <div class="stock-meta-item">
                        <span class="stock-meta-label">止损价</span>
                        <span class="stock-meta-value down">${r.stop_loss || '-'}</span>
                    </div>
                </div>
                <div class="stock-reason">${r.reason || '-'}</div>
            </div>
        `).join('');
    }
}

// ==================== 持仓管理 ====================

let portfolioPolling = null;

async function loadPortfolioList() {
    const portfolio = await api('/api/portfolio/list');
    const container = document.getElementById('portfolioList');

    if (!portfolio?.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">💼</div><p>暂无持仓，请在上方添加</p></div>';
        return;
    }

    container.innerHTML = `
        <table class="holding-table">
            <thead><tr><th>代码</th><th>名称</th><th>成本价</th><th>股数</th><th>添加时间</th><th>操作</th></tr></thead>
            <tbody>
                ${portfolio.map(h => `
                    <tr>
                        <td><strong>${h.code}</strong></td>
                        <td>${h.name || '-'}</td>
                        <td>¥ ${formatNum(h.cost)}</td>
                        <td>${h.shares || '-'}</td>
                        <td>${h.add_time || '-'}</td>
                        <td><button class="btn btn-danger btn-sm" onclick="removeHolding('${h.code}')">删除</button></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>`;

    // 检查分析状态
    const status = await api('/api/portfolio/analyze/status');
    if (status?.running) {
        document.getElementById('portfolioProgress').style.display = 'block';
        document.getElementById('portfolioStatus').textContent = '分析中...';
        document.getElementById('portfolioStatus').classList.add('running');
        document.getElementById('btnRunAnalysis').disabled = true;
        if (!portfolioPolling) startPortfolioPolling();
    }

    // 加载已有分析结果
    loadPortfolioAnalysisResult();
}

async function addHolding() {
    const code = document.getElementById('inputCode').value.trim();
    const cost = parseFloat(document.getElementById('inputCost').value);
    const shares = parseInt(document.getElementById('inputShares').value) || 0;
    const name = document.getElementById('inputName').value.trim();

    if (!code || !cost || cost <= 0) {
        toast('请填写股票代码和成本价', 'error');
        return;
    }

    const res = await api('/api/portfolio/add', {
        method: 'POST',
        body: JSON.stringify({ code, cost, shares, name })
    });

    if (res?.ok) {
        toast(`已添加 ${code}`, 'success');
        document.getElementById('inputCode').value = '';
        document.getElementById('inputCost').value = '';
        document.getElementById('inputShares').value = '';
        document.getElementById('inputName').value = '';
        loadPortfolioList();
    } else {
        toast(res?.msg || '添加失败', 'error');
    }
}

async function removeHolding(code) {
    if (!confirm(`确定移除 ${code}？`)) return;
    const res = await api('/api/portfolio/remove', {
        method: 'POST',
        body: JSON.stringify({ code })
    });
    if (res?.ok) {
        toast(`已移除 ${code}`, 'success');
        loadPortfolioList();
    } else {
        toast(res?.msg || '移除失败', 'error');
    }
}

async function runPortfolioAnalysis() {
    const btn = document.getElementById('btnRunAnalysis');
    btn.disabled = true;
    const res = await api('/api/portfolio/analyze', { method: 'POST' });
    if (res?.ok) {
        toast('持仓分析任务已启动...', 'success');
        document.getElementById('portfolioProgress').style.display = 'block';
        document.getElementById('portfolioStatus').textContent = '分析中...';
        document.getElementById('portfolioStatus').classList.add('running');
        startPortfolioPolling();
    } else {
        toast(res?.msg || '启动失败', 'error');
        btn.disabled = false;
    }
}

function startPortfolioPolling() {
    if (portfolioPolling) clearInterval(portfolioPolling);
    portfolioPolling = setInterval(async () => {
        const status = await api('/api/portfolio/analyze/status');
        if (status && !status.running) {
            clearInterval(portfolioPolling);
            portfolioPolling = null;
            document.getElementById('portfolioProgress').style.display = 'none';
            document.getElementById('portfolioStatus').textContent = '就绪';
            document.getElementById('portfolioStatus').classList.remove('running');
            document.getElementById('btnRunAnalysis').disabled = false;
            toast('持仓分析完成！', 'success');
            loadPortfolioAnalysisResult();
        }
    }, 3000);
}

async function loadPortfolioAnalysisResult() {
    const result = await api('/api/portfolio/analyze/result');
    if (!result?.holdings_analysis?.length) {
        document.getElementById('portfolioResultCard').style.display = 'none';
        return;
    }

    document.getElementById('portfolioResultCard').style.display = 'block';
    const container = document.getElementById('portfolioAnalysisResult');

    container.innerHTML = result.holdings_analysis.map(item => {
        if (!item.trend && item.analysis) {
            return `<div class="analysis-card">
                <div class="analysis-card-header">
                    <div class="analysis-card-title">${item.code} ${item.name || ''}</div>
                </div>
                <div class="analysis-detail">${item.analysis}</div>
            </div>`;
        }

        const actionClass = {
            '持有': 'action-hold', '分批卖出': 'action-sell',
            '止损': 'action-stop', '加仓': 'action-buy',
            '卖出': 'action-sell', '清仓': 'action-stop'
        }[item.action] || 'action-hold';

        const riskClass = { '低': 'risk-low', '中': 'risk-mid', '高': 'risk-high' }[item.risk_level] || '';

        const profitPct = item.profit_pct || 0;
        const profitClass = profitPct >= 0 ? 'profit-up' : 'profit-down';

        return `
        <div class="analysis-card">
            <div class="analysis-card-header">
                <div>
                    <div class="analysis-card-title">${item.code} ${item.name || ''}</div>
                    <span class="${profitClass}" style="font-size:14px">盈亏: ${profitPct >= 0 ? '+' : ''}${formatNum(profitPct)}%</span>
                </div>
                <span class="analysis-action ${actionClass}">${item.action || '-'}</span>
            </div>
            <div class="analysis-card-body">
                <div class="analysis-field">
                    <span class="analysis-field-label">成本价</span>
                    <span class="analysis-field-value">¥ ${formatNum(item.cost)}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">当前价</span>
                    <span class="analysis-field-value">¥ ${formatNum(item.current_price)}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">趋势</span>
                    <span class="analysis-field-value">${item.trend || '-'}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">风险等级</span>
                    <span class="analysis-field-value ${riskClass}">${item.risk_level || '-'}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">支撑位</span>
                    <span class="analysis-field-value">${item.support_level || '-'}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">压力位</span>
                    <span class="analysis-field-value">${item.resistance_level || '-'}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">建议卖出点</span>
                    <span class="analysis-field-value profit-up">${item.sell_point || '-'}</span>
                </div>
                <div class="analysis-field">
                    <span class="analysis-field-label">止损价位</span>
                    <span class="analysis-field-value profit-down">${item.stop_loss || '-'}</span>
                </div>
                <div class="analysis-detail">
                    <strong>趋势分析：</strong>${item.trend_analysis || '-'}<br><br>
                    <strong>量能分析：</strong>${item.volume_analysis || '-'}<br><br>
                    <strong>操作理由：</strong>${item.action_reason || '-'}
                </div>
            </div>
        </div>`;
    }).join('');
}

// ==================== 历史记录 ====================

async function loadHistory() {
    const filter = document.getElementById('historyFilter').value;
    const list = await api(`/api/history/list?type=${filter}`);
    const container = document.getElementById('historyList');

    if (!list?.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📋</div><p>暂无历史记录</p></div>';
        return;
    }

    container.innerHTML = `
        <table class="history-table">
            <thead><tr><th>文件名</th><th>类型</th><th>大小</th><th>时间</th></tr></thead>
            <tbody>
                ${list.map(f => `
                    <tr onclick="viewHistory('${f.filename}')">
                        <td>${f.filename}</td>
                        <td><span class="badge ${f.type === 'selection' ? 'badge-selection' : 'badge-portfolio'}">${f.type === 'selection' ? '选股' : '持仓'}</span></td>
                        <td>${(f.size / 1024).toFixed(1)} KB</td>
                        <td>${f.modified}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>`;
}

async function viewHistory(filename) {
    const data = await api(`/api/history/detail?filename=${encodeURIComponent(filename)}`);
    if (!data) return;

    document.getElementById('modalTitle').textContent = filename;
    const body = document.getElementById('modalBody');

    if (filename.startsWith('selection_')) {
        let html = '';
        if (data.analysis) html += `<div class="analysis-text" style="margin-bottom:16px">${data.analysis}</div>`;
        if (data.recommendations?.length) {
            html += '<div class="stock-grid">';
            data.recommendations.forEach(r => {
                html += `<div class="stock-card">
                    <div class="stock-rank">${r.rank || '?'}</div>
                    <div class="stock-name">${r.name || '-'}</div>
                    <div class="stock-code">${r.code || '-'}</div>
                    <span class="stock-industry">${r.industry || '-'}</span>
                    <div class="stock-price">¥ ${formatNum(r.current_price)}</div>
                    <div class="stock-reason">${r.reason || '-'}</div>
                </div>`;
            });
            html += '</div>';
        }
        body.innerHTML = html || '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
    } else {
        body.innerHTML = '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.6">' +
            JSON.stringify(data, null, 2) + '</pre>';
    }

    document.getElementById('historyModal').classList.add('active');
}

function closeModal() {
    document.getElementById('historyModal').classList.remove('active');
}

// 按 Esc 关闭弹窗
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ==================== 初始化 ====================

loadDashboard();
