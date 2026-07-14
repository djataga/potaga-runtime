import { state } from './state.js';
import { showToast } from './toast.js';
import { hydrateDashboard } from './hydrate.js';

export function toggleOnboardingItem(id){
  const items = state.dashboard?.onboarding?.checklist || [];
  const item = items.find(entry => entry.id === id);
  if(!item) return;
  item.done = !item.done;
  hydrateDashboard(state.dashboard);
  showToast(`Onboarding checkpoint ${item.done ? 'completed' : 're-opened'} · <span class="mono">${item.title}</span>`);
}

export function initOnboarding(){}
