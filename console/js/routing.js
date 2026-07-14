import { state } from './state.js';
import { showToast } from './toast.js';

export function renderSol(){
  const banner = document.getElementById('degradedBanner');
  const lamp = document.querySelector('#solLamp .light');
  const label = document.getElementById('solLabel');
  const solSwitch = document.getElementById('solSwitch');
  if(!banner || !lamp || !label || !solSwitch) return;
  banner.classList.toggle('show', !state.solUp);
  lamp.className = 'light ' + (state.solUp ? 'preview' : 'down');
  label.innerHTML = 'Sol Ultra: <b style="color:' + (state.solUp ? 'var(--warn)' : 'var(--danger)') + '">' + (state.solUp ? 'preview · available' : 'offline') + '</b>';
  solSwitch.classList.toggle('on', !state.solUp);
  solSwitch.style.background = state.solUp ? '#C9D3DC' : 'var(--danger)';
  solSwitch.setAttribute('aria-checked', String(!state.solUp));

  document.querySelectorAll('#routeTable tr[data-sol]').forEach(tr => {
    const up = tr.querySelector('[data-up]');
    const fb = tr.querySelector('[data-upfb]');
    if(!up || !fb) return;
    if(!up.dataset.orig) up.dataset.orig = up.innerHTML;
    if(!fb.dataset.orig) fb.dataset.orig = fb.innerHTML;
    if(!state.solUp){
      const chain = fb.dataset.orig;
      const match = chain.match(/<span[^>]*chip[^>]*>.*?<\/span>/);
      const first = match ? match[0] : '';
      up.innerHTML = first + ' <small style="color:var(--danger);font-family:IBM Plex Mono;font-size:10px">degraded</small>';
      fb.innerHTML = chain.replace(first, '').trim() + ' <span class="chip m-sol" style="opacity:.45;text-decoration:line-through">sol @ ultra</span>';
    } else {
      up.innerHTML = up.dataset.orig;
      fb.innerHTML = fb.dataset.orig;
    }
  });
}

export function initRouting(){
  const solSwitch = document.getElementById('solSwitch');
  if(!solSwitch) return;
  solSwitch.addEventListener('click', () => {
    state.solUp = !state.solUp;
    renderSol();
    showToast(state.solUp ? 'Sol Ultra restored · primary routing table active' : 'Sol Ultra offline · degraded routing table active <span class="mono">[logged]</span>');
  });
  solSwitch.addEventListener('keydown', e => {
    if(e.key === 'Enter' || e.key === ' '){
      e.preventDefault();
      solSwitch.click();
    }
  });
  renderSol();
}
