import { state } from './state.js';
import { showToast } from './toast.js';

export function openProjectSwitcher(){
  const host = document.getElementById('projectFilter');
  if(host) host.focus();
  showToast('Project switcher ready · filter by stack, status, or stage');
}

export function applyProjectFilter(){
  const input = document.getElementById('projectFilter');
  const q = (input?.value || '').trim().toLowerCase();
  document.querySelectorAll('[data-project-card]').forEach(card => {
    card.style.display = card.dataset.search.includes(q) ? '' : 'none';
  });
}

export function selectProject(id){
  const items = state.dashboard?.projects?.items || [];
  const selected = items.find(item => item.id === id);
  if(!selected) return;
  state.dashboard.project.name = selected.name;
  state.dashboard.project.crumb = selected.summary;
  document.getElementById('projectName').textContent = selected.name;
  document.getElementById('projectCrumb').textContent = selected.summary;
  document.querySelectorAll('[data-project-card]').forEach(card => {
    card.classList.toggle('active', card.dataset.projectId === id);
  });
  showToast(`Project switched to <span class="mono">${selected.name}</span>`);
}

export function initProjectFlow(){
  const input = document.getElementById('projectFilter');
  if(input){
    input.addEventListener('input', applyProjectFilter);
  }
}
