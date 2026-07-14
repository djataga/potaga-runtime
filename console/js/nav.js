import { state } from './state.js';

export function go(id){
  state.activeScreen = id;
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const next = document.getElementById('scr-' + id);
  if(next) next.classList.add('active');
  document.querySelectorAll('.navlink').forEach(n => n.classList.toggle('active', n.dataset.nav === id));
  const content = document.querySelector('.content');
  if(content) content.scrollTop = 0;
}

export function initNav(){
  document.querySelectorAll('.navlink').forEach(n => {
    n.addEventListener('click', () => go(n.dataset.nav));
  });
}
