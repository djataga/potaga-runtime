import { state } from './state.js';
import { go } from './nav.js';

export const SPECS = {
  'SPEC-01':{t:'Project scoping',p:'Every screen is scoped to one project. Switching projects swaps the mounted memory stores and reloads MULTI_AGENT_PLAN.md; nothing bleeds across projects.',s:'Spec v4.0 §3 · preamble: single source of truth'},
  'SPEC-02':{t:'Degraded-mode banner',p:'When a preview-gated backend goes down, the runtime swaps the routing table, logs a degraded-mode event, and notifies the user exactly once per session. Silent fallbacks are prohibited.',s:'07_orchestrator §B.9 · SYSTEM_OVERVIEW guardrails'},
  'SPEC-03':{t:'Availability Monitor',p:'Backends carry a GA flag and live status. Non-GA backends are skipped in routing when status is not available, and every route terminates in a GA fallback chain.',s:'routing_matrix.yaml backends · README integration step 4'},
  'SPEC-04':{t:'Budget ledger',p:'Cost is reserved at dispatch with loop, Ultra, and tokenizer multipliers. Soft warning at 80% of ceiling; hard pause for user confirmation at 90%.',s:'07_orchestrator §B.2 · parameters.yaml budget'},
  'SPEC-05':{t:'Quality gates',p:'Coder→Tester requires static analysis, Tester completion requires coverage ≥ 70%, and Docs finalize only after Reviewer approval.',s:'07_orchestrator §B.4 · parameters.yaml quality_gates'},
  'SPEC-06':{t:'Single-writer decision log',p:'Agents post status to their cache partition; only the Orchestrator merges into MULTI_AGENT_PLAN.md. Other shared writes use optimistic concurrency.',s:'CHANGELOG 4.1 fix · 07_orchestrator §B.3'},
  'SPEC-07':{t:'Human checkpoints',p:'Architecture approval, critical security findings, and irreversible decisions raise a human checkpoint. The Orchestrator pauses until the user approves or requests changes.',s:'01_architect §6 · 04_reviewer §9'},
  'SPEC-08':{t:'Gate pipeline strip',p:'The five-phase strip mirrors the gate state machine: passed gates green, active gate amber, downstream gates neutral.',s:'Spec v4.0 §Workflow · 07_orchestrator §B.4'},
  'SPEC-09':{t:'Degraded-mode simulator',p:'Operators can preview the exact routing table the runtime would swap in if a preview backend disappears. Simulation writes nothing to the log.',s:'07_orchestrator §B.9 · availability rules'},
  'SPEC-10':{t:'Bounded escalation ladder',p:'L0 local → L1 Orchestrator → L2 Architect → L3 human. Security-relevant conflicts auto-escalate and Security Override ignores score margins.',s:'Escalation Protocols v1.0 §4–5'},
  'SPEC-11':{t:'Pricing epoch switch',p:'Sonnet 5 intro pricing sunsets Aug 31, 2026. The pricing epoch flips to standard inputs on Sept 1 and the UI shows both epochs with countdown.',s:'parameters.yaml pricing_epoch · 07_orchestrator §B.10'},
  'SPEC-12':{t:'Agent identity cards',p:'Each agent renders its fixed model/effort pair, fallback chain, tool allowlist, and store access exactly as granted at session creation.',s:'prompts 01–06 · Routing Transparency'},
  'SPEC-13':{t:'Governance without hidden knobs',p:'Every runtime-affecting parameter is surfaced from parameters.yaml with allowed range and an auditable config diff.',s:'parameters.yaml · CONTRIBUTING versioning rules'}
};

export function buildDrawer(activeId){
  const body = document.getElementById('drawerBody');
  if(!body) return;
  body.innerHTML = '';
  Object.entries(SPECS).forEach(([id, spec]) => {
    const el = document.createElement('div');
    el.className = 'specitem' + (id === activeId ? ' active' : '');
    el.innerHTML = `<span class="sid">${id}</span><b>${spec.t}</b><p>${spec.p}</p><div class="src">Source: ${spec.s}</div>`;
    el.addEventListener('click', () => jumpToSpec(id));
    body.appendChild(el);
  });
}

export function openDrawer(id){
  buildDrawer(id);
  document.getElementById('drawer')?.classList.add('show');
}

export function closeDrawer(){
  document.getElementById('drawer')?.classList.remove('show');
}

export function jumpToSpec(id){
  const tag = document.querySelector(`.spectag[data-specid="${id}"]`);
  if(!tag) return;
  const scr = tag.closest('.screen');
  if(scr && !scr.classList.contains('active')) go(scr.id.replace('scr-',''));
  buildDrawer(id);
  setTimeout(() => tag.closest('[data-spec]')?.scrollIntoView({block:'center', behavior:'smooth'}), 60);
}

export function setSpec(on){
  state.specMode = on;
  document.body.classList.toggle('specmode', on);
  const specSwitch = document.getElementById('specSwitch');
  if(specSwitch){
    specSwitch.classList.toggle('on', on);
    specSwitch.setAttribute('aria-checked', String(on));
  }
  if(!on) closeDrawer();
}

export function initSpec(){
  const specSwitch = document.getElementById('specSwitch');
  if(specSwitch){
    specSwitch.addEventListener('click', () => setSpec(!state.specMode));
    specSwitch.addEventListener('keydown', e => {
      if(e.key === 'Enter' || e.key === ' '){
        e.preventDefault();
        specSwitch.click();
      }
    });
  }
  document.querySelectorAll('.spectag').forEach(tag => {
    tag.addEventListener('click', e => {
      e.stopPropagation();
      openDrawer(tag.dataset.specid);
    });
  });
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape') closeDrawer();
  });
}
