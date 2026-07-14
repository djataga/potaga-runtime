import { updateParameters } from './api.js';
import { showToast } from './toast.js';
import { state } from './state.js';

function normalizeValue(value){
  if(value === 'true') return true;
  if(value == 'false') return false;
  if(value !== '' && !Number.isNaN(Number(value))) return Number(value);
  return value;
}

function collectInputs(){
  const diff = {};
  document.querySelectorAll('.param-input').forEach(input => {
    diff[input.dataset.paramKey] = normalizeValue(input.value.trim());
  });
  return diff;
}

function renderDiff(diff){
  const host = document.getElementById('configDiff');
  if(!host) return;
  const rows = Object.entries(diff).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join('
');
  host.textContent = rows || 'No pending changes.';
}

export function initConfigEditor(){
  const saveBtn = document.getElementById('saveConfigBtn');
  const previewBtn = document.getElementById('previewConfigBtn');
  if(!saveBtn || !previewBtn) return;

  document.querySelectorAll('.param-input').forEach(input => {
    input.addEventListener('input', () => renderDiff(collectInputs()));
  });

  previewBtn.addEventListener('click', () => {
    renderDiff(collectInputs());
    showToast('Config diff regenerated from current form values');
  });

  saveBtn.addEventListener('click', async () => {
    const diff = collectInputs();
    renderDiff(diff);
    const result = await updateParameters({
      scope: 'frontend-parameters',
      diff,
      baselineVersion: state.dashboard?.settings?.version || 'v4.0'
    });
    const status = result.saved ? 'saved to API' : 'preview only';
    showToast(`Parameters ${status} · <span class="mono">config diff</span> ready`);
  });
}
