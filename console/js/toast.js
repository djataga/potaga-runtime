let toastTimer;
export function showToast(html){
  const t = document.getElementById('toast');
  if(!t) return;
  t.innerHTML = html;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}
