import { state } from './state.js';

export function openAdrDetail(id){
  if(!state.dashboard?.adrs?.items) return;
  state.dashboard.adrs.selected = id;
  state.dashboard.adrs.items = state.dashboard.adrs.items.map(item => ({ ...item, active: item.id === id }));
  const detail = document.getElementById('adrDetail');
  const list = document.getElementById('adrList');
  if(list){
    list.querySelectorAll('.navlink').forEach((btn, index) => {
      btn.classList.toggle('active', state.dashboard.adrs.items[index]?.id === id);
    });
  }
  const item = state.dashboard.adrs.items.find(x => x.id === id);
  if(detail && item){
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

export function filterAdrList(){
  const input = document.getElementById('adrFilter');
  const q = (input?.value || '').trim().toLowerCase();
  document.querySelectorAll('#adrList .navlink').forEach(btn => {
    btn.style.display = btn.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

export function initAdrBrowser(){
  const input = document.getElementById('adrFilter');
  if(input){
    input.addEventListener('input', filterAdrList);
  }
}
