import { state as uiState } from './state.js';

function setText(id, value){
  const el = document.getElementById(id);
  if(el && value !== undefined && value !== null) el.textContent = value;
}

function setHtml(id, value){
  const el = document.getElementById(id);
  if(el && value !== undefined && value !== null) el.innerHTML = value;
}

function renderAvailability(items = []){
  const host = document.getElementById('availabilityList');
  if(!host) return;
  host.innerHTML = items.map(item => `
    <div class="lamp" ${item.id === 'sol-ultra' ? 'id="solLamp"' : ''}>
      <span class="light ${item.status}"></span>
      <div><b>${item.label}</b><small>${item.note}</small></div>
      <span class="ga">${item.tier}</span>
    </div>
  `).join('');
}

function renderDecisionLog(rows = []){
  const host = document.getElementById('decisionLogList');
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <div class="l"><span class="t">${row.time}</span><span class="ev ${row.type}">${row.type}</span><span class="d">${row.message}</span></div>
  `).join('');
}

function renderAgentActivity(rows = []){
  const host = document.getElementById('agentActivityTable');
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <tr><td><span class="agent ${row.agentClass}">${row.agent}</span></td><td><span class="chip ${row.modelClass}">${row.model}</span></td><td><span class="status ${row.statusClass}"><span class="dot"></span>${row.statusText}</span></td></tr>
  `).join('');
}

function renderRoutingTable(rows = []){
  const host = document.getElementById('routeTableBody');
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <tr ${row.preview ? 'data-sol' : ''}>
      <td class="mono">${row.route}</td>
      <td ${row.preview ? 'data-up' : ''}>${row.primary}</td>
      <td ${row.preview ? 'data-upfb' : ''}>${row.fallback}</td>
      <td style="color:var(--muted);font-size:12px">${row.notes}</td>
    </tr>
  `).join('');
}

function renderConflictLog(rows = []){
  const host = document.getElementById('conflictLogBody');
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <tr class="click" ${row.action || ''}>
      <td class="mono">${row.id}</td>
      <td>${row.type}</td>
      <td>${row.parties}</td>
      <td class="mono">${row.level}</td>
      <td><span class="status s-done"><span class="dot"></span>${row.resolution}</span></td>
    </tr>
  `).join('');
}

function renderEditableParameters(sectionId, rows = []){
  const host = document.getElementById(sectionId);
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <tr data-param-row data-param-key="${row.key}" data-param-section="${sectionId}">
      <td class="mono">${row.key}</td>
      <td class="num"><input class="param-input" data-param-key="${row.key}" type="text" value="${row.value}" /></td>
      <td style="color:var(--muted);font-size:12px">${row.note}</td>
    </tr>
  `).join('');
}

function renderMemoryStores(rows = []){
  const host = document.getElementById('memoryStoresBody');
  if(!host) return;
  host.innerHTML = rows.map(row => `
    <tr><td class="mono">${row.store}</td><td>${row.content}</td><td>${row.access}</td></tr>
  `).join('');
}


function renderProjects(data = {}){
  const list = document.getElementById('projectList');
  const stats = document.getElementById('projectStats');
  if(list){
    list.innerHTML = (data.items || []).map(item => `
      <div class="card" data-project-card data-project-id="${item.id}" data-search="${item.search}" style="cursor:pointer" onclick="selectProject('${item.id}')">
        <div class="chead"><h3>${item.name}</h3><span class="status ${item.statusClass}"><span class="dot"></span>${item.status}</span></div>
        <div class="cbody" style="display:grid;gap:8px">
          <div style="font-size:12.5px;color:var(--muted)">${item.summary}</div>
          <div class="kicker">Stack</div>
          <div>${item.stack}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <span class="chip m-sonnet">${item.primary}</span>
            <span class="chip m-opus">${item.escalation}</span>
            <span class="pill">${item.phase}</span>
          </div>
        </div>
      </div>
    `).join('');
  }
  if(stats){
    stats.innerHTML = (data.stats || []).map(item => `<div class="lamp"><span class="light ${item.state}"></span><div><b>${item.label}</b><small>${item.note}</small></div></div>`).join('');
  }
}

function renderOnboarding(data = {}){
  const host = document.getElementById('onboardingChecklist');
  const timeline = document.getElementById('onboardingTimeline');
  if(host){
    host.innerHTML = (data.checklist || []).map(item => `
      <div class="lamp" data-onboarding-row>
        <span class="light ${item.done ? 'ok' : 'preview'}"></span>
        <div style="flex:1">
          <b>${item.title}</b>
          <small>${item.note}</small>
        </div>
        <button class="btn ghost sm" onclick="toggleOnboardingItem('${item.id}')">${item.done ? 'Mark open' : 'Mark done'}</button>
      </div>
    `).join('');
  }
  if(timeline){
    timeline.innerHTML = (data.timeline || []).map(item => `<div class="l"><span class="t">${item.step}</span><span class="ev ${item.kind}">${item.kind}</span><span class="d">${item.detail}</span></div>`).join('');
  }
}

function renderAdrs(data = {}){
  const list = document.getElementById('adrList');
  const detail = document.getElementById('adrDetail');
  if(list){
    list.innerHTML = (data.items || []).map(item => `
      <button class="navlink ${item.active ? 'active' : ''}" style="width:100%;justify-content:flex-start" onclick="openAdrDetail('${item.id}')">
        <span class="ic">#</span><span class="txt">${item.id} · ${item.title}</span>
      </button>
    `).join('');
  }
  if(detail && data.selected){
    const item = data.items.find(x => x.id === data.selected) || data.items?.[0];
    if(item){
      detail.innerHTML = `
        <div class="kicker">${item.id}</div>
        <h3 style="margin:6px 0 10px">${item.title}</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <span class="pill">Status · ${item.status}</span>
          <span class="pill">Owner · ${item.owner}</span>
          <span class="pill">Scope · ${item.scope}</span>
        </div>
        <p style="font-size:13px;color:var(--muted);margin-bottom:12px">${item.context}</p>
        <div class="grid g2">
          <div class="card"><div class="chead"><h3>Decision</h3></div><div class="cbody">${item.decision}</div></div>
          <div class="card"><div class="chead"><h3>Consequences</h3></div><div class="cbody">${item.consequences}</div></div>
        </div>
        <div class="card" style="margin-top:16px"><div class="chead"><h3>Links</h3></div><div class="cbody"><span class="mono">${item.links}</span></div></div>
      `;
    }
  }
}

export function hydrateDashboard(data){
  uiState.dashboard = data;
  setText('projectName', data.project.name);
  setText('projectCrumb', data.project.crumb);
  setText('progressPillText', data.project.progressText);
  setText('epochLabel', data.project.pricingEpoch);
  setHtml('degradedBannerText', data.project.degradedBannerHtml);

  renderProjects(data.projects);
  renderOnboarding(data.onboarding);
  renderAdrs(data.adrs);
  renderAvailability(data.overview.availability);
  setText('budgetSpentValue', data.overview.budget.spentLabel);
  setText('budgetMetaValue', data.overview.budget.metaLabel);
  setText('budgetReservedValue', data.overview.budget.reservedLabel);
  setText('budgetSoftValue', data.overview.budget.softLabel);
  setText('budgetHardValue', data.overview.budget.hardLabel);
  renderDecisionLog(data.overview.decisionLog);
  renderAgentActivity(data.overview.agentActivity);

  renderRoutingTable(data.routing.matrix);
  renderConflictLog(data.conflicts.log);
  renderEditableParameters('conflictParametersBody', data.settings.conflictResolution);
  renderEditableParameters('qualityParametersBody', data.settings.qualityGates);
  renderMemoryStores(data.settings.memoryStores);
}
