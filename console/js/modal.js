export function openModal(){
  document.getElementById('overlay')?.classList.add('show');
}

export function closeModal(){
  document.getElementById('overlay')?.classList.remove('show');
}

export function initModal(){
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape') closeModal();
  });
}
