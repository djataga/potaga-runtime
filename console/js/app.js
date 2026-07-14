import { initNav, go } from './nav.js';
import { initSpec, closeDrawer } from './spec.js';
import { initRouting, renderSol } from './routing.js';
import { initModal, openModal, closeModal } from './modal.js';
import { initCheckpoint } from './checkpoint.js';
import { showToast } from './toast.js';
import { flip } from './tasks.js';
import { getDashboardState } from './api.js';
import { hydrateDashboard } from './hydrate.js';
import { initConfigEditor } from './config-editor.js';
import { initProjectFlow, openProjectSwitcher, applyProjectFilter, selectProject } from './project-flow.js';
import { initOnboarding, toggleOnboardingItem } from './onboarding.js';
import { initAdrBrowser, filterAdrList, openAdrDetail } from './adr-browser.js';

const screens = ['projects','overview','onboarding','plan','routing','conflicts','budget','agents','adrs','settings'];

async function loadScreens(){
  const mount = document.getElementById('screenMount');
  const html = await Promise.all(screens.map(async name => {
    const res = await fetch(`./screens/${name}.html`);
    return res.text();
  }));
  mount.innerHTML = html.join('
');
}

function bindGlobals(){
  window.go = go;
  window.flip = flip;
  window.toast = showToast;
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.closeDrawer = closeDrawer;
  window.openProjectSwitcher = openProjectSwitcher;
  window.applyProjectFilter = applyProjectFilter;
  window.selectProject = selectProject;
  window.toggleOnboardingItem = toggleOnboardingItem;
  window.filterAdrList = filterAdrList;
  window.openAdrDetail = openAdrDetail;
}

async function loadState(){
  const data = await getDashboardState();
  hydrateDashboard(data);
  renderSol();
}

function startPolling(){
  setInterval(async () => {
    try {
      await loadState();
    } catch (_err) {
      showToast('Polling retry deferred · latest state retained');
    }
  }, 60000);
}

async function init(){
  await loadScreens();
  bindGlobals();
  initNav();
  initSpec();
  initRouting();
  initModal();
  initCheckpoint();
  await loadState();
  initConfigEditor();
  initProjectFlow();
  initOnboarding();
  initAdrBrowser();
  go('projects');
  startPolling();
}

init();
