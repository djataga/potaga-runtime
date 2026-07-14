import { showToast } from './toast.js';

export function initCheckpoint(){
  const btn = document.getElementById('approveBtn');
  if(!btn) return;
  btn.addEventListener('click', () => {
    const cp = document.getElementById('checkpoint');
    if(!cp) return;
    cp.classList.add('approved');
    cp.querySelector('.ic').textContent = '✓';
    document.getElementById('cpText').innerHTML = 'Remediation approved by DJ · 15:02:41 · logged to Decision Log as <span class="mono">escalation-resolved</span>. Reviewer resumed.';
    cp.querySelector('.acts').innerHTML = '<span class="pill" style="background:#E6F4EC;color:var(--ok)"><span class="dot" style="background:var(--ok)"></span>Resolved</span>';
    showToast('Checkpoint approved · <span class="mono">[HUMAN REQUIRED]</span> cleared');
  });
}
